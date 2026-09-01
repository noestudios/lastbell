"""Demo-data seeder: a realistic quarter-end database, no real students.

``lastbell seed-demo`` fabricates the Rivera family — a high schooler with
seven courses and an elementary kid with one — then replays two marking
periods of school days chronologically through the *real* pipeline:
``apply_time_rules`` → ``diff`` → ``record_alert`` → ``persist_snapshot``.
Statuses, history rows, the term rollover, and every alert's wording are
therefore exactly what production writes; the only fabrication is the portal
snapshots themselves, plus rewriting each pass's timestamp defaults to the
simulated day. Nothing is ever sent: alerts are recorded, not dispatched.

The output answers the UX plan's density question ("design against
quarter-end volume, not week-one data"): ~25–40 assignments per class, a
graded backlog with a few missing / due-soon / ungraded-past-due stragglers,
months of course-percent history for trend charts, and a term of alerts.
Deterministic for a given ``seed``, so screenshots are reproducible.
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from . import store
from . import watchers as watchermod
from .differ import apply_time_rules, diff
from .models import Assignment, AssignmentStatus, Course, Snapshot, Student, WatcherKind

# ── the fabricated household ──────────────────────────────────────────

_HS = Student(agu="D1", name="Maya Rivera", school="Bellwood High",
              initials="M.R.")
_ES = Student(agu="D2", name="Eli Rivera", school="Bellwood Elementary",
              initials="E.R.")

# (title, teacher, personality). Personalities shape the percent trajectory
# so trend charts have something to say: steady lines, a slide, a recovery,
# and one volatile mess. The slide bombs one big test so a GRADE_DROP fires.
_HS_COURSES = (
    ("1: Hon English 9A", "S. Whitfield", "steady_high"),
    ("2: Algebra 1A", "D. Okonkwo", "slipping"),
    ("3: Hon Biology A", "R. Vance", "volatile"),
    ("4: US History A", "M. Petrova", "recovering"),
    ("5: Spanish 2A", "J. Delgado", "steady_mid"),
    ("6: Theatre HS 1A", "L. Marsh", "steady_high"),
    ("7: Chorus HS 1A", "A. Boateng", "steady_high"),
)

_TOPICS = {
    "1: Hon English 9A": ("Narrative Voice", "Poetry Annotations", "The Odyssey",
                          "Argument Draft", "Socratic Seminar", "Vocabulary"),
    "2: Algebra 1A": ("Visual Patterns", "Linear Equations", "Inequalities",
                      "Systems", "Exponents", "Sequences"),
    "3: Hon Biology A": ("Cell Structure", "Macromolecules", "Enzymes",
                         "Photosynthesis", "Mitosis", "Lab Safety"),
    "4: US History A": ("Reconstruction", "Gilded Age", "Progressive Era",
                        "Primary Sources", "Immigration", "DBQ Practice"),
    "5: Spanish 2A": ("Todo sobre mí", "La familia", "El pretérito",
                      "La comida", "De compras", "El barrio"),
    "6: Theatre HS 1A": ("Stage Directions", "Monologue", "Improv",
                         "Scene Study", "Tech Roles", "One Acts"),
    "7: Chorus HS 1A": ("Sight Reading", "Warm-up Technique", "Fall Concert",
                        "Sectionals", "Rhythm Studies", "Solfège"),
    "Ms. Okafor's class": ("Reading Log", "Math Facts", "Science Journal",
                           "Spelling", "Book Report", "Show and Tell"),
}

# (kind, points, weight): weights pick the mix; big assessments are rare.
_KINDS = (("Homework", 10, 5), ("Classwork", 15, 4), ("Quiz", 30, 3),
          ("Assessment", 100, 1), ("Project", 50, 1))


@dataclass
class _Planned:
    """Everything the fake portal will ever say about one assignment."""
    gu: str
    course_gu: str
    name: str
    kind: str
    points: float
    assigned: date
    due: date
    graded_on: Optional[date] = None   # score appears this day
    score: Optional[float] = None
    missing_on: Optional[date] = None  # portal flags it missing this day


def _quality(personality: str, progress: float, rng: random.Random) -> float:
    """Score fraction for one assignment, given quarter progress in [0, 1]."""
    mean = {
        "steady_high": 0.95,
        "steady_mid": 0.86,
        "slipping": 0.93 - 0.21 * progress,
        "recovering": 0.72 + 0.20 * progress,
        "volatile": 0.85,
    }[personality]
    sd = 0.10 if personality == "volatile" else 0.045
    return max(0.3, min(1.0, rng.gauss(mean, sd)))


def _school_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]


def _plan_course(rng: random.Random, course_gu: str, title: str, term: str,
                 start: date, end: date, personality: str,
                 count: int) -> list[_Planned]:
    """Lay out one course's quarter: what's assigned, due, graded, missed."""
    days = _school_days(start, end)
    topics = _TOPICS[title]
    kinds = [k for k in _KINDS for _ in range(k[2])]
    planned: list[_Planned] = []
    bombed = rng.randrange(count // 3, count // 2) if personality == "slipping" else -1
    for i in range(count):
        kind, points, _w = ("Assessment", 100, 1) if i == bombed \
            else rng.choice(kinds)
        due = days[min(int(i / count * len(days)) + rng.randrange(0, 3),
                       len(days) - 1)]
        topic = topics[i % len(topics)]
        unit = i // len(topics) + 1
        name = {
            "Homework": f"U{unit}L{i % 9 + 1} HW: {topic}",
            "Classwork": f"Exit Ticket ({topic})",
            "Quiz": f"Quiz {i % 9 + 1}: {topic}",
            "Assessment": f"Unit {unit} Test ({topic})",
            "Project": f"{topic} Project",
        }[kind]
        p = _Planned(gu=f"{course_gu}-{term}-{i}", course_gu=course_gu,
                     name=name, kind=kind, points=float(points),
                     assigned=due - timedelta(days=rng.randrange(3, 11)),
                     due=due)
        roll = rng.random()
        if i == bombed:
            p.graded_on = due + timedelta(days=rng.randrange(1, 4))
            p.score = round(points * 0.35)
        elif roll < 0.04 and personality in ("slipping", "volatile", "recovering"):
            p.missing_on = due + timedelta(days=rng.randrange(1, 4))
        elif roll < 0.07:
            pass                        # teacher never grades it: past-due bait
        else:
            p.graded_on = due + timedelta(days=rng.randrange(1, 8))
            p.score = round(p.points * _quality(personality, i / count, rng) * 2) / 2
        planned.append(p)
    return planned


def _mark(pct: float) -> str:
    for cut, letter in ((89.5, "A"), (79.5, "B"), (69.5, "C"), (59.5, "D")):
        if pct >= cut:
            return letter
    return "E"


def _snapshot_on(day: date, term: str, courses: list[tuple[Course, str]],
                 plans: dict[str, list[_Planned]], agu: str,
                 grade_courses: bool) -> Snapshot:
    """What the portal would show for this student on this day."""
    snap_courses, assignments = [], []
    for course, _personality in courses:
        earned = possible = 0.0
        for p in plans[course.edupoint_gu]:
            if p.assigned > day:
                continue
            if p.graded_on and p.graded_on <= day:
                status, score = AssignmentStatus.GRADED, p.score
                earned += p.score or 0.0
                possible += p.points
            elif p.missing_on and p.missing_on <= day:
                status, score = AssignmentStatus.MISSING, None
            else:
                status, score = AssignmentStatus.DUE, None   # time rules refine
            assignments.append(Assignment(
                edupoint_gu=p.gu, course_gu=course.edupoint_gu, name=p.name,
                kind=p.kind, assigned=p.assigned, due_date=p.due,
                graded_at=p.graded_on if status is AssignmentStatus.GRADED else None,
                score=score, points=p.points, status=status))
        pct = earned / possible * 100 if grade_courses and possible else None
        snap_courses.append(Course(
            edupoint_gu=course.edupoint_gu, title=course.title,
            teacher=course.teacher, term=term,
            mark=_mark(pct) if pct is not None else "",
            percent=f"{pct:.2f}%" if pct is not None else ""))
    return Snapshot(student_agu=agu, courses=snap_courses,
                    assignments=assignments, term=term)


def seed_demo(conn: sqlite3.Connection, *, seed: int = 2026,
              today: Optional[date] = None) -> dict:
    """Populate an (empty, schema'd) connection with the demo season."""
    rng = random.Random(seed)
    today = today or date.today()

    # Two nine-week quarters; the current one ends just after "today", so the
    # dashboard shows genuine quarter-end density plus a live due-soon edge.
    mp2_end = today + timedelta(days=4)
    mp2_start = mp2_end - timedelta(days=62)
    mp1_end = mp2_start - timedelta(days=3)
    mp1_start = mp1_end - timedelta(days=62)

    hs_courses = [(Course(edupoint_gu=f"c{i}", title=t, teacher=teach),
                   pers) for i, (t, teach, pers) in enumerate(_HS_COURSES)]
    es_courses = [(Course(edupoint_gu="e0", title="Ms. Okafor's class",
                          teacher="Adaeze Okafor"), "steady_high")]

    plans: dict[str, dict[str, list[_Planned]]] = {"MP1": {}, "MP2": {}}
    for term, start, end in (("MP1", mp1_start, mp1_end),
                             ("MP2", mp2_start, mp2_end)):
        for course, pers in hs_courses:
            plans[term][course.edupoint_gu] = _plan_course(
                rng, course.edupoint_gu, course.title, term, start, end,
                pers, rng.randrange(25, 41))
        for course, pers in es_courses:
            plans[term][course.edupoint_gu] = _plan_course(
                rng, course.edupoint_gu, course.title, term, start, end,
                pers, rng.randrange(12, 17))

    # Replay every school day through the real pipeline, then stamp the rows
    # this pass created (their defaults say "now") with the simulated day.
    n_alerts = 0
    for day in _school_days(mp1_start, today):
        term = "MP1" if day <= mp1_end else "MP2"
        start = mp1_start if term == "MP1" else mp2_start
        for student, courses, graded in ((_HS, hs_courses, True),
                                         (_ES, es_courses, False)):
            snap = _snapshot_on(day, term, courses, plans[term], student.agu, graded)
            apply_time_rules(snap, today=day)
            gh0, ch0, al0 = [conn.execute(
                f"SELECT COALESCE(MAX({key}), 0) FROM {table}").fetchone()[0]
                for table, key in (("grade_history", "id"),
                                   ("course_history", "id"), ("alerts", "rowid"))]
            events = diff(store.load_snapshot(conn, student.agu), snap, today=day)
            store.persist_snapshot(conn, student, snap)
            for ev in events:
                store.record_alert(conn, student.agu, ev)
            n_alerts += len(events)
            ts = f"{day} 20:{rng.randrange(60):02d}:{rng.randrange(60):02d}"
            conn.execute("UPDATE grade_history SET seen_at=? WHERE id>?", (ts, gh0))
            conn.execute("UPDATE course_history SET seen_at=? WHERE id>?", (ts, ch0))
            conn.execute("UPDATE alerts SET created_at=? WHERE rowid>?", (ts, al0))
    conn.commit()

    # Watchers + subscriptions, mirroring the recommended setup: a guardian
    # on the 16:00 digest with urgent-now, and the student on urgent types.
    mom = watchermod.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                                 {"email": {"to": "mom.demo@example.com"}})
    maya = watchermod.add_watcher(conn, "Maya", WatcherKind.STUDENT,
                                  {"sms": {"to": "3015550123@vtext.com"}})
    for agu in (_HS.agu, _ES.agu):
        watchermod.subscribe(conn, mom, agu, send_at="16:00", urgent_now=True)
    watchermod.subscribe(conn, maya, _HS.agu,
                         ["assignment_missing", "upcoming_deadline"], ["sms"])

    # A household keeps up, mostly: ack the bulk of the older alerts.
    cutoff = f"{today - timedelta(days=5)} 00:00:00"
    for i, row in enumerate(conn.execute(
            "SELECT id, created_at FROM alerts WHERE created_at < ? "
            "ORDER BY created_at", (cutoff,)).fetchall()):
        if rng.random() < 0.85:
            who = mom.id if i % 3 else maya.id
            conn.execute(
                "UPDATE alerts SET acked_by=?, acked_at=? WHERE id=?",
                (who, row["created_at"][:11] + "22:30:00", row["id"]))
    conn.commit()

    return {
        "students": 2,
        "terms": ("MP1", "MP2"),
        "assignments": conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0],
        "alerts": n_alerts,
        "course_history": conn.execute("SELECT COUNT(*) FROM course_history").fetchone()[0],
        "span_days": (today - mp1_start).days,
    }
