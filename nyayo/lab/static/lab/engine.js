// An engine's page: while a read of the shown commit is queued or going on, ask where it is and show the page again when it is
// done or has failed; and offer the repository's branches as suggestions.
(function () {
  "use strict";
  var form = document.getElementById("engine-form");
  if (!form) return;

  var list = document.getElementById("branch-list");
  fetch("/create/test/branches?repo=" + encodeURIComponent(form.dataset.repo), { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      (data.branches || []).forEach(function (name) {
        var option = document.createElement("option");
        option.value = name;
        list.appendChild(option);
      });
    }).catch(function () { /* no suggestions */ });

  if (!form.dataset.waiting || !form.dataset.sha) return;
  var timer = setInterval(function () {
    fetch("/tunables?engine=" + encodeURIComponent(form.dataset.engine) + "&sha=" + encodeURIComponent(form.dataset.sha), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.state === "done" || data.state === "failed") { clearInterval(timer); location.reload(); }
      }).catch(function () { /* try again */ });
  }, 3000);
})();
