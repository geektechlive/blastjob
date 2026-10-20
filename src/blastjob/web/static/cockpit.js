// Cockpit behaviour that used to be inline in the templates. The Content-Security-
// Policy allows only same-origin scripts (plus nonced inline ones), so inline event
// handlers and hx-on attributes are not available; templates carry data attributes
// and this file acts on them. See docs/how-it-works/web.md.
(function () {
  "use strict";

  // data-after-request="reload|home" on an htmx element: runs after its request
  // finishes, whether or not it succeeded (what hx-on::after-request did).
  document.addEventListener("htmx:afterRequest", function (e) {
    var action = e.target && e.target.dataset ? e.target.dataset.afterRequest : "";
    if (action === "reload") {
      window.location.reload();
    } else if (action === "home") {
      window.location = "/";
    }
  });

  // A run panel (templates/_build_panel.html, _followup_panel.html): stream the
  // run's events into it. Panels arrive both in full pages and as htmx swaps, so
  // this runs from htmx.onLoad; data-wired stops a panel being wired twice.
  function wirePanel(panel) {
    if (panel.dataset.wired) { return; }
    panel.dataset.wired = "1";
    var runId = panel.dataset.runId;
    var out = panel.querySelector(".build-out");
    var st = panel.querySelector(".build-status");
    var es = new EventSource("/builds/" + encodeURIComponent(runId) + "/events");
    es.addEventListener("output", function (e) {
      out.textContent += e.data;
      out.scrollTop = out.scrollHeight;
    });
    es.addEventListener("log", function (e) { st.textContent = e.data; });
    es.addEventListener("done", function () {
      // Do NOT overwrite the status line here. The last `log` event is the payoff
      // ("Saved bundle to ...", "Saved {path}", "Profile saved"), and clobbering it
      // with "Done." is the whole of "I clicked Tailor and nothing happened".
      es.close();
      // The packet path is read back from build_runs.artifact_dir on demand, so it
      // also survives a refresh -- which an SSE-only message never could.
      if (panel.dataset.doneGet) {
        htmx.ajax("GET", panel.dataset.doneGet, panel.dataset.doneTarget);
      }
    });
    es.addEventListener("error", function (e) {
      // Native connection blips have no data and auto-reconnect (resumes via
      // Last-Event-ID). Only a payload means a real run error.
      if (e && e.data) { st.textContent = "Error: " + e.data; es.close(); }
    });
  }

  htmx.onLoad(function (root) {
    if (root.matches && root.matches(".build-panel[data-run-id]")) { wirePanel(root); }
    if (root.querySelectorAll) {
      root.querySelectorAll(".build-panel[data-run-id]").forEach(wirePanel);
    }
  });
})();
