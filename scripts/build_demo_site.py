"""Render the dashboard from ``lastbell seed-demo`` data into static files.

GitHub Pages serves the result as a demo anyone can click through before
installing anything: a fabricated family at end-of-quarter volume, no real
students. Starting from the home page, every internal link is followed and the
page it reaches rendered — each student's views and course filters, the
alert log by page and by type, history by class and by kind of change —
and every ``href='/…'`` is rewritten to the file that page became, so
the links work under a site prefix (``/lastbell``).

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
from urllib.parse import parse_qs, unquote, urlparse

from lastbell import seed, store
from lastbell.dashboard import server as srv

_LINK = re.compile(r"(href|src|action)='(/[^']*)'")
_FRESHNESS = re.compile(r"<footer class='credit freshness[^']*'[^>]*>.*?</footer>", re.S)
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
# Query parameters that don't change what a page shows: ``strip=open`` is
# the JS-off way to clear a course filter, i.e. the unfiltered page.
_IGNORED_PARAMS = {"strip"}
_BANNER = ("<div class='demo-banner' role='note' style='margin:0 0 16px;padding:10px 14px;"
           "border-radius:8px;background:rgba(14,116,144,.12);font-size:14px;"
           "line-height:20px'>This is a static demo of the Last Bell dashboard: a "
           "fabricated family from <code>lastbell seed-demo</code>, no real students. "
           "Pages and links work; nothing can be changed. "
           "<a href='https://github.com/noestudios/lastbell'>Install your own.</a></div>")

# Forms can't post to a static host (Pages answers 405). This runs before
# app.js and catches every submit in the capture phase, so neither the
# browser nor the dashboard's fetch-based posting ever sends it; the
# dashboard's own toast says why, in the warn color, and dismisses itself
# the way app.js dismisses a real one.
_DEMO_JS = (
    "<script>document.addEventListener('submit',function(e){"
    "e.preventDefault();e.stopImmediatePropagation();"
    "var old=document.querySelector('.toast');if(old)old.remove();"
    "var t=document.createElement('div');t.className='toast';t.setAttribute('role','status');"
    "t.style.borderLeftColor='var(--warn)';"
    "t.textContent='This is a static demo, so nothing can be changed here. "
    "Install Last Bell to get a dashboard of your own.';"
    "document.body.appendChild(t);"
    "var a=document.getElementById('announce');if(a)a.textContent=t.textContent;"
    "setTimeout(function(){t.classList.add('toast-exit');"
    "setTimeout(function(){t.remove();},400);},6000);"
    "},true);</script>")


def target(url: str) -> str:
    """The static file a dashboard URL becomes (relative to the site root).
    A page's directory comes from its path; its name from the query
    parameters that shape it (``course-c1_view-due.html``), so every
    filter combination the dashboard links to is its own file."""
    u = urlparse(url)
    path = u.path
    frag = f"#{u.fragment}" if u.fragment else ""
    params = {k: v[0] for k, v in parse_qs(u.query).items()
              if k not in _IGNORED_PARAMS and v and v[0]}
    if path == "/favicon.ico":
        return "static/favicon.png"
    if path.startswith("/static/"):
        return path.lstrip("/")
    if path == "/":
        folder, params = "", {}
    elif path.startswith("/student/"):
        folder = f"student/{_SAFE.sub('_', unquote(path[len('/student/'):]))}"
    elif path == "/alerts":
        folder = "alerts"
        if params.get("page") == "1":
            del params["page"]
    elif path == "/history":
        folder = "history"
    elif path in ("/settings", "/watchers") or path.startswith("/settings/"):
        folder, params = "settings", {}
    else:
        folder, params = "", {}
    name = ("_".join(f"{k}-{_SAFE.sub('_', v)}" for k, v in sorted(params.items()))
            or "index")
    return (f"{folder}/{name}.html" if folder else f"{name}.html") + frag


def rewrite(html: str, base: str, built: date | None = None) -> str:
    html = _LINK.sub(lambda m: f"{m.group(1)}='{base}/{target(m.group(2))}'", html)
    html = html.replace("<main id='main'>", "<main id='main'>" + _BANNER, 1)
    html = html.replace("<script src=", _DEMO_JS + "<script src=", 1)
    when = (built or date.today()).strftime("%b ") + str((built or date.today()).day)
    return _FRESHNESS.sub(f"<footer class='credit freshness'>Static demo built {when}. "
                          "A live install says when it last checked the portal here.</footer>",
                          html)


def crawl(conn) -> dict:
    """Render the home page, then every internal link any rendered page
    carries, until nothing new turns up. Returns {file: html}."""
    pages: dict = {}
    queue = ["/", "/settings"]
    while queue:
        url = queue.pop()
        dest = target(url).split("#")[0]
        if dest in pages or dest.startswith("static/"):
            continue
        status, html = srv._handle(conn, url)
        if status == 301:                 # a redirect: follow it, once
            queue.append(html)
            continue
        if status != 200:
            continue
        pages[dest] = html
        for _, href in _LINK.findall(html):
            queue.append(href)
    return pages


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
            pages = crawl(conn)
        finally:
            conn.close()
    written = []
    for rel, html in pages.items():
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rewrite(html, base, built), encoding="utf-8")
        written.append(dest)
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
