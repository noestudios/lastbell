"""The static demo (GitHub Pages): every dashboard page rendered from seed
data, every link rewritten to the file it became."""
from __future__ import annotations

import importlib.util
import re
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_demo_site.py"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("build_demo_site", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path_factory.mktemp("site")
    files = mod.build(out, "/lastbell", built=date(2026, 9, 4))
    return mod, out, files


def test_targets():
    spec = importlib.util.spec_from_file_location("build_demo_site", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    t = mod.target
    assert t("/") == "index.html"
    assert t("/?anything=1") == "index.html"
    assert t("/student/D1") == "student/D1/index.html"
    assert t("/student/D1?view=due") == "student/D1/view-due.html"
    assert t("/student/D1?course=c1") == "student/D1/course-c1.html"
    assert t("/student/D1?view=due&course=c1") == "student/D1/course-c1_view-due.html"
    assert t("/student/D1?view=due&strip=open") == "student/D1/view-due.html"
    assert t("/student/D1?view=problems&status=missing#hit") == \
        "student/D1/status-missing_view-problems.html#hit"
    assert t("/student/A%20B?course=x/y") == "student/A_B/course-x_y.html"
    assert t("/alerts") == "alerts/index.html"
    assert t("/alerts?page=1") == "alerts/index.html"
    assert t("/alerts?page=3") == "alerts/page-3.html"
    assert t("/alerts?type=grade_drop") == "alerts/type-grade_drop.html"
    assert t("/history?all=1") == "history/all-1.html"
    assert t("/history?course=c1&field=score") == "history/course-c1_field-score.html"
    assert t("/settings/watcher-add") == "settings/index.html"
    assert t("/watchers") == "settings/index.html"
    assert t("/static/style.css") == "static/style.css"
    assert t("/favicon.ico") == "static/favicon.png"


def test_site_has_every_page_and_every_link_resolves(site):
    mod, out, files = site
    for rel in ("index.html", "student/D1/index.html", "student/D1/view-due.html",
                "student/D1/course-c0.html", "student/D1/course-c0_view-due.html",
                "student/D2/view-everything.html", "alerts/index.html", "alerts/page-2.html",
                "history/index.html", "history/all-1.html", "history/field-score.html",
                "settings/index.html", "static/style.css", "static/app.js",
                "static/favicon.png", ".nojekyll"):
        assert (out / rel).exists(), rel
    assert any(p.name.startswith("course-") and "_field-" in p.name
               for p in (out / "history").iterdir())        # class × kind combos
    pages = list(out.rglob("*.html"))
    assert len(pages) > 100
    for page in pages:
        html = page.read_text(encoding="utf-8")
        assert not re.search(r"(href|src|action)='/[^l]", html), page   # only /lastbell/…
        assert "href='/lastbell/static/style.css'" in html
        assert "static demo of the Last Bell dashboard" in html
        # The submit catcher precedes app.js so it registers first.
        assert html.index("document.addEventListener('submit'") < html.index("static/app.js")
        # Every link on every page is a file that exists.
        for href in re.findall(r"href='/lastbell/([^'#]+)'", html):
            assert (out / href).exists(), (page, href)
    home = (out / "index.html").read_text(encoding="utf-8")
    assert "Static demo built Sep 4" in home
    assert "Last checked" not in home
    assert "href='/lastbell/student/D1/index.html'" in home
    assert "The watcher looks like it isn't running" not in home
