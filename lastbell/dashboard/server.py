"""HTTP plumbing: routing, the settings POST handlers, and ``serve``."""
from __future__ import annotations

import hmac
import ipaddress
import os
import socket
import re
import sqlite3
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from .. import store
from ..service import in_container

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
        from .. import health

        return 200, render_overview(students, courses, counts, store.last_poll(conn),
                                    failure_note=health.dashboard_note(health.current(conn)))
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
        # A filter is one of the types actually present, or nothing: the
        # value is reflected into the page, so it is never free text.
        if alert_type and alert_type not in {c["type"] for c in counts}:
            alert_type = ""
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
        from .. import updates

        return 200, render_settings(watchermod.list_watchers(conn),
                                    watchermod.list_subscriptions(conn),
                                    students,
                                    error=(query.get("err") or [""])[0],
                                    notice=(query.get("ok") or [""])[0],
                                    installed=updates.installed_version())
    if path == "/watchers":   # pre-Settings URL; keep old bookmarks working
        return 301, "/settings"
    return 404, _page(
        "Not found",
        "<h1>No such page</h1>"
        "<p>That address doesn't go anywhere. "
        "<a href='/'>Back to the overview</a>.</p>",
        nav_students=students)


def _row_prefixes(form: dict) -> list[str]:
    """The per-row field prefixes a section form posted (r0, r1, …), in row
    order: a manage table's rows bind to one form with prefixed names."""
    seen = sorted({int(m.group(1)) for k in form
                   for m in [re.match(r"r(\d+)-", k)] if m})
    return [f"r{n}" for n in seen]


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
            return done(updates.describe(status, latest, updates.installed_version()))
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
        if action == "channels-save":
            # Every address row of the Watchers table in one post; only the
            # rows whose address differs from what's saved are written, so
            # an untouched row can't fail validation or churn.
            changed = []
            for r in _row_prefixes(form):
                w = watchermod.require_watcher(conn, val(f"{r}-watcher"))
                cname = val(f"{r}-channel")
                key = notify.ADDRESS_KEY.get(cname)
                if key is None:
                    continue
                saved = (w.channels.get(cname) or {}).get(key, "")
                to = val(f"{r}-to")
                if to == saved:
                    continue
                if not to:
                    raise watchermod.WatcherError(
                        f"{w.name}'s {chlabel.get(cname, cname)} channel needs an address")
                to = notify.validate_address(cname, to)
                watchermod.set_channels(conn, w.name, {cname: {key: to}})
                changed.append(f"{w.name}'s {chlabel.get(cname, cname)}: {to}")
            if not changed:
                return done("No changes to save")
            if len(changed) == 1:
                return done(f"Updated {changed[0]}")
            return done(f"Updated {len(changed)} addresses")
        if action == "subscriptions-save":
            # Every row of the Subscriptions table in one post; a row is
            # rewritten only when its posted values differ from what's saved
            # (rewriting churns ids, and the daily-summary sent-marker hangs
            # off the id).
            current = {s.id: s for s in watchermod.list_subscriptions(conn)}
            changed = []
            for r in _row_prefixes(form):
                ids = [i for i in val(f"{r}-ids").split(",") if i]
                subs = [current[i] for i in ids if i in current]
                if not subs:
                    continue                      # removed since the page loaded
                types = vals(f"{r}-type") or ["*"]
                if "*" in types:
                    types = ["*"]
                channel = val(f"{r}-channel") or "*"
                at = val(f"{r}-at") or None
                urgent = bool(val(f"{r}-urgent"))
                first = subs[0]
                if (sorted(set(types)) == sorted({s.alert_type for s in subs})
                        and channel == first.channel and at == first.send_at
                        and urgent == bool(first.urgent_now)):
                    continue
                watchermod.set_subscription_group(
                    conn, [s.id for s in subs], types, channel, at, urgent_now=urgent)
                changed.append(f"{first.watcher_name}'s subscription for "
                               f"{first.student_name}")
            if not changed:
                return done("No changes to save")
            if len(changed) == 1:
                return done(f"Updated {changed[0]}")
            return done(f"Updated {len(changed)} subscriptions")
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


_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
_ANY_ADDRESS = {"", "0.0.0.0", "::"}


def _host_name(host_header: str) -> str:
    """The name part of a Host header, lower-cased: port and IPv6 brackets
    stripped (``pi.local:8321`` → ``pi.local``, ``[::1]:8321`` → ``::1``)."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:h.index("]")] if "]" in h else h[1:]
    if h.count(":") == 1:          # name:port (more colons = a bare IPv6 literal)
        return h.rsplit(":", 1)[0]
    return h


def host_allowed(host_header: str | None, bind_host: str, extra: tuple = ()) -> bool:
    """Is this request addressed to a name the dashboard answers to?

    Binding to 127.0.0.1 keeps the network out, but not the reader's own
    browser: a page on any site can point a hostname it controls at
    127.0.0.1 (DNS rebinding) and then read this dashboard — full names and
    grades — as if it were its own origin, and the Origin check in
    ``same_origin`` can't tell, because Origin and Host then agree. What a
    rebound request can't fake is the Host header, which names the
    attacker's domain. So the dashboard answers only to: loopback names, the
    address it was bound to, any IP literal (an address can't be rebound),
    ``.local`` mDNS names (resolved by multicast, not public DNS, on every
    mainstream desktop and phone OS — the browser is where this attack
    runs), and hostnames the
    owner lists in ``LASTBELL_DASHBOARD_HOSTNAMES``. A request with no Host
    header at all (an HTTP/1.0 client, never a browser) is let through."""
    if not host_header:
        return True
    name = _host_name(host_header)
    if name in _LOOPBACK_NAMES:
        return True
    if bind_host and bind_host.lower() not in _ANY_ADDRESS and name == bind_host.lower():
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name.endswith(".local"):
        return True
    return name in {e.strip().lower() for e in extra if e and e.strip()}


# ── the network key ───────────────────────────────────────────────────
#
# Binding beyond loopback puts full names and the watcher list on the
# network, and the dashboard has no accounts. So: a request from the machine
# itself needs nothing (nothing changes for the default install), and a
# request from anywhere else needs the dashboard's key — a long random
# string generated once, kept in the settings file, printed as a link when
# the dashboard starts. Opening that link once sets a cookie; the browser
# is then remembered. The same idea as Jupyter's token.

COOKIE = "lastbell_key"
_LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1"}


class DashboardRefused(RuntimeError):
    """A bind the dashboard won't do without being told twice."""


def peer_is_local(ip: str) -> bool:
    """Did this request come from the machine the dashboard runs on?"""
    try:
        return ipaddress.ip_address((ip or "").split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def key_matches(presented: str | None, key: str) -> bool:
    return bool(presented) and bool(key) and hmac.compare_digest(
        presented.encode("utf-8"), key.encode("utf-8"))


def cookie_key(cookie_header: str | None) -> str:
    try:
        jar = SimpleCookie()
        jar.load(cookie_header or "")
        return jar[COOKIE].value if COOKIE in jar else ""
    except Exception:  # a malformed Cookie header is just "no cookie"
        return ""


def admitted(peer_ip: str, cookie_header: str | None, key: str) -> bool:
    """Local peers need no key; anyone else needs the cookie."""
    return peer_is_local(peer_ip) or key_matches(cookie_key(cookie_header), key)


def check_bind(host: str) -> None:
    """Refuse a bind to a public address unless LASTBELL_DASHBOARD_PUBLIC=1:
    the key crosses the wire in the clear, which is fine on a home LAN or
    Tailscale and wrong on the open internet, where a TLS proxy belongs in
    front. (0.0.0.0 can't be judged from here; it gets the warning instead.)"""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return
    if addr.is_global and os.environ.get("LASTBELL_DASHBOARD_PUBLIC") != "1":
        raise DashboardRefused(
            f"{host} is a public address, and the dashboard speaks plain HTTP: "
            f"its key would cross the internet in the clear. Put a TLS reverse "
            f"proxy in front (or Tailscale) and bind to loopback, or set "
            f"LASTBELL_DASHBOARD_PUBLIC=1 if you have really thought about it.")


def key_link(host: str, port: int, key: str) -> str:
    """The one-time link, on a name other devices can use: the bound address
    when it is a real one, else this machine's name. In a container the
    machine's name is the container ID, meaningless outside it; compose
    publishes the port on the Docker host's loopback, so that is the link."""
    if host in _LOOPBACK_BINDS or host in _ANY_ADDRESS:
        if in_container():
            host = "127.0.0.1"
        else:
            name = socket.gethostname()
            if "." not in name:
                name += ".local"
            host = name
    elif ":" in host:                      # bare IPv6 literal
        host = f"[{host}]"
    return f"http://{host}:{port}/?key={key}"


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


def serve(db_path: Path, host: str, port: int, hostnames: tuple = (),
          key: str = "") -> None:
    from .. import store

    widened = host not in _LOOPBACK_BINDS
    if widened:
        check_bind(host)
        if not key:
            raise DashboardRefused(
                "binding beyond loopback needs the dashboard key; "
                "`lastbell dashboard` supplies it (this is a programming error).")

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

        def _refuse_host(self) -> None:
            name = escape(_host_name(self.headers.get("Host") or ""))
            body = _page(
                "Refused",
                "<h1>Not answering to that name</h1>"
                f"<p>This request was addressed to <code>{name}</code>. The "
                "dashboard answers only to localhost, IP addresses, "
                "<code>.local</code> names, and the hostnames listed in "
                "<code>LASTBELL_DASHBOARD_HOSTNAMES</code> — that is what keeps "
                "a web page you happen to visit from reaching it through your "
                "own browser (DNS rebinding). If this is your own name for "
                "this machine, add it to that setting and restart the "
                "dashboard.</p>").encode("utf-8")
            self.send_response(421)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _refuse_key(self, path: str, wrong: bool) -> None:
            body = _page(
                "Key required",
                "<h1>This dashboard asks for its key</h1>"
                + ("<p class='banner bad'>That key didn't match.</p>" if wrong else "")
                + "<p>You're reaching it over the network rather than from the "
                "machine it runs on, so it wants the key it printed when it "
                "started (<code>lastbell dashboard --show-key</code> prints it "
                "again, as a link). Open that link once in this browser and "
                "you're remembered.</p>"
                f"<form method='get' action='{escape(path)}' class='keyform'>"
                "<input type='password' name='key' autocomplete='off' "
                "placeholder='paste the key' size='40'> "
                "<button type='submit'>Open</button></form>").encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _admit(self, allow_query_key: bool) -> bool:
            """True when the request may proceed. A GET carrying the right
            ``?key=`` is answered with the cookie and a redirect to the same
            URL without it (so the key doesn't sit in the address bar or the
            history), and False is returned since the response is written."""
            if admitted(self.client_address[0], self.headers.get("Cookie"), key):
                return True
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            presented = (query.get("key") or [""])[0]
            if allow_query_key and key_matches(presented, key):
                rest = [(k, v) for k, vs in query.items() if k != "key" for v in vs]
                target = parsed.path + ("?" + urlencode(rest) if rest else "")
                self.send_response(303)
                self.send_header("Location", target)
                self.send_header("Set-Cookie", f"{COOKIE}={key}; Path=/; HttpOnly; "
                                               f"SameSite=Lax; Max-Age=31536000")
                self.end_headers()
                return False
            self._refuse_key(parsed.path, wrong=bool(presented))
            return False

        def do_GET(self) -> None:  # noqa: N802 (stdlib name)
            if not host_allowed(self.headers.get("Host"), host, hostnames):
                self._refuse_host()
                return
            static = self._STATIC.get(urlparse(self.path).path)
            # Static assets carry no data and the refusal page needs them.
            if not static and not self._admit(allow_query_key=True):
                return
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
            conn = None
            try:
                conn = store.connect(db_path)
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
                if conn is not None:
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
            if not host_allowed(self.headers.get("Host"), host, hostnames):
                self._refuse_host()
                return
            if not self._admit(allow_query_key=False):
                return
            path = urlparse(self.path).path
            if not path.startswith("/settings/"):
                self.send_error(404)
                return
            if not same_origin(self.headers):
                self.send_error(403, "cross-site request refused")
                return
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            conn = None
            try:
                conn = store.connect(db_path)
                status, result = _handle_settings_post(
                    conn, path[len("/settings/"):], form)
            except sqlite3.OperationalError:
                # The poller is mid-commit and held the file past the busy
                # timeout. An answer, not a dropped socket: the page's fetch
                # fallback would otherwise re-submit the same form natively.
                status, result = 303, ("/settings?err=" + quote(
                    "The database was busy (a poll was writing). Nothing was "
                    "changed — try again in a moment."))
            finally:
                if conn is not None:
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
    port = server.server_address[1]
    print(f"dashboard: http://{host}:{port}/  (Ctrl-C to stop)")
    if widened:
        print("  reachable from the network. From this machine nothing is asked;")
        print("  every other device needs the dashboard key once — open this link")
        print("  there and the browser is remembered:")
        print(f"      {key_link(host, port, key)}")
        print("  (lastbell dashboard --show-key prints it again.) No TLS: the key")
        print("  crosses the network in the clear — fine on a home LAN or Tailscale,")
        print("  not the public internet; put a TLS proxy in front there.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
