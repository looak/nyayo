// The Create tune page. The SPSA input textarea fills a table of its parameters, each with a Tune tickbox; an unticked one is
// frozen, and its "Start / frozen at" is the value both sides play. The form still works without this: the textarea is
// what is sent, and a plain field takes the frozen ones. With it, each frozen parameter is sent as a hidden pair.
(function () {
  "use strict";
  var form = document.getElementById("tune");
  if (!form) return;
  var area = document.getElementById("f-spsa_inputs");
  var body = document.getElementById("param-rows");
  var tableBox = document.getElementById("param-table");
  var counts = document.getElementById("counts");
  var hidden = document.getElementById("frozen-inputs");
  var state = {};    // name -> { frozen: bool, at: string }
  var rows = [];

  try {
    var posted = JSON.parse(form.dataset.frozen || "{}");
    Object.keys(posted).forEach(function (name) { state[name] = { frozen: true, at: posted[name] }; });
  } catch (e) { /* nothing posted before */ }

  function parse() {
    rows = [];
    area.value.split(/\r?\n/).forEach(function (line) {
      var p = line.split(",").map(function (x) { return x.trim(); });
      if (p.length !== 7 || !p[0]) return;
      rows.push({ line: line, name: p[0], type: p[1], value: p[2], min: p[3], max: p[4], c: p[5], r: p[6] });
    });
  }

  function minus(text) { return String(text).replace("-", "−"); }

  function render() {
    body.textContent = "";
    rows.forEach(function (row) {
      var s = state[row.name] || (state[row.name] = { frozen: false, at: row.value });
      if (s.at === "" || s.at === undefined) s.at = row.value;
      var tr = document.createElement("tr");
      var tag = document.createElement("span");
      var box = document.createElement("input");
      box.type = "checkbox"; box.checked = !s.frozen; box.setAttribute("aria-label", "Tune " + row.name);
      box.addEventListener("change", function () { s.frozen = !box.checked; render(); });
      var at = document.createElement("input");
      at.className = "at"; at.value = s.at; at.setAttribute("aria-label", row.name + (s.frozen ? " frozen at" : " starts at"));
      function paint() {
        tr.className = s.frozen ? "frozen" : "";
        var off = s.frozen && Number(s.at) !== Number(row.value);
        at.className = "at" + (off ? " off" : "");
        tag.textContent = s.frozen ? (off ? "frozen, not at its default" : "frozen") : "";
        tag.className = "tag" + (off ? " off" : "");
      }
      at.addEventListener("input", function () { s.at = at.value; paint(); sync(); });
      function cell(node, cls) {
        var td = document.createElement("td");
        if (cls) td.className = cls;
        if (typeof node === "string") td.textContent = node; else td.appendChild(node);
        tr.appendChild(td);
        return td;
      }
      cell(box);
      cell(row.name, "name").appendChild(tag);
      cell(minus(row.value), "num"); cell(at, "num"); cell(minus(row.min), "num"); cell(minus(row.max), "num");
      cell(row.c, "num"); cell(row.r, "num");
      paint();
      body.appendChild(tr);
    });
    tableBox.hidden = counts.hidden = rows.length === 0;
    sync();
  }

  function sync() {
    // the frozen ones go as hidden pairs; counts and the games a tune plays follow
    hidden.textContent = "";
    var frozen = 0;
    rows.forEach(function (row) {
      var s = state[row.name];
      if (!s.frozen) return;
      frozen++;
      [["spsa_frozen", row.name], ["spsa_frozen_value_" + row.name, s.at]].forEach(function (pair) {
        var input = document.createElement("input");
        input.type = "hidden"; input.name = pair[0]; input.value = pair[1];
        hidden.appendChild(input);
      });
    });
    document.getElementById("n-tuned").textContent = rows.length - frozen;
    document.getElementById("n-frozen").textContent = frozen;
    summarize(rows.length - frozen, frozen);
  }

  function num(id, fallback) { var n = parseInt((document.getElementById(id) || {}).value, 10); return isNaN(n) ? fallback : n; }

  function summarize(tuned, frozen) {
    var iterations = num("f-spsa_iterations", 0), pairs = num("f-spsa_pairs_per", 0), games = num("f-spsa_check_games", 500);
    var everyText = ((document.getElementById("f-spsa_check_every") || {}).value || "").trim();
    var every = everyText === "" ? Math.floor(iterations / 20) : num("f-spsa_check_every", 0);
    var played = 2 * pairs * iterations, checks = every > 0 ? Math.floor(iterations / every) * games : 0;
    var text = tuned + " tuned, " + frozen + " frozen";
    if (played) text += " · about " + (played + checks).toLocaleString("en-US") + " games" + (checks ? " with the checks" : "");
    document.getElementById("summary").textContent = text;
  }

  function setAll(frozen) {
    rows.forEach(function (row) { (state[row.name] = state[row.name] || { at: row.value }).frozen = frozen; });
    render();
  }

  area.addEventListener("input", function () { parse(); render(); });
  document.getElementById("tune-all").addEventListener("click", function (e) { e.preventDefault(); setAll(false); });
  document.getElementById("freeze-all").addEventListener("click", function (e) { e.preventDefault(); setAll(true); });
  ["f-spsa_iterations", "f-spsa_pairs_per", "f-spsa_check_every", "f-spsa_check_games"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("input", sync);
  });

  // a tuned parameter's start is what was edited in the table: written back into the line that is sent
  form.addEventListener("submit", function () {
    var lines = area.value.split(/\r?\n/);
    rows.forEach(function (row) {
      var s = state[row.name];
      if (s.frozen || s.at === row.value) return;
      var i = lines.indexOf(row.line);
      if (i >= 0) {
        var p = lines[i].split(",").map(function (x) { return x.trim(); });
        p[2] = s.at;
        lines[i] = p.join(", ");
      }
    });
    area.value = lines.join("\n");
  });

  parse();
  render();
})();
