// The Create test page: a preset fills in the fields it sets, a kind sets the bounds, and what GitHub says of the
// typed branches is asked for as they're typed (the form works without it: the server asks again when it creates).
(function () {
  "use strict";
  var form = document.getElementById("workload");
  if (!form) return;
  var engines = JSON.parse(document.getElementById("engine-table").textContent);

  function field(name) { return document.getElementById("f-" + name); }
  function fact(id, text, state) {
    var cell = document.getElementById(id);
    cell.textContent = text || "–";
    cell.className = cell.className.replace(/\b(none|bad|warn)\b/g, "").trim();
    if (state) cell.classList.add(state);
  }
  function checked(name) { return form.querySelector('input[name="' + name + '"]:checked'); }

  // the fields a preset sets are its own, apart from the ones that name a place or a kind of test
  var KEPT = { bounds: 1, confidence: 1, base_branch: 1, dev_repo: 1, base_repo: 1 };

  function applyPreset() {
    var radio = checked("preset");
    var engine = engines[field("engine").value];
    if (!radio || !engine) return;
    var values = engine.presets[radio.value] || {};
    Object.keys(values).forEach(function (name) {
      var f = field(name);
      if (f && !KEPT[name]) f.value = values[name];
    });
    summarize();
  }

  function summarize() {
    fact("r-repo", field("dev_repo").value);
    fact("r-bounds", field("test_mode").value === "SPRT" ? field("bounds").value : field("max_games").value + " games");
    fact("r-tc", field("dev_tc").value);
    fact("r-options", field("dev_options").value);
    var typed = field("name").value.trim();
    fact("r-name", typed || document.getElementById("r-name").dataset.found || "", typed || document.getElementById("r-name").dataset.found ? null : "none");
  }

  var timer = null, serial = 0;
  function lookup() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      var dev = field("dev_branch").value.trim(), base = field("base_branch").value.trim();
      var mine = ++serial;
      if (!dev) { ["r-sha", "r-subject", "r-bench", "r-distance"].forEach(function (id) { fact(id, "–", "none"); }); return; }
      var url = form.dataset.lookup + "?" + new URLSearchParams({ repo: field("dev_repo").value, dev: dev, base: base });
      fetch(url, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (data) {
        if (mine === serial) show(data, dev);
      }).catch(function () { fact("r-subject", "the lookup did not answer", "warn"); });
    }, 400);
  }

  function show(data, dev) {
    if (data.error) { fact("r-subject", data.error, "bad"); return; }
    var d = data.dev || {}, b = data.base || {};
    if (d.down) { fact("r-subject", "GitHub did not answer: type the full sha and the bench under Advanced to go without it", "warn"); return; }
    if (d.missing) {
      fact("r-sha", "–", "none"); fact("r-subject", dev + " not found in " + field("dev_repo").value, "bad");
      fact("r-bench", "–", "none"); fact("r-distance", "–", "none"); return;
    }
    document.getElementById("r-name").dataset.found = d.named_by ? d.name + " (" + d.named_by + ")" : d.name;
    summarize();
    fact("r-sha", d.short);
    fact("r-subject", d.subject);
    var typedDev = field("dev_bench").value.trim(), typedBase = field("base_bench").value.trim();
    if (typedDev) fact("r-bench", typedDev + " (typed)");
    else fact("r-bench", d.bench ? d.bench + " (from the commit message)" : "no Bench: line in the commit message", d.bench ? null : "bad");
    fact("r-base-sha", b.short || "–", b.short ? null : "none");
    if (typedBase) fact("r-base-bench", typedBase + " (typed)");
    else fact("r-base-bench", b.bench || (b.short ? "no Bench: line in the commit message" : "–"), b.bench ? null : (b.short ? "bad" : "none"));
    if (data.behind) fact("r-distance", data.behind + " behind base: rebase first", "warn");
    else if (data.behind === 0) fact("r-distance", "level with base");
    else fact("r-distance", "–", "none");
  }

  form.querySelectorAll('input[name="preset"]').forEach(function (r) { r.addEventListener("change", applyPreset); });
  form.querySelectorAll('input[name="test_kind"]').forEach(function (r) {
    r.addEventListener("change", function () { field("bounds").value = r.dataset.bounds; summarize(); });
  });
  ["dev_branch", "base_branch", "dev_repo"].forEach(function (n) { field(n).addEventListener("input", lookup); });
  ["name", "dev_tc", "dev_options", "bounds", "max_games", "test_mode", "dev_repo"].forEach(function (n) { field(n).addEventListener("input", summarize); });
  field("engine").addEventListener("change", function () {
    var engine = engines[field("engine").value];
    if (engine) { field("dev_repo").value = engine.source; field("base_repo").value = engine.source; field("scale_nps").value = engine.nps; }
    applyPreset();
  });
  summarize();
  if (field("dev_branch").value.trim()) lookup();
})();
