"""Message rendering: one plain-text body every channel can carry, plus an
HTML alternative that email attaches.

The plain text is the message — ntfy, Telegram, Pushover, and the console
all show it as-is, and it is what a lock screen previews. ``Message`` is a
``str`` subclass so every transport that only knows strings keeps working;
the email channel notices the ``.html`` riding along and sends both parts
(``multipart/alternative``), so a mail client shows the styled version and
falls back to the text.

Two shapes share the look:

* **alerts** — events from a poll or a digest, grouped by what they mean
  (Needs attention / Slipping / Coming up / Grades posted), per student.
* **summary** — the daily "where things stand" (built in ``summary.py``,
  rendered here).

Everything stays low-PII: initials, course, assignment — never a name.
"""
from __future__ import annotations

import re
from datetime import date
from html import escape
from typing import Iterable, Sequence

# ── the carrier ───────────────────────────────────────────────────────


class Message(str):
    """Plain text with an optional HTML twin. ``str(msg)`` / ``msg`` is the
    text; ``msg.html`` is '' when no alternative exists."""

    html: str

    def __new__(cls, text: str, html: str = "") -> "Message":
        obj = super().__new__(cls, text)
        obj.html = html
        return obj


# ── vocabulary ────────────────────────────────────────────────────────

# alert type -> (group heading, subject noun, singular/plural)
_KINDS = {
    "assignment_missing": ("Needs attention", "missing", "missing"),
    "grade_drop": ("Needs attention", "grade drop", "grade drops"),
    "ungraded_past_due": ("Slipping", "past due", "past due"),
    "upcoming_deadline": ("Coming up", "due soon", "due soon"),
    "grade_changed": ("Grades posted", "grade change", "grade changes"),
    "term_final": ("Marking period closed", "term final", "term finals"),
    "source_conflict": ("Check with the teacher", "gradebook/Canvas mismatch",
                        "gradebook/Canvas mismatches"),
    "daily_summary": ("Summary", "summary", "summaries"),
}
_GROUP_ORDER = ["Needs attention", "Slipping", "Check with the teacher", "Coming up",
                "Grades posted", "Marking period closed", "Summary", "Other"]
_GROUP_COLOR = {
    "Needs attention": "#b42318",
    "Slipping": "#b54708",
    "Check with the teacher": "#b54708",
    "Coming up": "#0e7490",
    "Grades posted": "#475467",
    "Marking period closed": "#475467",
    "Summary": "#475467",
    "Other": "#475467",
}
_INK, _MUTED, _RULE, _PAGE, _CARD = "#1f2933", "#667085", "#e4e7ec", "#f2f4f7", "#ffffff"
_FONT = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, "
         "Arial, sans-serif")


def _kind(alert_type: str) -> tuple[str, str, str]:
    return _KINDS.get(alert_type, ("Other", alert_type.replace("_", " "),
                                   alert_type.replace("_", " ")))


# ── subjects ──────────────────────────────────────────────────────────


def subject(initials: Sequence[str], types: Iterable[str]) -> str:
    """``[Last Bell] J.P.H.: 1 missing, 2 due soon`` — the counts by kind, in
    severity order, so the subject line alone says whether to open it now."""
    counts: dict[str, int] = {}
    for t in types:
        counts[t] = counts.get(t, 0) + 1
    order = list(_KINDS)
    parts = []
    for t in sorted(counts, key=lambda t: order.index(t) if t in order else len(order)):
        _, one, many = _kind(t)
        n = counts[t]
        parts.append(f"{n} {one if n == 1 else many}")
    who = ", ".join(dict.fromkeys(initials)) or "your student"
    return f"[Last Bell] {who}: {', '.join(parts) or 'update'}"


# ── one event line ────────────────────────────────────────────────────

# “{course}: “{name}” {rest}” — the differ's assignment-level shape.
_ASSIGNMENT = re.compile(r"^(?P<course>.+?): “(?P<name>[^”]*)”\s*(?P<rest>.*)$", re.S)
# “{course}: overall …” — the course-level shape.
_COURSE = re.compile(r"^(?P<course>.+?): (?P<rest>overall .*)$", re.S)
_VIA = re.compile(r"\s*\[(Canvas)\]\s*$")


def _pill(label: str) -> str:
    return (f"<span style=\"display:inline-block;margin-left:6px;padding:0 7px;"
            f"border:1px solid {_RULE};border-radius:999px;font-size:11px;"
            f"line-height:18px;color:{_MUTED};vertical-align:1px\">{escape(label)}</span>")


def item_html(detail: str) -> str:
    """One event as two short lines: the course, muted; then the assignment
    in bold with what happened. A ``[Canvas]`` tail becomes a pill."""
    via = ""
    m = _VIA.search(detail)
    if m:
        via, detail = _pill(m.group(1)), detail[:m.start()]
    m = _ASSIGNMENT.match(detail)
    if m:
        course, name, rest = m.group("course"), m.group("name"), m.group("rest")
        return (f"<div style=\"font-size:12px;color:{_MUTED}\">{escape(course)}</div>"
                f"<div><strong>{escape(name)}</strong> {escape(rest)}{via}</div>")
    m = _COURSE.match(detail)
    if m:
        return (f"<div style=\"font-size:12px;color:{_MUTED}\">{escape(m.group('course'))}</div>"
                f"<div>{escape(m.group('rest'))}{via}</div>")
    return f"<div>{escape(detail)}{via}</div>"


# ── alerts (poll events, digests) ─────────────────────────────────────

Item = tuple[str, str]   # (alert_type, detail)


def _grouped(items: Iterable[Item]) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for alert_type, detail in items:
        groups.setdefault(_kind(alert_type)[0], []).append(detail)
    return [(g, groups[g]) for g in _GROUP_ORDER if g in groups]


def alerts(sections: Sequence[tuple[str, Sequence[Item]]], *, title: str = "") -> Message:
    """``sections`` is ``[(student initials, [(alert_type, detail), …]), …]``.
    One student → no student heading (the subject already names them)."""
    many = len(sections) > 1
    lines: list[str] = []
    parts: list[str] = []
    for initials, items in sections:
        if many:
            lines.append(initials)
            parts.append(f"<h2 style=\"margin:18px 0 6px;font-size:16px;color:{_INK}\">"
                         f"{escape(initials)}</h2>")
        for group, details in _grouped(items):
            lines.append(f"{'  ' if many else ''}{group}")
            lines.extend(f"{'  ' if many else ''}  • {d}" for d in details)
            parts.append(_group_html(group, [item_html(d) for d in details]))
        lines.append("")
    text = "\n".join(lines).rstrip()
    heading = title or (f"{sum(len(i) for _, i in sections)} updates")
    return Message(text, _document(heading, "".join(parts)))


def _group_html(label: str, rows: list[str]) -> str:
    color = _GROUP_COLOR.get(label, _GROUP_COLOR["Other"])
    body = "".join(
        f"<tr><td style=\"padding:8px 12px;border-left:3px solid {color};"
        f"border-bottom:1px solid {_RULE};font-size:14px;line-height:20px;"
        f"color:{_INK}\">{r}</td></tr>" for r in rows)
    return (f"<h3 style=\"margin:16px 0 4px;font-size:12px;letter-spacing:.06em;"
            f"text-transform:uppercase;color:{color}\">{escape(label)}</h3>"
            f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" width=\"100%\" "
            f"style=\"border-collapse:collapse\">{body}</table>")


# ── daily summary ─────────────────────────────────────────────────────


def summary_html(initials: str, today: date, overall: Sequence[tuple[str, str]],
                 groups: Sequence[tuple[str, str, Sequence[str]]],
                 recent: Sequence[str], all_clear: bool) -> str:
    """``overall`` is ``[(course, shown grade)]``; ``groups`` is
    ``[(label, color-key, [event-style detail lines])]``; ``recent`` are alert
    details from the past week."""
    parts = [f"<div style=\"font-size:12px;color:{_MUTED};margin:0 0 12px\">"
             f"{escape(today.strftime('%A, %B ') + str(today.day))}</div>"]
    if overall:
        rows = "".join(
            f"<tr><td style=\"padding:6px 0;border-bottom:1px solid {_RULE};font-size:14px;"
            f"color:{_INK}\">{escape(c)}</td>"
            f"<td align=\"right\" style=\"padding:6px 0;border-bottom:1px solid {_RULE};"
            f"font-size:14px;color:{_INK};white-space:nowrap\"><strong>{escape(g)}</strong></td></tr>"
            for c, g in overall)
        parts.append(f"<h3 style=\"margin:8px 0 4px;font-size:12px;letter-spacing:.06em;"
                     f"text-transform:uppercase;color:{_MUTED}\">Overall</h3>"
                     f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
                     f"width=\"100%\" style=\"border-collapse:collapse\">{rows}</table>")
    if all_clear:
        parts.append(f"<p style=\"margin:16px 0;font-size:14px;color:{_INK}\">"
                     "Nothing missing, nothing overdue, nothing due soon. 🎉</p>")
    for label, key, details in groups:
        if details:
            parts.append(_group_html(label if key not in _GROUP_COLOR else label,
                                     [item_html(d) for d in details])
                         .replace(_GROUP_COLOR.get(label, _GROUP_COLOR["Other"]),
                                  _GROUP_COLOR.get(key, _GROUP_COLOR["Other"])))
    if recent:
        parts.append(_group_html("This week's alerts", [item_html(d) for d in recent]))
    return _document(f"Daily summary for {initials}", "".join(parts))


# ── the page ──────────────────────────────────────────────────────────


def _document(heading: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"color-scheme\" content=\"light dark\">"
        f"<title>{escape(heading)}</title>"
        "<style>@media (prefers-color-scheme: dark){"
        ".lb-page{background:#101828!important}"
        ".lb-card{background:#1d2939!important}"
        ".lb-card, .lb-card td, .lb-card div, .lb-card p, .lb-card h1, .lb-card h2"
        "{color:#e4e7ec!important}"
        ".lb-muted, .lb-card td div:first-child{color:#98a2b3!important}"
        "}</style></head>"
        f"<body class=\"lb-page\" style=\"margin:0;padding:24px 12px;background:{_PAGE};"
        f"font-family:{_FONT}\">"
        "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" align=\"center\" "
        "width=\"100%\" style=\"max-width:560px;margin:0 auto\"><tr><td>"
        f"<div style=\"font-size:12px;letter-spacing:.08em;text-transform:uppercase;"
        f"color:{_MUTED};padding:0 4px 8px\">Last Bell</div>"
        f"<div class=\"lb-card\" style=\"background:{_CARD};border:1px solid {_RULE};"
        f"border-radius:12px;padding:20px 20px 12px\">"
        f"<h1 style=\"margin:0 0 4px;font-size:20px;line-height:26px;color:{_INK}\">"
        f"{escape(heading)}</h1>"
        f"{body}</div>"
        f"<div class=\"lb-muted\" style=\"font-size:11px;line-height:16px;color:{_MUTED};"
        f"padding:12px 4px 0\">Sent by your own Last Bell — initials only, by design. "
        f"Change what you get in the dashboard's Settings or with "
        f"<code>lastbell subscribe</code>.</div>"
        "</td></tr></table></body></html>")


# ── a sample, for “does it look right?” ───────────────────────────────

SAMPLE_ITEMS: list[Item] = [
    ("assignment_missing", "Algebra 2: “Unit 3 Practice Set” is marked missing [Canvas]"),
    ("ungraded_past_due", "Biology: “Cell Lab Report” (was due Tue Sep 1) is still ungraded"),
    ("upcoming_deadline", "English 9: “Personal Narrative Draft” due Fri Sep 4 [Canvas]"),
    ("grade_changed", "US History: “Map Quiz” graded: 9/10"),
    ("grade_changed", "Algebra 2: overall 91.4% (A-) → 88.7% (B+)"),
]


def sample() -> tuple[str, Message]:
    """A realistic alert message with made-up courses — what a real one will
    look like, without anyone's data."""
    types = [t for t, _ in SAMPLE_ITEMS]
    return (subject(["A.B."], types) + " (sample)",
            alerts([("A.B.", SAMPLE_ITEMS)], title="Sample alerts for A.B."))
