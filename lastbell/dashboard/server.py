"""HTTP plumbing: routing, the settings POST handlers, and ``serve``."""
from __future__ import annotations

import sqlite3
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .. import store

from .queries import (
    _HISTORY_ALL,
    _HISTORY_LIMIT,
    alerts_last_page,
    alerts_total,
    build_student_ctx,
    fetch_alert_counts,
    fetch_alerts,
    fetch_course_history,
    fetch_courses,
    fetch_history,
    fetch_history_class_counts,
    fetch_history_field_counts,
    fetch_history_totals,
    fetch_open_counts,
    fetch_student,
    fetch_students,
)
from .render import (
    _APPJS_PATH,
    _FAVICON_PATH,
    _STYLE_PATH,
    _page,
    render_alerts,
    render_history,
    render_overview,
    render_student,
)
from .settings import (
    render_settings,
)

# ── http plumbing ─────────────────────────────────────────────────────


def _handle(conn: sqlite3.Connection, path: str) -> tuple[int, str]:
    """Route one request (path may carry a query string). Returns
    (status, html) — or, for a 301, the redirect target instead of a body."""
    from .. import watchers as watchermod

    parsed = urlparse(path)
    path, query = parsed.path, parse_qs(parsed.query)
    # Every page's nav carries the student links, so fetch once up front.
    students = fetch_students(conn)
    if path == "/":
        # The overview is "right now": only the current term's courses/counts.
        courses = {s["id"]: fetch_courses(conn, s["id"], term=s["current_term"])
                   for s in students}
        counts = {s["id"]: fetch_open_counts(conn, s["id"], term=s["current_term"])
                  for s in students}
        return 200, render_overview(students, courses, counts, store.last_poll(conn))
    if path.startswith("/student/"):
        agu = path[len("/student/"):]
        student = fetch_student(conn, agu)
        if student is None:
            return 404, _page(
                "Not found",
                "<h1>No student by that id</h1>"
                "<p>They may have been removed, or the link is stale. "
                "<a href='/'>Back to the overview</a> — every current student "
                "is listed there.</p>",
                nav_students=students)
        ctx = build_student_ctx(conn, student,
                                (query.get("view") or [""])[0],
                                (query.get("course") or [""])[0],
                                (query.get("status") or [""])[0],
                                strip_open=(query.get("strip") or [""])[0] == "open")
        return 200, render_student(student, ctx, nav_students=students)
    if path == "/alerts":
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
        except ValueError:
            page = 1
        alert_type = (query.get("type") or [""])[0]
        counts = fetch_alert_counts(conn)
        total = alerts_total(counts, alert_type)
        last = alerts_last_page(total)
        if page > last:
            # Past the end (stale bookmark, hand-edited URL): land on the
            # last real page rather than an empty table, URL kept truthful.
            q = ([f"type={quote(alert_type)}"] if alert_type else []) + (
                [f"page={last}"] if last > 1 else [])
            return 301, "/alerts" + ("?" + "&".join(q) if q else "")
        return 200, render_alerts(fetch_alerts(conn, page, alert_type), counts,
                                  nav_students=students, page=page,
                                  alert_type=alert_type, total=total)
    if path == "/history":
        h_course = (query.get("course") or [""])[0]
        h_field = (query.get("field") or [""])[0]
        show_all = (query.get("all") or [""])[0] == "1"
        limit = _HISTORY_ALL if show_all else _HISTORY_LIMIT
        return 200, render_history(
            fetch_history(conn, limit, course=h_course, field=h_field),
            fetch_course_history(conn, limit, course=h_course, field=h_field),
            fetch_history_class_counts(conn), fetch_history_field_counts(conn),
            course=h_course, field=h_field, nav_students=students,
            totals=fetch_history_totals(conn, course=h_course, field=h_field),
            show_all=show_all)
    if path == "/settings":
        return 200, render_settings(watchermod.list_watchers(conn),
                                    watchermod.list_subscriptions(conn),
                                    students,
                                    error=(query.get("err") or [""])[0],
                                    notice=(query.get("ok") or [""])[0])
    if path == "/watchers":   # pre-Settings URL; keep old bookmarks working
        return 301, "/settings"
    return 404, _page(
        "Not found",
        "<h1>No such page</h1>"
        "<p>That address doesn't go anywhere. "
        "<a href='/'>Back to the overview</a>.</p>",
        nav_students=students)


def _handle_settings_post(conn: sqlite3.Connection, action: str,
                          form: dict) -> tuple[int, str]:
    """POST /settings/<action> — the Settings page's write paths. They carry
    no auth of their own: the bind address is the access control. Always
    redirects back to /settings; a validation failure carries the message in
    ?err= and renders as a banner, with the tables (and the browser's
    back-button form state) intact."""
    from .. import notify
    from .. import watchers as watchermod
    from ..models import WatcherKind

    def val(key: str) -> str:
        return (form.get(key) or [""])[0].strip()

    def vals(key: str) -> list[str]:
        return [v.strip() for v in (form.get(key) or []) if v.strip()]

    def channel_update() -> dict:
        name = val("channel")
        if name not in notify.ADDRESS_KEY:
            raise watchermod.WatcherError(
                f"unknown channel {name!r} (valid: {', '.join(notify.CHANNEL_NAMES)})")
        key = notify.ADDRESS_KEY[name]
        if key is None:                       # console needs no address
            return {name: {}}
        address = val("to")
        if address:
            address = notify.validate_address(name, address)
        return {name: {key: address} if address else None}

    def done(message: str, new_rows: tuple = ()) -> tuple[int, str]:
        """Success redirect: ?ok= becomes the toast, ?new= names the row
        elements the client animates in."""
        target = "/settings?ok=" + quote(message)
        if new_rows:
            target += "&new=" + ",".join(new_rows)
        return 303, target

    # Toasts name who and what changed ("Removed Mom's email
    # (mom@example.com)"), not just the verb — the reader shouldn't have to
    # diff the table to learn what happened.
    chlabel = {"email": "email", "sms": "text message"}

    def _addr(update: dict, cname: str) -> str:
        return next(iter((update.get(cname) or {}).values()), "")

    try:
        if action == "watcher-add":
            name = val("name")
            if not name:
                raise watchermod.WatcherError("the watcher needs a name")
            channels = {}
            if val("channel"):
                channels = channel_update()
                if None in channels.values():
                    raise watchermod.WatcherError(
                        f"the {val('channel')} channel needs an address")
            w = watchermod.add_watcher(conn, name, WatcherKind(val("kind")), channels)
            msg = f"Added watcher {w.name}"
            if channels:
                cname = next(iter(channels))
                addr = _addr(channels, cname)
                msg += f" with {chlabel.get(cname, cname)}"
                msg += f" {addr}" if addr else ""
            return done(msg, (f"row-w-{w.id}", f"row-chadd-{w.id}"))
        if action == "watcher-remove":
            w = watchermod.get_watcher(conn, val("name"))
            watchermod.remove_watcher(conn, val("name"))
            return done(f"Removed watcher {w.name if w else val('name')}")
        if action == "channel":         # add or update; removal is its own action
            update = channel_update()
            if None in update.values():
                raise watchermod.WatcherError(
                    f"the {val('channel')} channel needs an address")
            existing = watchermod.require_watcher(conn, val("watcher"))
            cname = next(iter(update))
            watchermod.set_channels(conn, val("watcher"), update)
            label, addr = chlabel.get(cname, cname), _addr(update, cname)
            if cname in existing.channels:
                return done(f"Updated {existing.name}'s {label}"
                            + (f": {addr}" if addr else ""))
            return done(f"Added {label} for {existing.name}"
                        + (f": {addr}" if addr else ""),
                        (f"row-ch-{existing.id}-{cname}",))
        if action == "channel-test":
            # Tests the *saved* address: what the next alert would use. An
            # edited-but-not-updated field is the Update button's business.
            w = watchermod.require_watcher(conn, val("watcher"))
            cname = val("channel")
            if cname not in w.channels:
                raise watchermod.WatcherError(
                    f"{w.name} has no {chlabel.get(cname, cname)} channel")
            address = w.channels[cname]
            where = next(iter(address.values()), "") if address else ""
            try:
                notify.send_test(cname, address)
            except Exception as e:
                raise watchermod.WatcherError(
                    f"Test {chlabel.get(cname, cname)} to {where} failed: {e}") from e
            return done(f"Sent a test {chlabel.get(cname, cname)} to {where} — "
                        f"check that it arrived")
        if action == "check-updates":
            from .. import updates

            try:
                status, latest = updates.check()
            except updates.UpdateCheckError as e:
                raise watchermod.WatcherError(f"Update check failed: {e}") from e
            return done(updates.describe(status, latest))
        if action == "channel-remove":
            w = watchermod.require_watcher(conn, val("watcher"))
            cname = val("channel")
            old = next(iter((w.channels.get(cname) or {}).values()), "")
            watchermod.set_channels(conn, val("watcher"), {cname: None})
            return done(f"Removed {w.name}'s {chlabel.get(cname, cname)}"
                        + (f" ({old})" if old else ""))
        if action == "subscribe":
            w = watchermod.require_watcher(conn, val("watcher"))
            if val("student") == "*":     # one step: every student at once
                targets = [s["id"] for s in fetch_students(conn)]
                target_desc = "all students"
            else:
                srow = watchermod.resolve_student(conn, val("student"))
                targets = [srow["id"]]
                target_desc = srow["name"]
            sub_types, channel = vals("type"), val("channel")
            added: list[str] = []
            for student_id in targets:
                added += watchermod.subscribe(
                    conn, w, student_id,
                    None if not sub_types or "*" in sub_types else sub_types,
                    None if channel in ("", "*") else [channel],
                    val("at") or None,
                    urgent_now=bool(val("urgent")))
            if not added:
                return done(f"{w.name} is already subscribed to {target_desc}")
            n = len(added)
            return done(f"Subscribed {w.name} to {target_desc}"
                        + (f" — {n} subscriptions" if n > 1 else ""),
                        tuple(f"row-sub-{i}" for i in added))
        if action == "subscription-update":
            ids = [i for i in val("ids").split(",") if i]
            named = next((s for s in watchermod.list_subscriptions(conn)
                          if s.id in ids), None)
            watchermod.set_subscription_group(
                conn, ids, vals("type"), val("channel") or "*",
                val("at") or None, urgent_now=bool(val("urgent")))
            return done(f"Updated {named.watcher_name}'s subscription for "
                        f"{named.student_name}" if named
                        else "Subscription updated")
        if action == "unsubscribe":
            ids = [i for i in val("ids").split(",") if i]
            if not ids:
                raise watchermod.WatcherError("no subscription selected")
            named = next((s for s in watchermod.list_subscriptions(conn)
                          if s.id in ids), None)
            for sub_id in ids:
                watchermod.remove_subscription(conn, sub_id)
            return done(f"Unsubscribed {named.watcher_name} from "
                        f"{named.student_name}" if named
                        else "Subscription removed")
        return 404, _page(
            "Not found",
            "<h1>No such action</h1>"
            "<p>That settings action doesn't exist — the page may be out of "
            "date. <a href='/settings'>Back to Settings</a>.</p>",
            nav_students=fetch_students(conn))
    except (watchermod.WatcherError, ValueError) as e:
        return 303, "/settings?err=" + quote(str(e))


def same_origin(headers) -> bool:
    """Is this POST from the dashboard's own pages? Binding to localhost
    doesn't stop the reader's *browser* from being pointed here by any site
    they visit — a cross-site form post could add a stranger's address as a
    watcher on every student. Browsers send ``Origin`` on every POST (and
    ``Sec-Fetch-Site`` on modern ones); a request with neither is not a
    browser and is let through, as before."""
    site = headers.get("Sec-Fetch-Site")
    if site and site not in ("same-origin", "none"):
        return False
    origin = headers.get("Origin") or headers.get("Referer")
    if not origin:
        return True
    return urlparse(origin).netloc.lower() == (headers.get("Host") or "").lower()


def serve(db_path: Path, host: str, port: int) -> None:
    from .. import store

    # Apply any pending schema migrations up front — the per-request
    # connections below assume current columns.
    boot = store.connect(db_path)
    store.ensure_schema(boot)
    boot.close()

    class Handler(BaseHTTPRequestHandler):
        _STATIC = {
            "/static/style.css": (_STYLE_PATH, "text/css; charset=utf-8"),
            "/static/app.js": (_APPJS_PATH, "text/javascript; charset=utf-8"),
            "/static/favicon.png": (_FAVICON_PATH, "image/png"),
            # Browsers ask for this on their own; answer it rather than 404.
            "/favicon.ico": (_FAVICON_PATH, "image/png"),
        }

        def do_GET(self) -> None:  # noqa: N802 (stdlib name)
            static = self._STATIC.get(urlparse(self.path).path)
            if static:
                payload = static[0].read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", static[1])
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            # A connection per request: cheap for SQLite, and thread-safe by
            # construction under ThreadingHTTPServer.
            conn = store.connect(db_path)
            try:
                status, html = _handle(conn, self.path)
            except sqlite3.OperationalError as e:
                status, html = 500, _page(
                    "Error",
                    "<h1>Something went wrong</h1>"
                    "<p>The dashboard couldn't read its database just now — "
                    "usually momentary (a poll or backup holding the file). "
                    "Refresh to try again; if it keeps happening, check that "
                    "the database file exists and is writable.</p>"
                    f"<p class='small'>Detail: {escape(str(e))}</p>")
            finally:
                conn.close()
            if status == 301:   # html is the redirect target, not a body
                self.send_response(301)
                self.send_header("Location", html)
                self.end_headers()
                return
            payload = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802 (stdlib name)
            path = urlparse(self.path).path
            if not path.startswith("/settings/"):
                self.send_error(404)
                return
            if not same_origin(self.headers):
                self.send_error(403, "cross-site request refused")
                return
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            conn = store.connect(db_path)
            try:
                status, result = _handle_settings_post(
                    conn, path[len("/settings/"):], form)
            finally:
                conn.close()
            if status == 303:
                self.send_response(303)
                self.send_header("Location", result)
                self.end_headers()
                return
            payload = result.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args) -> None:
            pass  # keep the terminal quiet; this is a background convenience

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"dashboard: http://{host}:{port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
