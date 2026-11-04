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

  // The branch fields are comboboxes over the source's real branches: an arrow and a list that opens on a click, filters as
  // you type, and takes the arrow keys, Enter and Escape. The text stays free (a sha or any other name works), and with
  // GitHub down the list says why it is empty and the form is as it was.
  var branches = { repo: null, state: "loading", names: [], why: "" };
  var combos = [];

  function fillBranches() {
    var repo = field("dev_repo").value;
    if (branches.repo === repo) return;
    branches = { repo: repo, state: "loading", names: [], why: "" };
    combos.forEach(function (c) { c.render(); });
    fetch(form.dataset.branches + "?" + new URLSearchParams({ repo: repo }), { credentials: "same-origin" })
      .then(function (r) { return r.json(); }).then(function (data) {
        if (branches.repo !== repo) return;
        branches.names = data.branches || [];
        branches.state = branches.names.length ? "ready" : "none";
        branches.why = data.error || "";
        combos.forEach(function (c) { c.render(); });
      }).catch(function () {
        if (branches.repo !== repo) return;
        branches.state = "none"; branches.why = "The list did not answer.";
        branches.repo = null;
        combos.forEach(function (c) { c.render(); });
      });
  }

  function combo(input) {
    var wrap = document.createElement("span"), list = document.createElement("ul"), arrow = document.createElement("button");
    wrap.className = "combo";
    list.className = "combo-list"; list.id = input.id + "-list"; list.setAttribute("role", "listbox"); list.hidden = true;
    arrow.type = "button"; arrow.className = "combo-arrow"; arrow.tabIndex = -1; arrow.textContent = "▾";
    arrow.setAttribute("aria-label", "Show the branches");
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input); wrap.appendChild(arrow); wrap.appendChild(list);
    input.setAttribute("role", "combobox"); input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", list.id); input.setAttribute("aria-expanded", "false");
    var open = false, filter = "", active = -1, shown = [];

    function note(text) { var li = document.createElement("li"); li.className = "combo-note"; li.textContent = text; list.appendChild(li); }

    function render() {
      list.textContent = ""; shown = []; active = -1;
      if (!open) return;
      if (branches.state === "loading") { note("Loading branches…"); return; }
      if (branches.state === "none") { note((branches.why || "No branches found.") + " Type a branch or a sha."); return; }
      var needle = filter.toLowerCase();
      shown = branches.names.filter(function (n) { return n.toLowerCase().indexOf(needle) >= 0; });
      shown.forEach(function (name, i) {
        var li = document.createElement("li");
        li.textContent = name; li.setAttribute("role", "option"); li.id = list.id + "-" + i;
        if (name === input.value) li.classList.add("current");
        li.addEventListener("mousedown", function (e) { e.preventDefault(); pick(name); });
        li.addEventListener("click", function (e) { e.stopPropagation(); });
        list.appendChild(li);
      });
      if (!shown.length) note("No branch matches “" + filter + "”: it is used as typed.");
    }

    function setOpen(on, all) {
      open = on; filter = all ? "" : input.value.trim();
      list.hidden = !on; input.setAttribute("aria-expanded", on ? "true" : "false");
      if (on) fillBranches();
      render();
    }

    function pick(name) {
      input.value = name;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      setOpen(false);
    }

    function move(by) {
      if (!shown.length) return;
      active = (active + by + shown.length) % shown.length;
      Array.prototype.forEach.call(list.children, function (li, i) { li.classList.toggle("active", i === active); });
      input.setAttribute("aria-activedescendant", list.children[active].id);
      list.children[active].scrollIntoView({ block: "nearest" });
    }

    input.addEventListener("click", function () { if (!open) setOpen(true, true); });
    input.addEventListener("input", function (e) { if (e.isTrusted) setOpen(true, false); });
    input.addEventListener("blur", function () { setOpen(false); });
    arrow.addEventListener("mousedown", function (e) { e.preventDefault(); });
    arrow.addEventListener("click", function () { input.focus(); setOpen(!open, true); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!open) setOpen(true, true);
        move(e.key === "ArrowDown" ? 1 : -1);
      } else if (e.key === "Enter" && open && active >= 0) {
        e.preventDefault(); pick(shown[active]);
      } else if (e.key === "Escape" && open) {
        e.preventDefault(); setOpen(false);
      }
    });
    return { render: render };
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
  ["dev_branch", "base_branch"].forEach(function (n) { combos.push(combo(field(n))); });
  ["dev_branch", "base_branch", "dev_repo"].forEach(function (n) { field(n).addEventListener("input", lookup); });
  ["name", "dev_tc", "dev_options", "bounds", "max_games", "test_mode", "dev_repo"].forEach(function (n) { field(n).addEventListener("input", summarize); });
  field("engine").addEventListener("change", function () {
    var engine = engines[field("engine").value];
    if (engine) { field("dev_repo").value = engine.source; field("base_repo").value = engine.source; field("scale_nps").value = engine.nps; }
    applyPreset();
    fillBranches();
  });
  field("dev_repo").addEventListener("change", fillBranches);
  summarize();
  fillBranches();
  if (field("dev_branch").value.trim()) lookup();
})();
