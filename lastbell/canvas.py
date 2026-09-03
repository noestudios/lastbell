"""Canvas (Instructure) — the second, *leading* source.

ParentVUE is the gradebook of record; Canvas is where the work actually
lives. Teachers post assignments there the day they assign them, students
turn work in there, and the "missing" flag is set there — the Synergy
gradebook sees all of it only when the teacher syncs, days later if at all.
So Canvas feeds the day-to-day cards (Needs attention, Due soon) and
ParentVUE keeps the course grades and the finals.

**How we get in.** Canvas has a real, documented REST API, and the parent's
Canvas identity is provisioned from the student-information system: the
portal's home page ("Launch Pad") carries a district tile whose link is
Canvas's SAML entry, and Synergy itself is the identity provider. So the
ParentVUE session we already hold is the only credential — following that
link once per poll yields a Canvas session cookie. A personal access token
(``lastbell set-canvas-token``) skips the hop when the district allows one.

**What we read** (all GETs, all observer-scoped, paginated by the API):

    /api/v1/users/self/observees                       the linked students
    /api/v1/courses?include[]=observed_users           which course is whose
    /api/v1/users/{student}/courses/{course}/assignments?include[]=submission
    /api/v1/courses/{course}/assignment_groups         category names

**What we keep.** Published assignments that carry a due date or points —
the ungraded, undated "read this page" items a course shell accumulates are
noise a parent can't act on. Canvas's own words are trusted (``missing`` →
MISSING, a posted score → GRADED, a submission → SUBMITTED); everything else
is DUE and the time rules take it from there.

**How it merges.** A Canvas course is matched to the ParentVUE course of the
same title (period prefixes and the ``-Teacher-S1-2027`` suffix stripped),
and its assignments attach to that course row keyed ``canvas:<id>``, so they
never collide with gradebook GUIDs. A Canvas assignment whose name matches
a ParentVUE assignment in the same course is *superseded*: the gradebook
copy is the record, so the Canvas row is hidden from counts and lists and
the differ stops alerting on it — but it is kept and updated, and when the
two disagree on a grade the dashboard shows "Canvas says …" on the
gradebook row and one alert asks you to check with the teacher. Unmatched
Canvas courses with real work become their own rows (source ``canvas``)
with no course grade.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import unescape
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

import requests

from .client import USER_AGENT, Child, ParentVueClient
from .models import (
    SOURCE_CANVAS,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
)

KEY_PREFIX = "canvas:"
# Pause between Canvas API calls: the pace of a parent clicking through
# courses, well inside the per-token throttle.
CALL_DELAY_S = 0.25
_JSON_PREFIX = re.compile(r"^\s*while\(1\);")


class CanvasError(RuntimeError):
    """Canvas couldn't be reached or answered unexpectedly. Never fatal to a
    poll: the ParentVUE snapshot proceeds without the Canvas layer."""


def with_deadline(fn: Callable[[], object], seconds: float, what: str):
    """Run ``fn`` with a wall-clock cap. Per-request timeouts don't cover
    everything (name resolution, a library waiting on a prompt), and the
    Canvas layer is optional: past the deadline the poll gets a CanvasError
    and carries on with the gradebook. The stuck worker is a daemon thread,
    so it can't keep the process alive either."""
    import threading

    result: dict = {}

    def run() -> None:
        try:
            result["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — re-raised on the caller's thread
            result["error"] = e

    t = threading.Thread(target=run, name=f"canvas:{what}", daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise CanvasError(f"{what} took longer than {seconds:g}s and was abandoned")
    if "error" in result:
        raise result["error"]
    return result.get("value")


# ── the API client ────────────────────────────────────────────────────


class CanvasClient:
    """Read-only Canvas REST calls over a session cookie (the SAML hop) or a
    personal access token (``Authorization: Bearer``)."""

    def __init__(self, host: str, *, session: Optional[requests.Session] = None,
                 token: str = "", delay_s: float = CALL_DELAY_S) -> None:
        self.host = host
        self.base = f"https://{host}"
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self._token = token
        self._delay = delay_s
        self._calls = 0

    @property
    def calls(self) -> int:
        return self._calls

    def get(self, path: str, **params):
        """One GET, JSON-decoded; a list result follows ``Link: rel=next``
        pages until exhausted."""
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = self.base + path
        out: list = []
        while url:
            if self._calls and self._delay:
                time.sleep(self._delay)
            self._calls += 1
            r = self.session.get(url, params=params if "?" not in url else None,
                                 headers=headers, timeout=30, allow_redirects=False)
            if r.status_code == 401:
                raise CanvasError("Canvas said the session isn't signed in (401)")
            if r.status_code != 200:
                raise CanvasError(f"Canvas answered HTTP {r.status_code} for {path}")
            try:
                data = json.loads(_JSON_PREFIX.sub("", r.text))
            except ValueError as e:
                raise CanvasError(f"Canvas returned non-JSON for {path}") from e
            if not isinstance(data, list):
                return data
            out.extend(data)
            url = (r.links.get("next") or {}).get("url")
            params = {}
        return out


# ── getting in ────────────────────────────────────────────────────────

_CANVAS_HINT = re.compile(r"instructure\.com|/login/saml|canvas", re.I)
_SAML_FORM = re.compile(r"<form[^>]+action=\"([^\"]+)\"[^>]*>(.*?)</form>", re.I | re.S)
_INPUT = re.compile(r'<input[^>]+name="([^"]+)"[^>]*value="([^"]*)"', re.I)


def discover_launch_url(pv: ParentVueClient) -> Optional[str]:
    """The Canvas entry link on the portal's Launch Pad, or None when the
    district hasn't put one there."""
    for title, url in pv.launch_pad_links():
        if _CANVAS_HINT.search(url):
            return url
    return None


def saml_login(session: requests.Session, launch_url: str) -> str:
    """Follow the Launch Pad link through the SAML exchange. Canvas redirects
    to the SIS's identity provider, which — for a signed-in portal session —
    answers with an auto-submitting form carrying the assertion. Submit it
    and Canvas sets its session cookie. Returns the Canvas host."""
    r = session.get(launch_url, timeout=30, allow_redirects=True)
    for _ in range(3):
        m = _SAML_FORM.search(r.text)
        if not (m and re.search(r'name="SAML(Response|Request)"', m.group(2))):
            break
        action = unescape(m.group(1))
        fields = {k: unescape(v) for k, v in _INPUT.findall(m.group(2))}
        r = session.post(action, data=fields, timeout=30, allow_redirects=True)
    host = urlparse(r.url).netloc
    if not host or "SamlAuth" in r.url or "/login" in urlparse(r.url).path:
        raise CanvasError("the SAML hand-off didn't end at a signed-in Canvas page")
    return host


def connect(pv: Optional[ParentVueClient], *, host: str = "", token: str = "",
            delay_s: float = CALL_DELAY_S) -> CanvasClient:
    """A signed-in client. Token + host → bearer auth, no portal hop.
    Otherwise ride the portal session through the Launch Pad link (``host``
    optionally pins which link to expect)."""
    if token and host:
        client = CanvasClient(host, token=token, delay_s=delay_s)
    else:
        if pv is None:
            raise CanvasError("no ParentVUE session to hand off from")
        launch = discover_launch_url(pv)
        if launch is None:
            raise CanvasError("no Canvas link on the portal's home page "
                              "(set LASTBELL_CANVAS=off to stop looking)")
        if host and host not in launch:
            raise CanvasError(f"the portal's Canvas link points at "
                              f"{urlparse(launch).netloc}, not LASTBELL_CANVAS_HOST")
        pv.login()
        found = saml_login(pv.session, launch)
        client = CanvasClient(host or found, session=pv.session, delay_s=delay_s)
    me = client.get("/api/v1/users/self")
    if not isinstance(me, dict) or "id" not in me:
        raise CanvasError("Canvas didn't recognise the session")
    return client


# ── who is who ────────────────────────────────────────────────────────


@dataclass
class Observee:
    id: int
    name: str

    @property
    def tokens(self) -> set[str]:
        return _name_tokens(self.name)


def _name_tokens(name: str) -> set[str]:
    # "Hays, Jasper P (Student)" / "JASPER P HAYS" / "Jasper" → {hays, jasper, p}
    return {w.lower() for w in re.findall(r"[A-Za-z]+", re.sub(r"\(.*?\)", "", name))}


def match_students(children: Iterable[Child], observees: Iterable[Observee]
                   ) -> dict[str, Observee]:
    """ParentVUE AGU → Canvas observee, by name. The portal may give only a
    first name; Canvas gives "Last, First". A child matches an observee when
    every token of the portal name appears in the Canvas name — and only if
    that pairing is unambiguous both ways. A lone child and a lone observee
    pair regardless (one family, one student)."""
    children, observees = list(children), list(observees)
    if len(children) == 1 and len(observees) == 1:
        return {children[0].agu: observees[0]}
    out: dict[str, Observee] = {}
    taken: set[int] = set()
    for child in children:
        ct = _name_tokens(child.name)
        hits = [o for o in observees if ct and ct <= o.tokens and o.id not in taken]
        if len(hits) == 1:
            out[child.agu] = hits[0]
            taken.add(hits[0].id)
    return out


# ── course names ──────────────────────────────────────────────────────

# "Hon Biology A-Yeh-S1-2027" → title "Hon Biology A", teacher "Yeh".
_CANVAS_SUFFIX = re.compile(r"^(?P<title>.+?)-(?P<teacher>[A-Za-z'’.\- ]+)-[A-Z]\d-\d{4}$")
# "7: Hon Biology A" → "Hon Biology A".
_PERIOD_PREFIX = re.compile(r"^\d+\s*:\s*")


def split_course_name(name: str) -> tuple[str, str]:
    """(title, teacher surname) from a Canvas course name; teacher '' when
    the district doesn't encode one."""
    m = _CANVAS_SUFFIX.match(name.strip())
    if m:
        return m.group("title").strip(), m.group("teacher").strip()
    return name.strip(), ""


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _PERIOD_PREFIX.sub("", title).lower())


def match_course(canvas_name: str, courses: Iterable[Course]) -> Optional[Course]:
    """The ParentVUE course this Canvas course is, by normalized title."""
    want = norm_title(split_course_name(canvas_name)[0])
    if not want:
        return None
    hits = [c for c in courses if norm_title(c.title) == want]
    return hits[0] if len(hits) == 1 else None


# ── assignments ───────────────────────────────────────────────────────


def _local_date(iso: Optional[str]) -> Optional[date]:
    """Canvas timestamps are UTC ISO-8601; a due date of 03:59:59Z is 11:59pm
    the previous evening in Maryland, so convert to the local calendar."""
    if not iso:
        return None
    try:
        return (datetime.fromisoformat(iso.replace("Z", "+00:00"))
                .astimezone().date())
    except ValueError:
        return None


def keep_assignment(a: dict) -> bool:
    """Only work a parent can act on: published, and dated or worth points.
    Excused work is dropped outright — nothing to chase, nothing to grade."""
    if not a.get("published", True) or a.get("workflow_state") == "deleted":
        return False
    sub = a.get("submission") or {}
    if sub.get("excused"):
        return False
    points = a.get("points_possible") or 0
    return bool(a.get("due_at")) or points > 0


def to_assignment(a: dict, course_gu: str, kinds: Optional[dict] = None) -> Assignment:
    """Normalize one Canvas assignment (+ the observed student's submission)."""
    sub = a.get("submission") or {}
    score = sub.get("score")
    if sub.get("missing"):
        status = AssignmentStatus.MISSING
    elif score is not None:
        status = AssignmentStatus.GRADED
    elif sub.get("submitted_at") or sub.get("workflow_state") in ("submitted", "pending_review"):
        status = AssignmentStatus.SUBMITTED
    else:
        status = AssignmentStatus.DUE
    kind = (kinds or {}).get(a.get("assignment_group_id"), "")
    raw = {k: a.get(k) for k in ("id", "name", "due_at", "points_possible",
                                  "html_url", "submission_types",
                                  "assignment_group_id", "published")}
    raw["submission"] = {k: sub.get(k) for k in (
        "workflow_state", "score", "grade", "submitted_at", "graded_at",
        "late", "missing", "excused", "posted_at")}
    return Assignment(
        edupoint_gu=f"{KEY_PREFIX}{a['id']}",
        course_gu=course_gu,
        name=(a.get("name") or "").strip(),
        kind=kind,
        due_date=_local_date(a.get("due_at")),
        graded_at=_local_date(sub.get("graded_at")) if score is not None else None,
        score=float(score) if score is not None else None,
        points=float(a["points_possible"]) if a.get("points_possible") is not None else None,
        status=status,
        source=SOURCE_CANVAS,
        raw=raw,
    )


# ── one collection pass ───────────────────────────────────────────────


@dataclass
class CanvasCourse:
    id: int
    name: str
    title: str
    teacher: str
    term: str
    assignments: list[Assignment] = field(default_factory=list)

    @property
    def gu(self) -> str:
        return f"{KEY_PREFIX}{self.id}"


@dataclass
class CanvasCollection:
    observee: Observee
    courses: list[CanvasCourse] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def observees(client: CanvasClient) -> list[Observee]:
    return [Observee(id=int(o["id"]), name=o.get("sortable_name") or o.get("name", ""))
            for o in client.get("/api/v1/users/self/observees", per_page=50)]


def collect(client: CanvasClient, observee: Observee, *,
            courses_cache: Optional[list] = None) -> CanvasCollection:
    """Every active course this observer watches for ``observee``, with its
    kept assignments. One course failing is reported, not fatal."""
    out = CanvasCollection(observee=observee)
    if courses_cache is None:
        courses_cache = client.get("/api/v1/courses", per_page=100,
                                   **{"enrollment_state": "active",
                                      "include[]": ["observed_users", "term"]})
    for c in courses_cache:
        enrollments = c.get("enrollments") or []
        if not any(e.get("associated_user_id") == observee.id for e in enrollments):
            continue
        title, teacher = split_course_name(c.get("name") or "")
        course = CanvasCourse(id=int(c["id"]), name=c.get("name") or "", title=title,
                              teacher=teacher, term=(c.get("term") or {}).get("name") or "")
        try:
            groups = {g["id"]: g.get("name", "") for g in client.get(
                f"/api/v1/courses/{course.id}/assignment_groups", per_page=100)}
            rows = client.get(
                f"/api/v1/users/{observee.id}/courses/{course.id}/assignments",
                per_page=100, **{"include[]": ["submission"], "order_by": "due_at"})
        except CanvasError as e:
            out.errors.append(f"{title}: {e}")
            continue
        course.assignments = [to_assignment(a, course.gu, groups)
                              for a in rows if keep_assignment(a)]
        out.courses.append(course)
    return out


# Canvas's own catch-all term — every instance has one, by this name. District
# shells (password reset, expectations training, "Class of 2030") live there;
# real classes get a real term. Never worth a course row of its own.
DEFAULT_TERM = "default term"


def merge(snapshot: Snapshot, collection: CanvasCollection, *,
          skip: Iterable[str] = ()) -> dict:
    """Fold a Canvas collection into the student's ParentVUE snapshot, in
    place. Returns counts for the poll's log line.

    A Canvas course that matches a gradebook course always contributes. One
    that matches nothing gets its own row only when it looks like a class:
    it has kept work, it isn't in Canvas's Default Term, it's in the same
    term as the classes that *did* match (when any did), and its name isn't
    on the ``skip`` list (``LASTBELL_CANVAS_SKIP`` fragments, case-blind).
    """
    stats = {"matched": 0, "own": 0, "assignments": 0, "superseded": 0, "skipped": 0}
    pv_courses = [c for c in snapshot.courses if c.source != SOURCE_CANVAS]
    pv_names: dict[str, dict[str, str]] = {}      # course gu -> {name: gu}
    for a in snapshot.assignments:
        if a.source != SOURCE_CANVAS and a.edupoint_gu:
            pv_names.setdefault(a.course_gu, {}).setdefault(_norm_name(a.name), a.edupoint_gu)
    skip = [f.strip().lower() for f in skip if f.strip()]
    targets = {cc.id: match_course(cc.name, pv_courses) for cc in collection.courses}
    class_terms = {cc.term.lower() for cc in collection.courses
                   if targets[cc.id] is not None and cc.term}
    for cc in collection.courses:
        target = targets[cc.id]
        if target is not None:
            stats["matched"] += 1
            course_gu = target.edupoint_gu
        elif not cc.assignments:
            continue
        elif (cc.term.lower() == DEFAULT_TERM
              or (class_terms and cc.term.lower() not in class_terms)
              or any(f in cc.name.lower() for f in skip)):
            stats["skipped"] += 1
            continue
        else:
            stats["own"] += 1
            course_gu = cc.gu
            snapshot.courses.append(Course(
                edupoint_gu=course_gu, title=cc.title, teacher=cc.teacher,
                term=snapshot.term, source=SOURCE_CANVAS))
        taken = pv_names.get(course_gu, {})
        for a in cc.assignments:
            a.course_gu = course_gu
            twin = taken.get(_norm_name(a.name))
            if twin:
                # The gradebook copy is the record. Keep this row — updated,
                # hidden — so a disagreement between the two can be noticed.
                a.superseded_by = twin
                stats["superseded"] += 1
            else:
                stats["assignments"] += 1
            snapshot.assignments.append(a)
    return stats


def _norm_name(name: str) -> str:
    return " ".join(name.split()).lower()


# Submission types a student hands in *through Canvas* — for these, "past
# due with no submission" means not turned in. Paper, in-class, and
# external-tool work may well be done; Canvas just can't see it.
ONLINE_TYPES = frozenset({
    "online_upload", "online_text_entry", "online_url", "online_quiz",
    "discussion_topic", "media_recording", "student_annotation",
})


def submits_online(a: Assignment) -> bool:
    """True when this Canvas assignment is turned in through Canvas itself."""
    types = (a.raw or {}).get("submission_types") or []
    return any(t in ONLINE_TYPES for t in types)


# ── the whole layer, as the poll sees it ──────────────────────────────


class CanvasLayer:
    """Connect once per poll, then ``apply`` per student. Any failure is a
    warning through ``warn`` and the ParentVUE snapshot goes on untouched."""

    def __init__(self, client: CanvasClient, children: Iterable[Child],
                 warn: Callable[[str], None] = lambda m: None,
                 skip: Iterable[str] = ()) -> None:
        self.client = client
        self.warn = warn
        self.skip = list(skip)
        self._by_agu = match_students(children, observees(client))
        self._courses: Optional[list] = None

    @property
    def matched(self) -> dict[str, Observee]:
        return dict(self._by_agu)

    def apply(self, snapshot: Snapshot) -> Optional[dict]:
        obs = self._by_agu.get(snapshot.student_agu)
        if obs is None:
            return None
        try:
            if self._courses is None:
                self._courses = self.client.get(
                    "/api/v1/courses", per_page=100,
                    **{"enrollment_state": "active",
                       "include[]": ["observed_users", "term"]})
            col = collect(self.client, obs, courses_cache=self._courses)
        except CanvasError as e:
            self.warn(f"Canvas: {e} — this poll used the gradebook only")
            return None
        for err in col.errors:
            self.warn(f"Canvas: {err} — that course was skipped this poll")
        return merge(snapshot, col, skip=self.skip)
