"""The Settings page: the Display card (household settings the app keeps
itself) and watcher/subscription CRUD, all plain HTML forms (the dashboard's
only write paths — see ``server`` for the handlers)."""
from __future__ import annotations

from html import escape

from .. import __version__, updates
from ..settings import SCORE_CUTOFF_DEFAULT, SCORE_CUTOFF_MAX

from .render import (
    _REPO_URL,
    _page,
)

def _type_multiselect(fid, selected, name: str = "type") -> str:
    """The alert-types control: a checkbox dropdown (<details> popover — the
    browser handles open/close; app.js keeps the summary label fresh and
    makes 'all alerts' exclusive). ``fid`` binds the controls to a form
    elsewhere; None means the checkboxes sit inside their form already.
    ``name`` is the field name — a section form prefixes it per row."""
    from ..models import AlertType

    sel = set(selected)
    if not sel or "*" in sel:
        sel = {"*"}
    opts = [("*", "all alerts")] + [
        (t.value, t.value.replace("_", " ")) for t in AlertType]
    if "*" in sel:
        label = "all alerts"
    elif len(sel) == 1:
        label = next(iter(sel)).replace("_", " ")
    else:
        label = f"{len(sel)} types"
    form_attr = f" form='{escape(fid)}'" if fid else ""
    boxes = "".join(
        f"<label><input type='checkbox' name='{escape(name)}' value='{escape(v)}'"
        f"{form_attr}{' checked' if v in sel else ''}> {escape(lab)}</label>"
        for v, lab in opts)
    return (f"<details class='msel'>"
            f"<summary aria-label='Alert types: {escape(label)}'>"
            f"{escape(label)}</summary>"
            f"<div class='msel-list' role='group' "
            f"aria-label='Alert types'>{boxes}</div></details>")


def _section_save(fid: str, action: str, label: str) -> str:
    """One Save / Discard pair for a whole manage table. Hidden until app.js
    marks the form dirty (a field differs from what the server rendered);
    Discard is a native reset, so every bound field snaps back at once."""
    return (f"<form id='{fid}' method='post' action='{action}' class='sectionform'>"
            f"<span class='small'>Unsaved changes</span>"
            f"<button aria-label='{escape(label)}'>Save changes</button>"
            f"<button type='reset' class='ghost'>Discard</button></form>")


def _display_card(display: dict) -> str:
    """The Display card: how the pages look — the first setting the app
    keeps itself (0.3.0). One field, the score-tint cutoff, bound to its own
    section Save/Discard bar like every other table on the page. Household-
    wide for now; nothing alerts on it."""
    cutoff = int(display.get("score_cutoff") or 0)
    return (
        "<div class='card tablecard' id='display'><h2>Display</h2>"
        "<div class='field'>"
        "<label for='score-cutoff'>Tint scores below</label>"
        "<input id='score-cutoff' type='number' name='score_cutoff' "
        f"min='0' max='{SCORE_CUTOFF_MAX}' step='1' inputmode='numeric' "
        f"value='{cutoff}' form='display-save' autocomplete='off' "
        "aria-describedby='score-cutoff-help'>"
        "<span class='unit'>%</span></div>"
        "<p class='small' id='score-cutoff-help'>Graded assignments under this "
        "percent are tinted on the student pages. 70 is a C on the MCPS "
        "scale. Nothing alerts on it. 0 (or blank) turns the tint off.</p>"
        + _section_save("display-save", "/settings/display-save",
                        "Save the display settings")
        + "</div>")


def _options(pairs, selected="") -> str:
    """``<option>`` list from (value, label) pairs."""
    return "".join(
        f"<option value='{escape(v)}'{' selected' if v == selected else ''}>"
        f"{escape(label)}</option>"
        for v, label in pairs)


def render_settings(watcher_list, subscriptions, students=(),
                    error="", notice="", installed: str | None = None,
                    display: dict | None = None) -> str:
    """The Settings page: the Display card, then full watcher/subscription
    CRUD, all as plain HTML forms. These are the dashboard's only write
    paths; they carry no auth of their own — the bind address is the access
    control. Env-owned config (poll cadence, thresholds) is deliberately
    absent: if it can't be changed from here, it isn't shown here.
    ``display`` is the household display settings dict (``settings.display``).
    """
    from .. import notify

    d_card = _display_card(display or {"score_cutoff": SCORE_CUTOFF_DEFAULT})

    watcher_opts = [(w.name, w.name) for w in watcher_list]
    # The web UI offers email only (owner's call 2026-09-02: text message via
    # carrier gateways withdrawn — they're shut down or being retired). Other
    # channels stay CLI territory; pre-0.1.5 sms rows still render and work.
    channel_opts = [("email", "email")]
    channel_label = {"email": "email", "sms": "text message"}

    if watcher_list:
        # One row per watcher, then one row per channel under it (each
        # editable/removable in place), then an add-channel row for whatever
        # channels the watcher doesn't have yet. Every address field binds
        # (via form=) to ONE section form after the table — one Save for
        # the whole table, so editing three rows and saving keeps all
        # three (owner's call 2026-09-05: per-row Update lost the others).
        # Test/remove keep tiny row forms of their own: they read only the
        # hidden ids.
        w_rows = []
        ch_n = 0                     # section-form row index (r0-…, r1-…)
        for w in watcher_list:
            w_rows.append(
                f"<tr id='row-w-{escape(w.id)}' data-w='{escape(w.id)}'>"
                f"<td><strong>{escape(w.name)}</strong> "
                f"<span class='small'>{escape(w.kind.value)}</span></td>"
                f"<td></td>"
                f"<td data-label='Actions'>"
                f"<form method='post' action='/settings/watcher-remove' "
                f"class='rowform' data-group='{escape(w.id)}'>"
                f"<input type='hidden' name='name' value='{escape(w.name)}'>"
                f"<button class='ghost' "
                f"aria-label='Remove watcher {escape(w.name)}'>"
                f"remove</button></form>"
                f"</td></tr>")
            for cname, addr in w.channels.items():
                fid = f"ch-{escape(w.id)}-{escape(cname)}"
                hidden = (f"<input type='hidden' name='watcher' value='{escape(w.name)}'>"
                          f"<input type='hidden' name='channel' value='{escape(cname)}'>")
                if notify.ADDRESS_KEY.get(cname) is None:   # console: no address
                    addr_cell = "—"
                    buttons = (f"<button class='ghost' aria-label='Remove "
                               f"{escape(channel_label.get(cname, cname))} channel'>"
                               f"remove</button>")
                    form = (f"<form id='{fid}' method='post' "
                            f"action='/settings/channel-remove' class='rowform'>"
                            f"{hidden}{buttons}</form>")
                else:
                    address = next(iter(addr.values()), "")
                    # name='…-to' (never 'address' — browsers autofill that
                    # as a street address). Email rows opt into email
                    # autofill; sms rows hold a carrier gateway, so no
                    # suggestions.
                    autofill = "email" if cname == "email" else "off"
                    r = f"r{ch_n}"
                    ch_n += 1
                    addr_cell = (f"<input type='hidden' name='{r}-watcher' "
                                 f"value='{escape(w.name)}' form='chan-save'>"
                                 f"<input type='hidden' name='{r}-channel' "
                                 f"value='{escape(cname)}' form='chan-save'>"
                                 f"<input name='{r}-to' form='chan-save' "
                                 f"aria-label='{escape(channel_label.get(cname, cname))} address' "
                                 f"value='{escape(address)}' "
                                 f"autocomplete='{autofill}'>")
                    form = (f"<form id='{fid}' method='post' "
                            f"action='/settings/channel-test' class='rowform'>{hidden}"
                            f"<button class='ghost' "
                            f"aria-label='Send a test "
                            f"{escape(channel_label.get(cname, cname))} to "
                            f"{escape(address)}'>"
                            f"test</button> "
                            f"<button class='ghost' "
                            f"aria-label='Remove "
                            f"{escape(channel_label.get(cname, cname))} channel' "
                            f"formaction='/settings/channel-remove'>"
                            f"remove</button></form>")
                w_rows.append(
                    f"<tr class='chrow' id='row-{fid}' data-w='{escape(w.id)}'>"
                    f"<td class='chname'>{escape(channel_label.get(cname, cname))}</td>"
                    f"<td data-label='Address'>{addr_cell}</td>"
                    f"<td data-label='Actions'>{form}</td></tr>")
            remaining = [(c, label) for c, label in channel_opts
                         if c not in w.channels]
            if remaining:
                fid = f"chadd-{escape(w.id)}"
                w_rows.append(
                    f"<tr class='chrow' id='row-{fid}' data-w='{escape(w.id)}'>"
                    f"<td class='chname'>"
                    f"<select name='channel' form='{fid}' aria-label='Channel'>"
                    f"{_options(remaining)}</select></td>"
                    f"<td data-label='Address'>"
                    f"<input name='to' form='{fid}' aria-label='Address' autocomplete='off' "
                    f"placeholder='name@example.com' "
                    f"data-tip='Email address'></td>"
                    f"<td data-label='Actions'>"
                    f"<form id='{fid}' method='post' action='/settings/channel' "
                    f"class='rowform'>"
                    f"<input type='hidden' name='watcher' value='{escape(w.name)}'>"
                    f"<button>Add channel</button></form></td></tr>")
        w_body = ("<table class='manage' aria-label='Watchers'><tr class='head'><th scope='col'>Watcher</th>"
                  "<th scope='col'>Address</th><th scope='col'><span class='vh'>Actions</span></th></tr>" + "".join(w_rows) + "</table>"
                  + _section_save("chan-save", "/settings/channels-save",
                                  "Save the edited addresses"))
    else:
        w_body = "<p class='small'>None yet — add the first watcher above.</p>"

    add_form = (
        "<form method='post' action='/settings/watcher-add' class='edit'>"
        "<span class='formtitle'>Add</span>"
        "<input name='name' aria-label='Watcher name' placeholder='Name' required autocomplete='off'>"
        f"<select name='kind' aria-label='Watcher role'>{_options([('guardian', 'guardian'), ('student', 'student')])}</select>"
        f"<select name='channel' aria-label='Channel'>{_options([('', 'no channel yet')] + channel_opts)}</select>"
        "<input name='to' aria-label='Address' autocomplete='off' "
        "placeholder='name@example.com' "
        "data-tip='Email address'>"
        "<button>Add watcher</button></form>")
    w_card = ("<div class='card tablecard'><h2>Watchers</h2>"
              + add_form + w_body + "</div>")

    if subscriptions:
        # A displayed row is a GROUP of single-type subscription rows sharing
        # (watcher, student, channel, delivery, urgent); the Alerts cell is a
        # multiselect over the group's types. A <form> can't span table
        # cells, so every row's controls bind (via form=) to ONE section
        # form after the table, with per-row field names (r0-type, r0-at…):
        # one Save for the whole table, so turning "urgent now" off on five
        # rows and saving keeps all five (owner's call 2026-09-05). Remove
        # keeps a tiny row form of its own — it only reads the hidden ids.
        groups: dict = {}
        for s in subscriptions:
            key = (s.watcher_id, s.student_id, s.channel, s.send_at, s.urgent_now)
            groups.setdefault(key, []).append(s)
        s_rows = []
        urgent_tip = ("Send urgent alerts (missing, due soon, grade drop) "
                      "immediately instead of waiting for the digest")
        for n, group in enumerate(groups.values()):
            first = group[0]
            fid = f"sub-{escape(first.id)}"
            ids = ",".join(s.id for s in group)
            r = f"r{n}"
            s_rows.append(
                f"<tr id='row-{fid}'>"
                f"<td>{escape(first.watcher_name)} ⇒ {escape(first.student_name)}"
                f"<input type='hidden' name='{r}-ids' value='{escape(ids)}' "
                f"form='subs-save'></td>"
                f"<td data-label='Alerts'>"
                f"{_type_multiselect('subs-save', [s.alert_type for s in group], name=f'{r}-type')}</td>"
                f"<td data-label='Via'><select name='{r}-channel' form='subs-save' "
                f"aria-label='Delivery channel'>"
                f"{_options([('*', 'all configured')] + channel_opts, selected=first.channel)}"
                f"</select></td>"
                f"<td data-label='Delivery'><input type='time' name='{r}-at' form='subs-save' "
                f"aria-label='Daily digest time' "
                f"value='{escape(first.send_at or '')}' "
                f"data-tip='Daily digest time — blank for immediate delivery'> "
                f"<label class='urgent' data-tip='{escape(urgent_tip)}'>"
                f"<input type='checkbox' name='{r}-urgent' form='subs-save'"
                f"{' checked' if first.urgent_now else ''}> urgent now</label></td>"
                f"<td data-label='Actions'>"
                f"<form id='{fid}' method='post' action='/settings/unsubscribe' "
                f"class='rowform'>"
                f"<input type='hidden' name='ids' value='{escape(ids)}'>"
                f"<button class='ghost' "
                f"aria-label='Unsubscribe {escape(first.watcher_name)} "
                f"from {escape(first.student_name)}'>"
                f"remove</button></form></td></tr>")
        s_body = ("<table class='manage' aria-label='Subscriptions'><tr class='head'><th scope='col'>Watcher ⇒ Student</th>"
                  "<th scope='col'>Alerts</th><th scope='col'>Via</th><th scope='col'>Delivery</th><th scope='col'><span class='vh'>Actions</span></th></tr>"
                  + "".join(s_rows) + "</table>"
                  + _section_save("subs-save", "/settings/subscriptions-save",
                                  "Save the edited subscriptions"))
    else:
        s_body = "<p class='small'>No subscriptions yet.</p>"
    if watcher_list and students:
        student_opts = [("*", "all students")] + [
            (s["agu"], s["name"]) for s in students]
        s_form = (
            "<form method='post' action='/settings/subscribe' class='edit'>"
            "<span class='formtitle'>Add</span>"
            f"<select name='watcher' aria-label='Watcher'>{_options(watcher_opts)}</select>"
            "<span class='small'>gets</span>"
            f"{_type_multiselect(None, ['*'])}"
            "<span class='small'>for</span>"
            f"<select name='student' aria-label='Student'>{_options(student_opts)}</select>"
            "<span class='small'>via</span>"
            f"<select name='channel' aria-label='Delivery channel'>{_options([('*', 'all channels')] + channel_opts)}</select>"
            "<input type='time' name='at' value='16:00' aria-label='Daily digest time' "
            "data-tip='Daily digest time — clear for immediate delivery'>"
            "<label class='urgent' data-tip='Send urgent alerts (missing, due soon, "
            "grade drop) immediately instead of waiting for the digest'>"
            "<input type='checkbox' name='urgent' checked> urgent now</label>"
            "<button>Subscribe</button></form>")
    else:
        s_form = ("<p class='small'>Add a watcher first.</p>" if not watcher_list
                  else "<p class='small'>Students appear after the first run.</p>")
    s_card = ("<div class='card tablecard'><h2>Subscriptions</h2>"
              + s_form + s_body + "</div>")

    banner = (f"<div class='banner bad' role='alert'>{escape(error)}</div>"
              if error else "")
    toast = (f"<div class='toast' role='status' aria-live='polite'>"
             f"{escape(notice)}</div>" if notice else "")
    # The one place the app credits itself: quiet, at the very bottom of the
    # page people already come to for housekeeping. Outside #settings-main so
    # fetch-swaps never repaint it.
    # Version first: the one thing a bug report needs and a parent can't
    # otherwise answer. Check for updates is a POST so it only ever runs on
    # a click — the README promises no phone-home, and this keeps it true.
    ext = "target='_blank' rel='noopener noreferrer'"
    # Upgraded on disk but not restarted: the footer is the one place that
    # can say so, since it is the un-restarted process itself talking.
    pending = ""
    if updates.restart_pending(installed):
        pending = (f" <span class='badge warn'>{escape(installed)} installed — "
                   f"restart to use it</span>")
    credit = (
        "<footer class='credit'>"
        f"<a href='{_REPO_URL}/releases/tag/v{__version__}' {ext}>"
        f"Last Bell {__version__}</a>{pending} · "
        f"© 2026 <a href='{_REPO_URL}' {ext}>Chris Hays</a> · "
        f"<a href='{_REPO_URL}/blob/main/LICENSE' {ext}>MIT license</a> · "
        f"<a href='{_REPO_URL}/releases' {ext}>What's new</a> · "
        f"<a href='{_REPO_URL}/issues' {ext}>Report a problem</a> · "
        "<form method='post' action='/settings/check-updates' class='inline'>"
        "<button class='linkish' aria-label='Check for updates: asks PyPI "
        "whether a newer release exists, only when clicked'>"
        "Check for updates</button></form>"
        "</footer>")
    # settings-main is the region app.js swaps in place after a fetch-based
    # form post — banner, cards, and toast all live inside it.
    return _page("Settings", "<h1>Settings</h1><div id='settings-main'>"
                 + banner + d_card + w_card + s_card + toast + "</div>" + credit,
                 nav_students=students, path="/settings")


