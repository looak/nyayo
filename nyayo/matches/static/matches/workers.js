// The Workers area of a test's page, redrawn while the test can still change: every 2 seconds while a step is open,
// every 15 seconds otherwise (a worker may pick the test up). The area is fetched whole from the page's own
// /workers url and drawn only when it differs, keeping the logs a reader opened or closed. When the test's state
// has changed since the page was drawn, the page reloads so the head and the verdict follow.
(function () {
  var box = document.getElementById("workers");
  if (!box || !box.dataset.url) return;
  var state = box.dataset.state, shown = box.innerHTML, toggled = {};
  box.addEventListener("toggle", function (event) {
    var key = event.target.dataset && event.target.dataset.key;
    if (key) toggled[key] = event.target.open;
  }, true);

  function draw(html) {
    if (html === shown) return;
    shown = html;
    box.innerHTML = html;
    box.querySelectorAll("details[data-key]").forEach(function (el) {
      if (el.dataset.key in toggled) el.open = toggled[el.dataset.key];
    });
  }

  function next(live, open) {
    if (live) setTimeout(poll, open ? 2000 : 15000);
  }

  function poll() {
    fetch(box.dataset.url, {credentials: "same-origin", headers: {Accept: "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return next(true, false);
        if (d.state !== state) return location.reload();
        draw(d.html);
        next(d.live, d.open);
      })
      .catch(function () { next(true, false); });
  }

  next(box.dataset.live === "1", box.dataset.open === "1");
})();
