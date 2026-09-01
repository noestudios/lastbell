"""Dashboard: routing + rendering against a real (temp) database."""
from __future__ import annotations

import datetime

import pytest

from mcpsgradewatch import dashboard, store, watchers
from mcpsgradewatch.differ import Event
from mcpsgradewatch.models import (
    AlertType,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
    Student,
    WatcherKind,
)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def populated(conn):
    snap = Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="709775", title="Math <Adv>", teacher="Pat Example",
                        term="MP1", mark="B+", percent="87.20%")],
        assignments=[
            Assignment(edupoint_gu="a1", course_gu="709775", name="Fractions Quiz",
                       kind="Assessment", due_date=datetime.date(2026, 9, 12),
                       score=8.0, points=10.0, status=AssignmentStatus.GRADED),
            Assignment(edupoint_gu="a2", course_gu="709775", name="Collage",
                       status=AssignmentStatus.MISSING),
        ],
    )
    store.persist_snapshot(
        conn, Student(agu="1", name="Jasper P. Hays", school="Example ES",
                      initials="J.P.H."), snap)
    return conn


def _get(conn, path):
    return dashboard._handle(conn, path)


def test_overview_empty_db(conn):
    status, html = _get(conn, "/")
    assert status == 200
    assert "No students yet" in html


def test_overview_lists_students_and_flags(populated):
    status, html = _get(populated, "/")
    assert status == 200
    assert "Jasper P. Hays" in html
    assert "Math &lt;Adv&gt;" in html          # escaped
    assert "1 missing" in html
    assert ">87.2<" in html          # one-decimal display of the raw "87.20%"


def test_student_page_shows_assignments(populated):
    status, html = _get(populated, "/student/1")
    assert status == 200
    assert "Fractions Quiz" in html
    assert "80.0%" in html                    # score as a percentage…
    assert "title='8/10'" in html             # …raw points on hover
    assert "87.2% · B+" in html               # course heading, one decimal
    assert "MISSING" in html


def test_unknown_student_404s(populated):
    status, html = _get(populated, "/student/999")
    assert status == 404


def test_alerts_page(populated):
    store.record_alert(populated, "1", Event(
        type=AlertType.GRADE_CHANGED, student_agu="1", course_title="Math",
        detail="Math: “Fractions Quiz” graded: 8/10"))
    status, html = _get(populated, "/alerts")
    assert status == 200
    assert "grade changed" in html
    assert "Fractions Quiz" in html


def test_history_page(populated):
    # regrade -> one history row
    snap = Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="709775", title="Math <Adv>", term="MP1")],
        assignments=[Assignment(edupoint_gu="a1", course_gu="709775",
                                name="Fractions Quiz", score=9.0, points=10.0,
                                due_date=datetime.date(2026, 9, 12), kind="Assessment",
                                status=AssignmentStatus.GRADED)],
    )
    store.persist_snapshot(populated, Student(agu="1", name="Jasper P. Hays"), snap)
    status, html = _get(populated, "/history")
    assert status == 200
    assert "8.0 → 9.0" in html


def test_settings_page(populated):
    w = watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "mom@example.com"}})
    watchers.subscribe(populated, w, "1")
    status, html = _get(populated, "/settings")
    assert status == 200
    assert "Mom" in html
    assert "all configured" in html
    # a real web UI: forms for every write path, not CLI listings
    assert "mcpsgradewatch watcher" not in html
    assert "mcpsgradewatch subscribe" not in html
    for action in ("watcher-add", "watcher-remove", "channel", "channel-remove",
                   "subscribe", "subscription-update", "unsubscribe"):
        assert f"action='/settings/{action}'" in html
    # env-owned config is absent: if it can't be changed here, don't show it
    assert "MCPSGRADEWATCH_POLL_MINUTES" not in html
    assert "Polling" not in html
    # quiet hours are descoped from the web UI (CLI only)
    assert "quiet" not in html.lower()
    # subscribe form offers the one-step all-students option
    assert "all students" in html
    # add forms come above their tables
    assert (html.index("action='/settings/watcher-add'")
            < html.index("<table class='manage'>"))
    assert (html.index("action='/settings/subscribe'")
            < html.index("<table class='manage'>", html.index("Subscriptions")))
    # the web UI offers exactly email and text message as channels
    assert "text message" in html
    for gone in ("ntfy", "telegram", "pushover"):
        assert f"<option value='{gone}'>" not in html


def test_settings_channels_are_rows_under_their_watcher(populated):
    w = watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "mom@example.com"}})
    _, html = _get(populated, "/settings")
    # the channel row: name cell, address input preloaded, bound to a row form
    assert "<td class='chname'>email</td>" in html
    assert "value='mom@example.com'" in html
    assert f"form='ch-{w.id}-email'" in html
    # the add-channel row offers only channels the watcher doesn't have yet
    start = html.index(f"chadd-{w.id}")
    add_select = html[start:html.index("</select>", start)]
    assert "<option value='sms'>text message</option>" in add_select
    assert "<option value='email'>" not in add_select


def test_watchers_url_redirects_to_settings(populated):
    status, target = _get(populated, "/watchers")
    assert (status, target) == (301, "/settings")


def test_unknown_path_404s(conn):
    status, _ = _get(conn, "/nope")
    assert status == 404


def test_alerts_page_offers_ack_form_and_shows_ack_state(populated):
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    store.record_alert(conn, "1", Event(
        type=AlertType.GRADE_CHANGED, student_agu="1", course_title="Math",
        detail="Math: “Fractions Quiz” graded: 8/10"))
    status, html = _get(conn, "/alerts")
    assert "action='/ack'" in html and "<option>Mom</option>" in html

    alert_id = conn.execute("SELECT id FROM alerts").fetchone()["id"]
    status, target = dashboard._handle_ack(
        conn, {"alert_id": [alert_id], "watcher": ["Mom"]})
    assert (status, target) == (303, "/alerts")

    _, html = _get(conn, "/alerts")
    assert "✓ Mom" in html and "action='/ack'" not in html


def test_bad_ack_is_rejected(populated):
    status, html = dashboard._handle_ack(populated, {"alert_id": ["x"], "watcher": ["Nobody"]})
    assert status == 400


def test_settings_subscription_row_preselects_current_values(populated):
    conn = populated
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "m@x.com"}})
    watchers.subscribe(conn, w, "1", ["grade_changed"], ["email"], send_at="17:00")
    _, html = _get(conn, "/settings")
    # the row is editable: current values preloaded in its controls
    assert "value='17:00'" in html
    assert " checked> grade changed" in html   # its checkbox is preselected
    # single-type group: the multiselect summary names it
    assert "<summary>grade changed</summary>" in html


def test_stylesheet_exists_and_is_linked(populated):
    from mcpsgradewatch.dashboard import _STYLE_PATH

    css = _STYLE_PATH.read_text(encoding="utf-8")
    assert ":root" in css and "--accent" in css and "--bg" in css
    _, html = _get(populated, "/")
    assert "/static/style.css" in html


def test_theme_toggle_present_and_css_supports_override(populated):
    from mcpsgradewatch.dashboard import _STYLE_PATH

    _, html = _get(populated, "/")
    assert "id='themetoggle'" in html
    assert "mcpsgradewatch-theme" in html      # localStorage key in the script
    css = _STYLE_PATH.read_text(encoding="utf-8")
    assert ':root[data-theme="dark"]' in css
    assert ':root:not([data-theme="light"])' in css


def test_history_page_includes_course_grade_changes(populated):
    conn = populated
    snap = Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="709775", title="Math <Adv>", teacher="Pat Example",
                        term="MP1", mark="A-", percent="91.00%")],
    )
    store.persist_snapshot(conn, Student(agu="1", name="Jasper P. Hays"), snap)
    status, html = _get(conn, "/history")
    assert status == 200
    assert "Course grades" in html
    assert "87.20% → 91.00%" in html
    assert "B+ → A-" in html


def test_responsive_markup_hooks(populated):
    _, html = _get(populated, "/student/1")
    assert "<tr class='head'>" in html          # hideable header rows
    assert "data-label='Due'" in html           # stacked-mode cell labels
    _, home = _get(populated, "/")
    assert home.count("<svg") >= 4              # nav icons for narrow widths
    assert "class='lbl'" in home


# ── settings write paths (POST /settings/<action>) ────────────────────


def _post(conn, action, **fields):
    """Lists become repeated form values (e.g. the type multiselect)."""
    return dashboard._handle_settings_post(
        conn, action,
        {k: (list(v) if isinstance(v, (list, tuple)) else [v])
         for k, v in fields.items()})


def _ok(result):
    """Assert a success redirect (?ok= toast message); return the target."""
    status, target = result
    assert status == 303 and target.startswith("/settings?ok=")
    return target


def test_settings_add_and_remove_watcher(populated):
    conn = populated
    target = _ok(_post(conn, "watcher-add", name="Mom", kind="guardian",
                       channel="email", to="mom@example.com"))
    w = watchers.get_watcher(conn, "Mom")
    assert w.channels == {"email": {"to": "mom@example.com"}}
    # the redirect names the new rows so the client can animate them in
    assert f"new=row-w-{w.id},row-chadd-{w.id}" in target
    _ok(_post(conn, "watcher-remove", name="Mom"))
    assert watchers.list_watchers(conn) == []


def test_settings_add_watcher_without_channel(populated):
    _ok(_post(populated, "watcher-add", name="Dad", kind="guardian",
              channel="", to=""))
    assert watchers.get_watcher(populated, "Dad").channels == {}


def test_settings_channel_add_update_and_remove(populated):
    conn = populated
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    target = _ok(_post(conn, "channel", watcher="Mom", channel="ntfy",
                       to="topic-1"))
    assert f"new=row-ch-{w.id}-ntfy" in target        # an add animates in…
    assert watchers.get_watcher(conn, "Mom").channels == {"ntfy": {"topic": "topic-1"}}
    target = _ok(_post(conn, "channel", watcher="Mom", channel="ntfy",
                       to="topic-2"))
    assert "new=" not in target                       # …an update doesn't
    assert watchers.get_watcher(conn, "Mom").channels == {"ntfy": {"topic": "topic-2"}}
    _ok(_post(conn, "channel-remove", watcher="Mom", channel="ntfy"))
    assert watchers.get_watcher(conn, "Mom").channels == {}


def test_settings_channel_without_address_is_banner(populated):
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    status, target = _post(conn, "channel", watcher="Mom", channel="ntfy", to="")
    assert status == 303 and "err=" in target
    assert watchers.get_watcher(conn, "Mom").channels == {}


def test_settings_subscribe_and_unsubscribe(populated):
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    target = _ok(_post(conn, "subscribe", watcher="Mom", student="1",
                       type="grade_changed", channel="*", at="17:00"))
    (sub,) = watchers.list_subscriptions(conn)
    assert (sub.alert_type, sub.channel, sub.send_at) == ("grade_changed", "*", "17:00")
    assert f"new=row-sub-{sub.id}" in target
    _ok(_post(conn, "unsubscribe", ids=sub.id))
    assert watchers.list_subscriptions(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM watcher_student").fetchone()[0] == 0


def test_settings_error_redirects_with_message_and_renders_banner(populated):
    conn = populated
    status, target = _post(conn, "watcher-add", name="", kind="guardian",
                           channel="", to="")
    assert status == 303 and target.startswith("/settings?err=")
    status, html = _get(conn, target)
    assert status == 200
    assert "class='banner bad'" in html and "needs a name" in html


def test_settings_duplicate_watcher_is_a_banner_not_a_crash(populated):
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    status, target = _post(conn, "watcher-add", name="mom", kind="guardian",
                           channel="", to="")
    assert status == 303 and "err=" in target


def test_settings_unknown_action_404s(populated):
    status, _ = _post(populated, "nope")
    assert status == 404


def test_settings_subscribe_all_students(populated):
    conn = populated
    store.persist_snapshot(
        conn, Student(agu="2", name="Lilou Hays", school="Example ES",
                      initials="L.H."), Snapshot(student_agu="2"))
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    _ok(_post(conn, "subscribe", watcher="Mom", student="*",
              type="*", channel="*", at=""))
    subs = watchers.list_subscriptions(conn)
    assert sorted(s.student_name for s in subs) == ["Jasper P. Hays", "Lilou Hays"]
    assert all(s.alert_type == "*" and s.channel == "*" for s in subs)


def test_settings_subscription_update_in_place(populated):
    conn = populated
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    watchers.subscribe(conn, w, "1")
    (sub,) = watchers.list_subscriptions(conn)
    _ok(_post(conn, "subscription-update", ids=sub.id,
              type=["grade_changed", "grade_drop"], channel="email", at="17:00"))
    subs = watchers.list_subscriptions(conn)
    assert sorted(s.alert_type for s in subs) == ["grade_changed", "grade_drop"]
    assert all((s.channel, s.send_at) == ("email", "17:00") for s in subs)
    # …deselecting one type deletes its row; back to wildcard collapses to one
    _ok(_post(conn, "subscription-update", ids=",".join(s.id for s in subs),
              type="*", channel="*", at=""))
    (sub,) = watchers.list_subscriptions(conn)
    assert (sub.alert_type, sub.channel, sub.send_at) == ("*", "*", None)


def test_settings_subscription_update_duplicate_is_banner(populated):
    conn = populated
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    watchers.subscribe(conn, w, "1", ["grade_changed"])
    watchers.subscribe(conn, w, "1", ["grade_drop"])
    target = next(s for s in watchers.list_subscriptions(conn)
                  if s.alert_type == "grade_drop")
    status, redirect = _post(conn, "subscription-update", ids=target.id,
                             type="grade_changed", channel="*", at="")
    assert status == 303 and "err=" in redirect
    assert "identical" in redirect


def test_settings_success_renders_toast(populated):
    watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN)
    status, html = _get(populated, "/settings?ok=Channel+removed")
    assert status == 200
    assert "class='toast'" in html and "Channel removed" in html
    # errors stay a banner, not a toast
    _, html = _get(populated, "/settings")
    assert "class='toast'" not in html


def test_settings_rows_carry_ids_and_gated_update_buttons(populated):
    conn = populated
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "m@x.com"}})
    watchers.subscribe(conn, w, "1")
    (sub,) = watchers.list_subscriptions(conn)
    _, html = _get(conn, "/settings")
    assert f"id='row-w-{w.id}'" in html
    assert f"id='row-ch-{w.id}-email'" in html
    assert f"id='row-sub-{sub.id}'" in html
    # watcher-remove form names its row group so channels animate out with it
    assert f"data-group='{w.id}'" in html
    # Update buttons are class 'upd' (hidden until app.js marks the form dirty)
    assert "<button class='upd'>Update</button>" in html


def test_app_js_exists_and_is_linked(populated):
    from mcpsgradewatch.dashboard import _APPJS_PATH

    js = _APPJS_PATH.read_text(encoding="utf-8")
    assert "toast" in js and "dirty" in js and "prefers-reduced-motion" in js
    # fetch-and-swap (no page reload) and the removal confirmation dialog
    assert "settings-main" in js and "fetch(" in js
    assert ">Cancel<" in js and ">Remove<" in js and "last subscription" in js
    _, html = _get(populated, "/")
    assert "/static/app.js" in html


def test_settings_subscribe_form_defaults_to_4pm_digest_with_urgent(populated):
    watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN)
    _, html = _get(populated, "/settings")
    form = html[html.index("action='/settings/subscribe'"):]
    form = form[:form.index("</form>")]
    assert "value='16:00'" in form
    assert "name='urgent' checked" in form


def test_settings_subscribe_and_update_carry_urgent_flag(populated):
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    _ok(_post(conn, "subscribe", watcher="Mom", student="1",
              type="*", channel="*", at="16:00", urgent="on"))
    (sub,) = watchers.list_subscriptions(conn)
    assert sub.urgent_now and sub.send_at == "16:00"
    # unchecking the box on the row clears the flag
    _ok(_post(conn, "subscription-update", ids=sub.id, type="*",
              channel="*", at="16:00"))
    (sub,) = watchers.list_subscriptions(conn)
    assert not sub.urgent_now


def test_settings_page_has_swappable_region(populated):
    _, html = _get(populated, "/settings")
    assert "id='settings-main'" in html
    # the swap region wraps the cards so a fetch-based post can replace it
    assert html.index("id='settings-main'") < html.index("Watchers")


def test_channel_inputs_dodge_street_address_autofill(populated):
    watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN,
                         {"email": {"to": "m@x.com"}, "sms": {"to": "5551234567@vtext.com"}})
    _, html = _get(populated, "/settings")
    # never name='address' (browsers autofill it as a street address)
    assert "name='address'" not in html
    assert "placeholder='Address'" not in html
    # email rows invite email autofill; sms rows (carrier gateways) don't
    assert "autocomplete='email'" in html
    assert "autocomplete='off'" in html


def test_sms_address_must_be_a_gateway_not_a_phone_number(populated):
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    status, target = _post(conn, "channel", watcher="Mom", channel="sms",
                           to="3015551234")
    assert status == 303 and "err=" in target
    assert "vtext.com" in target        # the error teaches the gateway format
    assert watchers.get_watcher(conn, "Mom").channels == {}
    # a proper gateway address goes through
    _ok(_post(conn, "channel", watcher="Mom", channel="sms",
              to="3015551234@vtext.com"))
    assert watchers.get_watcher(conn, "Mom").channels == {
        "sms": {"to": "3015551234@vtext.com"}}


def test_email_address_must_look_like_email(populated):
    conn = populated
    status, target = _post(conn, "watcher-add", name="Dad", kind="guardian",
                           channel="email", to="not-an-address")
    assert status == 303 and "err=" in target
    assert watchers.get_watcher(conn, "Dad") is None
