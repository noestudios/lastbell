"""Render the dashboard from ``lastbell seed-demo`` data into static files.

GitHub Pages serves the result as a demo anyone can click through before
installing anything: a fabricated family at end-of-quarter volume, no real
students. Every page the dashboard has is rendered once per view it offers
(the home page, each student's views, the alert log by page and by type,
history, settings) and every ``href='/…'`` is rewritten to the file that
page became, so the links work under a site prefix (``/lastbell``).

Forms can't work on a static site; a banner on every page says so.

    python scripts/build_demo_site.py --out _site --base /lastbell
"""
from __future__ import annotations

import argparse
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lastbell import seed, store
from lastbell.dashboard import server as srv

VIEWS = ("due", "everything", "recent", "problems")
_LINK = re.compile(r"(href|src|action)='(/[^']*)'")
_FRESHNESS = re.compile(r"<footer class='credit freshness[^']*'[^>]*>.*?</footer>", re.S)
_BANNER = ("<div class='demo-banner' role='note' style='margin:0 0 16px;padding:10px 14px;"
           "border-radius:8px;background:rgba(14,116,144,.12);font-size:14px;"
           "line-height:20px'>This is a static demo of the Last Bell dashboard: a "
           "fabricated family from <code>lastbell seed-demo</code>, no real students. "
           "Pages and links work; the forms on Settings don't. "
           "<a href='https://github.com/noestudios/lastbell'>Install your own.</a></div>")


def target(url: str, alert_types=()) -> str:
    """The static file a dashboard URL becomes (relative to the site root)."""
    u = urlparse(url)
    path, q = u.path, parse_qs(u.query)
    frag = f"#{u.fragment}" if u.fragment else ""
    if path == "/":
        out = "index.html"
    elif path == "/favicon.ico":
        out = "static/favicon.png"
    elif path.startswith("/static/"):
        out = path.lstrip("/")
    elif path.startswith("/student/"):
        agu = path[len("/student/"):]
        view = (q.get("view") or [""])[0]
        out = f"student/{agu}/{view if view in VIEWS else 'index'}.html"
    elif path == "/alerts":
        kind = (q.get("type") or [""])[0]
        page = (q.get("page") or ["1"])[0]
        if kind in alert_types:
            out = f"alerts/{kind}.html"
        elif page.isdigit() and int(page) > 1:
            out = f"alerts/page-{page}.html"
        else:
            out = "alerts/index.html"
    elif path == "/history":
        out = "history/all.html" if (q.get("all") or [""])[0] == "1" else "history/index.html"
    elif path in ("/settings", "/watchers") or path.startswith("/settings/"):
        out = "settings/index.html"
    else:
        out = "index.html"
    return out + frag


def rewrite(html: str, base: str, alert_types=(), built: date | None = None) -> str:
    html = _LINK.sub(lambda m: f"{m.group(1)}='{base}/{target(m.group(2), alert_types)}'", html)
    html = html.replace("<main id='main'>", "<main id='main'>" + _BANNER, 1)
    when = (built or date.today()).strftime("%b ") + str((built or date.today()).day)
    return _FRESHNESS.sub(f"<footer class='credit freshness'>Static demo built {when}. "
                          "A live install says when it last checked the portal here.</footer>",
                          html)


def build(out: Path, base: str = "", rng_seed: int = 2026, built: date | None = None) -> list:
    """Write the site; returns the list of files written."""
    base = base.rstrip("/")
    if out.exists():
        shutil.rmtree(out)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "demo.db"
        conn = store.connect(db)
        try:
            store.ensure_schema(conn)
            seed.seed_demo(conn, seed=rng_seed)
            students = srv.fetch_students(conn)
            counts = srv.fetch_alert_counts(conn)
            types = tuple(c["type"] for c in counts)
            urls = ["/", "/alerts", "/history", "/history?all=1", "/settings"]
            urls += [f"/alerts?page={n}" for n in
                     range(2, srv.alerts_last_page(srv.alerts_total(counts, "")) + 1)]
            urls += [f"/alerts?type={t}" for t in types]
            for s in students:
                urls.append(f"/student/{s['agu']}")
                urls += [f"/student/{s['agu']}?view={v}" for v in VIEWS]
            written = []
            for url in urls:
                status, html = srv._handle(conn, url)
                if status != 200:
                    continue
                dest = out / target(url, types).split("#")[0]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(rewrite(html, base, types, built), encoding="utf-8")
                written.append(dest)
        finally:
            conn.close()
    static = out / "static"
    static.mkdir(parents=True, exist_ok=True)
    for src in (srv._STYLE_PATH, srv._APPJS_PATH, srv._FAVICON_PATH):
        shutil.copy(src, static / Path(src).name)
        written.append(static / Path(src).name)
    (out / ".nojekyll").write_text("")
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="_site")
    ap.add_argument("--base", default="", help="site prefix, e.g. /lastbell")
    args = ap.parse_args(argv)
    files = build(Path(args.out), args.base)
    print(f"wrote {len(files)} files under {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
