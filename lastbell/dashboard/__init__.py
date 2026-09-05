"""Web dashboard (Phase 3).

The dashboard is for *looking things up on demand* — alerts are always pushed
out, so nobody has to open this to find out something changed. It's stdlib
``http.server`` over the same SQLite file the watch loop writes: no framework,
no build step. Pages are SELECTs; the only write paths are the forms on
/settings (``POST /settings/<action>``): watchers, subscriptions, and the
Display card's household settings — bookkeeping only, never grade data.

It binds 127.0.0.1 by default. To share it on your LAN set
LASTBELL_DASHBOARD_HOST=0.0.0.0 — and know that unlike alert payloads it
shows full names (and, on /settings, watcher addresses), so treat the bind
address as the access control; the write paths carry no auth of their own.
"""
from __future__ import annotations

from .. import schools  # re-exported: tests stub ``dashboard.schools.school_url``
from .queries import (
    HIGHLIGHT_STATUSES,
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
    fetch_percent_history,
    fetch_status_history,
    fetch_strip_rows,
    fetch_student,
    fetch_students,
    fetch_view_rows,
)
from .render import (
    render_alerts,
    render_history,
    render_overview,
    render_student,
)
from .settings import (
    render_settings,
)
from .server import (
    same_origin,
    serve,
)
# Private names the tests (and the CLI) reach through the package.
from .queries import _HISTORY_LIMIT  # noqa: F401
from .render import _APPJS_PATH, _FAVICON_PATH, _HISTORY_PREVIEW, _STYLE_PATH, _nav_names, _nav_students, _page, _page_window, _when_html  # noqa: F401
from .server import _handle, _handle_settings_post  # noqa: F401

__all__ = [
    "schools",
    "HIGHLIGHT_STATUSES",
    "alerts_last_page",
    "alerts_total",
    "build_student_ctx",
    "fetch_alert_counts",
    "fetch_alerts",
    "fetch_course_history",
    "fetch_courses",
    "fetch_history",
    "fetch_history_class_counts",
    "fetch_history_field_counts",
    "fetch_history_totals",
    "fetch_open_counts",
    "fetch_percent_history",
    "fetch_status_history",
    "fetch_strip_rows",
    "fetch_student",
    "fetch_students",
    "fetch_view_rows",
    "render_alerts",
    "render_history",
    "render_overview",
    "render_student",
    "render_settings",
    "same_origin",
    "serve",
]
