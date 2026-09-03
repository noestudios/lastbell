"""Message rendering: text for every channel, an HTML twin for email."""
from __future__ import annotations

import datetime

from lastbell.notify import render
from lastbell.notify.email import build_message


def test_message_is_a_str_with_an_html_twin():
    m = render.Message("hello", "<p>hello</p>")
    assert m == "hello" and "hell" in m and m.html == "<p>hello</p>"
    assert render.Message("plain").html == ""


def test_subject_counts_by_kind_in_severity_order():
    s = render.subject(["J.P.H."], ["grade_changed", "upcoming_deadline",
                                    "assignment_missing", "grade_changed"])
    assert s == "[Last Bell] J.P.H.: 1 missing, 1 due soon, 2 grade changes"
    assert render.subject(["J.", "L."], ["term_final"]) == "[Last Bell] J., L.: 1 term final"


def test_item_html_splits_course_name_and_tags_canvas():
    h = render.item_html("1: Spanish 2A: “Unit 3 Practice” is marked missing [Canvas]")
    assert ">1: Spanish 2A<" in h                      # the period prefix survives
    assert "<strong>Unit 3 Practice</strong> is marked missing" in h
    assert ">Canvas</span>" in h and "[Canvas]" not in h
    h = render.item_html("Algebra 2: overall 91.4% (A-) → 88.7% (B+)")
    assert ">Algebra 2<" in h and "overall 91.4% (A-) → 88.7% (B+)" in h
    assert render.item_html("MP1 closed — final grades: Math 90%").startswith("<div>MP1 closed")
    assert "&lt;" in render.item_html("X: “<b>” graded: 1/1")   # escaped


def test_alerts_groups_by_meaning_and_names_students_only_when_several():
    one = render.alerts([("J.", [("grade_changed", "Math: “Quiz” graded: 9/10"),
                                 ("assignment_missing", "Art: “Collage” is marked missing")])])
    assert str(one).splitlines()[0] == "Needs attention"      # severity first
    assert "J." not in str(one).splitlines()[0]
    assert "Collage" in one.html and "NEEDS ATTENTION" in one.html.upper()
    two = render.alerts([("J.", [("grade_changed", "Math: “Quiz” graded: 9/10")]),
                         ("L.", [("upcoming_deadline", "Bio: “Lab” due Fri Sep 4")])])
    assert str(two).startswith("J.\n  Grades posted\n    • Math")
    assert "<h2" in two.html and "L.</h2>" in two.html


def test_email_carries_both_parts_or_just_text():
    msg = build_message("a@x", "b@y", "Subj", render.Message("text body", "<p>html body</p>"))
    assert msg.get_content_type() == "multipart/alternative"
    parts = {p.get_content_type(): p.get_content() for p in msg.iter_parts()}
    assert parts["text/plain"].strip() == "text body"
    assert "<p>html body</p>" in parts["text/html"]
    plain = build_message("a@x", "b@y", "Subj", "just text")
    assert plain.get_content_type() == "text/plain"


def test_summary_html_lists_overall_and_groups():
    html = render.summary_html("J.", datetime.date(2026, 9, 3),
                               [("Math", "87.2% (B+)")],
                               [("Missing (1)", "Needs attention", ["Math: “Collage” is marked missing"])],
                               ["Math: “Quiz” graded: 9/10"], all_clear=False)
    assert "Daily summary for J." in html and "87.2% (B+)" in html
    assert "Missing (1)" in html and "<strong>Collage</strong>" in html
    assert "This week" in html


def test_sample_has_no_real_names():
    subject, body = render.sample()
    assert subject.endswith("(sample)") and body.html and "A.B." in subject
