"""Write the emails Last Bell sends as HTML files: a digest, a daily
summary, and the watcher-health notice. For screenshots and design work;
every name is made up (the summary comes from ``seed-demo`` data).

    python scripts/render_emails.py --out data/email-preview
"""
from __future__ import annotations

import argparse
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from lastbell import health, seed, store, summary
from lastbell.notify import render

DIGEST = [
    ("M.R.", [
        ("assignment_missing", "AP Biology: “Enzyme Lab Write-up” is marked missing [Canvas]"),
        ("upcoming_deadline", "Algebra 2: “Unit 3 Test” due Thu Sep 10"),
        ("upcoming_deadline", "English 10: “Persuasive Essay Draft” due Fri Sep 11 [Canvas]"),
        ("grade_changed", "US History: “DBQ Practice” graded: 18/20"),
    ]),
    ("E.R.", [
        ("ungraded_past_due", "Reading: “Book Report” (was due Tue Sep 1) is still ungraded"),
        ("grade_drop", "Math 5: overall 93.2% (A) → 87.8% (B+)"),
    ]),
]


def digest() -> str:
    return render.alerts(DIGEST, title="Afternoon digest").html


def daily_summary(today: date, agu: str = "D1") -> str:
    with tempfile.TemporaryDirectory() as tmp:
        conn = store.connect(Path(tmp) / "demo.db")
        try:
            store.ensure_schema(conn)
            seed.seed_demo(conn, seed=2026)
            row = conn.execute("SELECT id, initials FROM students WHERE agu = ?",
                               (agu,)).fetchone()
            return summary.build(conn, row["id"], row["initials"], today=today).html
        finally:
            conn.close()


def failure() -> str:
    since = (datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
    state = health.Health(failures=2, since=since, kind="login",
                          detail="Invalid user id or password")
    return health.failure_message(state, host="pihole")[1].html


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/email-preview")
    ap.add_argument("--student", default="D2", help="seed-demo AGU for the summary")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, html in (("email-digest", digest()),
                       ("email-summary", daily_summary(date.today(), args.student)),
                       ("email-failure", failure())):
        (out / f"{name}.html").write_text(html, encoding="utf-8")
        print(f"wrote {out / name}.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
