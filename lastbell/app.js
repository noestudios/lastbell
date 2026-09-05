/* Settings-page niceties. Everything here is progressive enhancement over
 * plain form posts — with JS off, every button still works via the normal
 * POST → redirect → reload cycle. With JS on, form posts go over fetch and
 * the page's #settings-main region is swapped in place: no navigation, no
 * scroll reset. Motion timings follow the jshq tiers: fast (150ms) for
 * hover/press feedback, base (300ms) for fades, slow (400ms) for larger
 * movement (row collapse/expansion), linger (750ms) for attention decay
 * (the toast fade-out). One symmetric ease drives enters and exits.
 */
(function () {
  "use strict";

  var FAST = 150, BASE = 300, SLOW = 400, LINGER = 750;
  var EASE = "cubic-bezier(0.4, 0, 0.2, 1)";

  function reducedMotion() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  /* ── toast ──────────────────────────────────────────────────────────
   * The server renders the toast inside #settings-main; CSS runs its
   * enter. Dismissal is attention decaying, not movement: opacity only,
   * on the linger tier, removed by timeout (never animationend — hidden
   * documents don't run animations). */
  function armToast() {
    var toast = document.querySelector(".toast");
    if (!toast || toast.dataset.armed) return;
    toast.dataset.armed = "1";
    // 6s hold: the messages name who/what changed and deserve a real read.
    // Hovering or focusing the toast pauses the clock — slow readers keep
    // it as long as they're on it; leaving restarts the full hold.
    var timer = null;
    function dismiss() {
      toast.classList.add("toast-exit");
      setTimeout(function () { toast.remove(); }, LINGER);
    }
    function arm() { timer = setTimeout(dismiss, 6000); }
    function hold() { if (timer) { clearTimeout(timer); timer = null; } }
    toast.addEventListener("mouseenter", hold);
    toast.addEventListener("focusin", hold);
    toast.addEventListener("mouseleave", arm);
    toast.addEventListener("focusout", arm);
    arm();
  }

  /* Strip ?ok=/&new= after a full-page (JS-off style) navigation so a
   * refresh doesn't replay the toast and entrance motion. Fetch-based
   * posts never touch the URL in the first place. */
  function cleanUrl() {
    var url = new URL(window.location.href);
    if (!url.searchParams.has("ok") && !url.searchParams.has("new")) return;
    url.searchParams.delete("ok");
    url.searchParams.delete("new");
    history.replaceState(null, "", url.pathname + (url.search || ""));
  }

  /* ── dirty tracking ─────────────────────────────────────────────────
   * Update buttons stay hidden until the row's fields differ from what the
   * server rendered. form.elements includes controls bound via form=, so
   * one scan covers the whole row. */
  function isDirty(form) {
    return Array.prototype.some.call(form.elements, function (el) {
      if (el.tagName === "SELECT") {
        var def = null;
        for (var i = 0; i < el.options.length; i++) {
          if (el.options[i].defaultSelected) { def = el.options[i]; break; }
        }
        if (!def) def = el.options[0];
        return def ? el.value !== def.value : false;
      }
      if (el.tagName === "INPUT" && el.type === "checkbox") {
        return el.checked !== el.defaultChecked;
      }
      if (el.tagName === "INPUT" && el.type !== "hidden") {
        return el.value !== el.defaultValue;
      }
      return false;
    });
  }

  function trackDirty(e) {
    var form = e.target && e.target.form;
    if (form && form.classList.contains("rowform")) {
      form.classList.toggle("dirty", isDirty(form));
    }
  }

  /* ── row exit (removals) ────────────────────────────────────────────
   * Fade the row on the base tier, then collapse its height on the slow
   * tier so the content below slides up — THEN post. The submitting form
   * is moved to <body> first so emptying the cells can't disconnect it. */
  function rowsFor(form) {
    var group = form.getAttribute("data-group");
    if (group) {
      return Array.prototype.slice.call(
        document.querySelectorAll("tr[data-w='" + group + "']"));
    }
    var row = form.closest("tr");
    return row ? [row] : [];
  }

  function collapseRow(row) {
    Array.prototype.forEach.call(row.querySelectorAll("td"), function (td) {
      var h = td.offsetHeight;
      td.textContent = "";
      td.style.boxSizing = "border-box";
      td.style.height = h + "px";
      td.style.overflow = "hidden";
    });
    row.getBoundingClientRect();   // commit start values before transitioning
    Array.prototype.forEach.call(row.querySelectorAll("td"), function (td) {
      td.style.transition = "height " + SLOW + "ms " + EASE +
        ", padding " + SLOW + "ms " + EASE;
      td.style.height = "0px";
      td.style.paddingTop = "0";
      td.style.paddingBottom = "0";
      td.style.borderBottomWidth = "0";
    });
  }

  function animateRemoval(form, action, done) {
    var rows = rowsFor(form);
    rows.forEach(function (row) {
      row.style.transition = "opacity " + BASE + "ms " + EASE;
      row.style.opacity = "0";
    });
    setTimeout(function () {
      form.style.display = "none";
      document.body.appendChild(form);
      rows.forEach(collapseRow);
      setTimeout(done, SLOW + 20);
    }, BASE);
  }

  /* ── fetch-based posting ────────────────────────────────────────────
   * The server answers a form post with a 303 back to /settings; fetch
   * follows it, so one round trip yields both the outcome (in the final
   * URL's ?ok/?err/?new params) and the fresh page. Swap #settings-main
   * and the scroll position never moves. */
  function nativeFallback(form, action) {
    form.dataset.animated = "1";        // let the submit through untouched
    form.setAttribute("action", action);
    form.requestSubmit();
  }

  function ajaxSubmit(form, action) {
    var body;
    try {
      body = new URLSearchParams(new FormData(form)).toString();
    } catch (e) {
      nativeFallback(form, action);
      return;
    }
    fetch(action, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
      credentials: "same-origin",
    }).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.text().then(function (text) {
        return { url: resp.url, text: text };
      });
    }).then(function (res) {
      applySwap(res.text, res.url);
    }).catch(function () {
      nativeFallback(form, action);
    });
  }

  /* Mirror a status/error message into the persistent live region in the
   * page shell — a region that arrives pre-populated inside the swapped
   * subtree is not reliably announced, one that mutates in place is. */
  function announce(text) {
    var region = document.getElementById("announce");
    if (region) region.textContent = text || "";
  }

  function applySwap(html, finalUrl) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    var next = doc.getElementById("settings-main");
    var cur = document.getElementById("settings-main");
    if (!next || !cur) {            // not the settings page — hard navigate
      window.location.href = finalUrl;
      return;
    }
    // The swap destroys the focused element; remember something stable to
    // put focus back on (the row forms carry stable ids), else the section
    // heading — a keyboard user must not restart from the top of the page.
    var focusId = "";
    var active = document.activeElement;
    if (active && cur.contains(active)) {
      var anchor = active.closest("[id]");
      focusId = (anchor && anchor.id) || "";
    }
    cur.replaceWith(document.importNode(next, true));
    if (focusId) {
      var target = document.getElementById(focusId) ||
        document.querySelector("#settings-main h2");
      if (target) {
        // Rows/headings aren't natively focusable; controls must not lose
        // their tab-order, so only non-controls get the tabindex.
        if (!/^(BUTTON|INPUT|SELECT|TEXTAREA|A)$/.test(target.tagName)) {
          target.setAttribute("tabindex", "-1");
        }
        target.focus();
      }
    }
    var params;
    try {
      params = new URL(finalUrl, window.location.href).searchParams;
    } catch (e) {
      params = new URLSearchParams();
    }
    if (!reducedMotion()) {
      (params.get("new") || "").split(",").filter(Boolean).forEach(function (id) {
        var row = document.getElementById(id);
        if (row) enterRow(row);
      });
    }
    armToast();
    var toast = document.querySelector(".toast");
    if (toast) announce(toast.textContent);
    if (params.get("err")) {
      var banner = document.querySelector(".banner");
      if (banner) {
        announce(banner.textContent);
        banner.scrollIntoView({
          behavior: reducedMotion() ? "auto" : "smooth", block: "center"
        });
      }
    }
  }

  var REMOVE_ACTIONS = /\/settings\/(watcher-remove|channel-remove|unsubscribe)$/;

  /* ── confirmation (destructive removals) ────────────────────────────
   * Removing a watcher, or a watcher's LAST subscription, pops a Cancel /
   * Remove dialog that says plainly what is about to happen. Channel and
   * non-final subscription removals stay one click. */
  function confirmMessage(form, action) {
    if (/watcher-remove$/.test(action)) {
      var input = form.querySelector("input[name='name']");
      var name = (input && input.value) || "this watcher";
      return "Remove the watcher “" + name + "”? Their channels and " +
        "subscriptions are deleted too — they will stop receiving alerts.";
    }
    if (/unsubscribe$/.test(action)) {
      var row = form.closest("tr");
      var cell = row && row.querySelector("td");
      if (!cell) return null;
      var watcher = cell.textContent.split("⇒")[0].trim();
      var count = Array.prototype.filter.call(
        document.querySelectorAll("tr[id^='row-sub-'] td:first-child"),
        function (td) { return td.textContent.split("⇒")[0].trim() === watcher; }
      ).length;
      if (count <= 1) {
        return "Remove “" + watcher + "”’s last subscription? " +
          "They will stop receiving alerts entirely.";
      }
    }
    return null;
  }

  function showConfirm(message, opener, onRemove) {
    var scrim = document.createElement("div");
    scrim.className = "scrim";
    var box = document.createElement("div");
    box.className = "confirm";
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-modal", "true");
    // The message IS the dialog's accessible name — an alertdialog with no
    // name announces as an empty box in several SR/browser pairs.
    box.setAttribute("aria-describedby", "confirm-msg");
    box.setAttribute("aria-label", "Confirm removal");
    box.innerHTML = "<p id='confirm-msg'></p><div class='confirm-actions'>" +
      "<button type='button' class='ghost'>Cancel</button>" +
      "<button type='button' class='danger'>Remove</button></div>";
    box.querySelector("p").textContent = message;
    function close() {
      document.removeEventListener("keydown", onKey);
      scrim.remove();
      box.remove();
      if (opener && opener.isConnected) opener.focus();
    }
    function onKey(e) {
      if (e.key === "Escape") { close(); return; }
      // aria-modal promises the page behind doesn't exist; make Tab keep
      // that promise by wrapping between the dialog's two buttons.
      if (e.key === "Tab") {
        var btns = box.querySelectorAll("button");
        var first = btns[0], last = btns[btns.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        } else if (!box.contains(document.activeElement)) {
          e.preventDefault(); first.focus();
        }
      }
    }
    scrim.addEventListener("click", close);
    box.querySelector(".ghost").addEventListener("click", close);
    box.querySelector(".danger").addEventListener("click", function () {
      close();
      onRemove();
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(scrim);
    document.body.appendChild(box);
    box.querySelector(".ghost").focus();
  }

  function onSubmit(e) {
    var form = e.target;
    if (form.dataset.animated) return;   // fallback resubmission: let it go
    var action = (e.submitter && e.submitter.getAttribute("formaction")) ||
      form.getAttribute("action") || "";
    if (action.indexOf("/settings/") !== 0) return;
    if (!window.fetch || !window.DOMParser || !window.URLSearchParams) return;
    e.preventDefault();
    if (form.dataset.busy || document.querySelector(".scrim")) return;

    function proceed() {
      form.dataset.busy = "1";           // one in-flight post per form
      if (REMOVE_ACTIONS.test(action) && !reducedMotion()) {
        animateRemoval(form, action, function () { ajaxSubmit(form, action); });
      } else {
        ajaxSubmit(form, action);
      }
    }

    var message = REMOVE_ACTIONS.test(action) && confirmMessage(form, action);
    if (message) showConfirm(message, e.submitter, proceed);
    else proceed();
  }

  /* ── row entrance (?new=) ───────────────────────────────────────────
   * The space opens first (slow tier — content below slides down), then
   * the fields fade in (base tier). */
  function enterRow(row) {
    var tds = Array.prototype.slice.call(row.querySelectorAll("td"));
    var heights = tds.map(function (td) { return td.offsetHeight; });
    tds.forEach(function (td) {
      td.style.boxSizing = "border-box";
      td.style.overflow = "hidden";
      td.style.height = "0px";
      td.style.paddingTop = "0";
      td.style.paddingBottom = "0";
      td.style.borderBottomWidth = "0";
    });
    row.style.opacity = "0";
    row.getBoundingClientRect();
    tds.forEach(function (td, i) {
      td.style.transition = "height " + SLOW + "ms " + EASE +
        ", padding " + SLOW + "ms " + EASE;
      td.style.height = heights[i] + "px";
      td.style.paddingTop = "";
      td.style.paddingBottom = "";
      td.style.borderBottomWidth = "";
    });
    setTimeout(function () {
      row.style.transition = "opacity " + BASE + "ms " + EASE;
      row.style.opacity = "1";
      setTimeout(function () {   // clear inline styles so resizes reflow freely
        row.style.cssText = "";
        tds.forEach(function (td) { td.style.cssText = ""; });
      }, BASE + 20);
    }, SLOW);
  }

  function initEntrances() {
    var params = new URLSearchParams(window.location.search);
    var ids = (params.get("new") || "").split(",").filter(Boolean);
    if (!ids.length || reducedMotion()) return;
    ids.forEach(function (id) {
      var row = document.getElementById(id);
      if (row) enterRow(row);
    });
  }

  /* ── alert-types multiselect ────────────────────────────────────────
   * 'all alerts' is exclusive, at least one box stays checked, and the
   * summary mirrors the selection. Registered before trackDirty so the
   * dirty scan sees the reconciled checkboxes. Open popovers (this
   * multiselect and the nav student menu) close on an outside click. */
  function onTypeToggle(cb) {
    var det = cb.closest("details.msel");
    if (!det) return;
    var boxes = Array.prototype.slice.call(
      det.querySelectorAll("input[name='type']"));
    var all = boxes.filter(function (b) { return b.value === "*"; })[0];
    if (cb === all && cb.checked) {
      boxes.forEach(function (b) { if (b !== all) b.checked = false; });
    } else if (cb !== all && cb.checked && all) {
      all.checked = false;
    }
    if (!boxes.some(function (b) { return b.checked; }) && all) {
      all.checked = true;
    }
    var sel = boxes.filter(function (b) { return b.checked; });
    det.querySelector("summary").textContent =
      (all && all.checked) ? "all alerts"
        : sel.length === 1 ? sel[0].parentNode.textContent.trim()
          : sel.length + " types";
  }

  document.addEventListener("change", function (e) {
    if (e.target && e.target.name === "type") onTypeToggle(e.target);
  });
  document.addEventListener("click", function (e) {
    Array.prototype.forEach.call(
      document.querySelectorAll("details.msel[open], details.smenu[open]"),
      function (det) {
        if (!det.contains(e.target)) det.removeAttribute("open");
      });
  });
  // Escape closes an open popover the way outside-click does, returning
  // focus to its summary so keyboard users aren't stranded mid-page.
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    Array.prototype.forEach.call(
      document.querySelectorAll("details.msel[open], details.smenu[open]"),
      function (det) {
        var hadFocus = det.contains(document.activeElement);
        det.removeAttribute("open");
        var sum = det.querySelector("summary");
        if (sum && hadFocus) sum.focus();
      });
  });
  document.addEventListener("input", trackDirty);
  document.addEventListener("change", trackDirty);
  document.addEventListener("submit", onSubmit);
  /* ── student page: view switching in place ──────────────────────────
   * The four stat cards are links to ?view=…; followed as links they load
   * a new document, which starts at the top. Here a click fetches the
   * target page and swaps #student-view (the cards + the panel) so the
   * scroll position never moves; the URL and title follow via pushState
   * so bookmarks, refresh and the back button behave as before. Only a
   * pure view switch is intercepted — same path, same ?course= scope, no
   * #fragment — anything else (a course-scoped link, a #hit deep link) is
   * left to navigate. The course strip sits outside the region: its
   * toggle state survives, and its ?view= links are rewritten to match. */
  function viewSwitchTarget(link) {
    var region = document.getElementById("student-view");
    if (!region || !region.contains(link)) return null;
    var to, here;
    try {
      to = new URL(link.getAttribute("href"), window.location.href);
      here = new URL(window.location.href);
    } catch (e) {
      return null;
    }
    if (to.origin !== here.origin || to.pathname !== here.pathname) return null;
    if (to.hash || !to.searchParams.get("view")) return null;
    if ((to.searchParams.get("course") || "") !== (here.searchParams.get("course") || "")) {
      return null;
    }
    return to;
  }

  function rewriteStripViews(view) {
    Array.prototype.forEach.call(
      document.querySelectorAll("#allcourses a[href]"),
      function (a) {
        var u;
        try { u = new URL(a.getAttribute("href"), window.location.href); }
        catch (e) { return; }
        if (!u.searchParams.has("view")) return;
        u.searchParams.set("view", view);
        a.setAttribute("href", u.pathname + u.search + u.hash);
      });
  }

  function swapView(url, push) {
    return fetch(url.href, { credentials: "same-origin" }).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.text();
    }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, "text/html");
      var next = doc.getElementById("student-view");
      var cur = document.getElementById("student-view");
      if (!next || !cur) throw new Error("no region");
      var y = window.scrollY;
      cur.replaceWith(document.importNode(next, true));
      if (doc.title) document.title = doc.title;
      if (push) history.pushState({ lastbellView: 1 }, "", url.pathname + url.search);
      rewriteStripViews(url.searchParams.get("view"));
      window.scrollTo(0, y);            // belt and braces: the swap must not move the page
      var active = document.querySelector("#student-view a.stat.active");
      if (active) active.focus({ preventScroll: true });
      var label = active && active.querySelector(".lbl");
      if (label) announce(label.textContent + " view");
    });
  }

  document.addEventListener("click", function (ev) {
    if (ev.defaultPrevented || ev.button !== 0) return;
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    var link = ev.target.closest && ev.target.closest("a[href]");
    if (!link || link.target) return;
    var to = viewSwitchTarget(link);
    if (!to) return;
    ev.preventDefault();
    swapView(to, true).catch(function () { window.location.href = to.href; });
  });

  window.addEventListener("popstate", function () {
    if (!document.getElementById("student-view")) return;
    var here;
    try { here = new URL(window.location.href); } catch (e) { return; }
    if (!here.searchParams.get("view")) here.searchParams.set("view", "problems");
    swapView(here, false).catch(function () { window.location.reload(); });
  });

  window.addEventListener("DOMContentLoaded", function () {
    initEntrances();
    armToast();
    cleanUrl();
  });
})();
