// The Create tune page. The SPSA input textarea fills a table of its parameters, each in one of three states: Tune (nudged by
// SPSA, its "Start / frozen at" the value it starts from), Freeze (both sides play the value in that box, which differs from the
// default) or Default (not sent at all, the engine plays its own). Freezing at the default value is Default. The form still works
// without this: the textarea is what is sent, and a plain field takes the frozen ones. With it, each frozen parameter is sent as
// a hidden pair and each default one as a hidden name.
(function () {
  "use strict";
  var form = document.getElementById("tune");
  if (!form) return;
  var area = document.getElementById("f-spsa_inputs");
  var body = document.getElementById("param-rows");
  var tableBox = document.getElementById("param-table");
  var counts = document.getElementById("counts");
  var hidden = document.getElementById("frozen-inputs");
  var state = {};    // name -> { mode: "tune" | "freeze" | "default", at: string }
  var MODES = [["tune", "Tune"], ["freeze", "Freeze"], ["default", "Default"]];
  var rows = [];

  try {
    var posted = JSON.parse(form.dataset.frozen || "{}");
    Object.keys(posted).forEach(function (name) { state[name] = { mode: "freeze", at: posted[name] }; });
    JSON.parse(form.dataset.default || "[]").forEach(function (name) { state[name] = { mode: "default", at: "" }; });
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

  // what a row is in the end: a frozen value equal to the default is the default
  function effective(row, s) {
    return s.mode === "freeze" && Number(s.at) === Number(row.value) ? "default" : s.mode;
  }

  function render() {
    body.textContent = "";
    rows.forEach(function (row) {
      var s = state[row.name] || (state[row.name] = { mode: "tune", at: row.value });
      if (s.at === "" || s.at === undefined) s.at = row.value;
      var tr = document.createElement("tr");
      var tag = document.createElement("span");
      var choice = document.createElement("span");
      choice.className = "modes"; choice.setAttribute("role", "group"); choice.setAttribute("aria-label", row.name);
      var buttons = {};
      MODES.forEach(function (m) {
        var b = document.createElement("button");
        b.type = "button"; b.className = "mode " + m[0]; b.textContent = m[1];
        b.addEventListener("click", function () { s.mode = m[0]; render(); });
        buttons[m[0]] = b;
        choice.appendChild(b);
      });
      var at = document.createElement("input");
      at.className = "at"; at.value = s.at;
      function paint() {
        var mode = effective(row, s);
        tr.className = mode === "tune" ? "" : mode;
        MODES.forEach(function (m) { buttons[m[0]].setAttribute("aria-pressed", String(s.mode === m[0])); });
        at.disabled = s.mode === "default";
        at.setAttribute("aria-label", row.name + (s.mode === "tune" ? " starts at" : " frozen at"));
        tag.textContent = s.mode === "freeze" && mode === "default" ? "the default, so not sent" : mode === "default" ? "the engine's own" : "";
        tag.className = "tag";
      }
      at.addEventListener("input", function () { s.at = at.value; paint(); sync(); });
      function cell(node, cls) {
        var td = document.createElement("td");
        if (cls) td.className = cls;
        if (typeof node === "string") td.textContent = node; else td.appendChild(node);
        tr.appendChild(td);
        return td;
      }
      cell(choice);
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
    // the frozen ones go as hidden pairs and the default ones as hidden names; counts and the games a tune plays follow
    hidden.textContent = "";
    var n = { tune: 0, freeze: 0, "default": 0 };
    function add(name, value) {
      var input = document.createElement("input");
      input.type = "hidden"; input.name = name; input.value = value;
      hidden.appendChild(input);
    }
    rows.forEach(function (row) {
      var mode = effective(row, state[row.name]);
      n[mode]++;
      if (mode === "freeze") { add("spsa_frozen", row.name); add("spsa_frozen_value_" + row.name, state[row.name].at); }
      if (mode === "default") add("spsa_default", row.name);
    });
    document.getElementById("n-tuned").textContent = n.tune;
    document.getElementById("n-frozen").textContent = n.freeze;
    document.getElementById("n-default").textContent = n["default"];
    summarize(n.tune, n.freeze, n["default"]);
  }

  function num(id, fallback) { var n = parseInt((document.getElementById(id) || {}).value, 10); return isNaN(n) ? fallback : n; }

  function summarize(tuned, frozen, dflt) {
    var iterations = num("f-spsa_iterations", 0), pairs = num("f-spsa_pairs_per", 0), games = num("f-spsa_check_games", 500);
    var everyText = ((document.getElementById("f-spsa_check_every") || {}).value || "").trim();
    var every = everyText === "" ? Math.floor(iterations / 20) : num("f-spsa_check_every", 0);
    var played = 2 * pairs * iterations, checks = every > 0 ? Math.floor(iterations / every) * games : 0;
    var text = tuned + " tuned, " + frozen + " frozen, " + dflt + " default";
    if (played) text += " · about " + (played + checks).toLocaleString("en-US") + " games" + (checks ? " with the checks" : "");
    document.getElementById("summary").textContent = text;
  }

  function setAll(mode) {
    rows.forEach(function (row) { (state[row.name] = state[row.name] || { at: row.value }).mode = mode; });
    render();
  }

  area.addEventListener("input", function () { parse(); render(); });
  document.getElementById("tune-all").addEventListener("click", function (e) { e.preventDefault(); setAll("tune"); });
  document.getElementById("default-all").addEventListener("click", function (e) { e.preventDefault(); setAll("default"); });
  ["f-spsa_iterations", "f-spsa_pairs_per", "f-spsa_check_every", "f-spsa_check_games"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("input", sync);
  });

  // a tuned parameter's start is what was edited in the table: written back into the line that is sent
  form.addEventListener("submit", function () {
    var lines = area.value.split(/\r?\n/);
    rows.forEach(function (row) {
      var s = state[row.name];
      if (s.mode !== "tune" || s.at === row.value) return;
      var i = lines.indexOf(row.line);
      if (i >= 0) {
        var p = lines[i].split(",").map(function (x) { return x.trim(); });
        p[2] = s.at;
        lines[i] = p.join(", ");
      }
    });
    area.value = lines.join("\n");
  });

  // The engine's parameters, read from the engine at the commit the branch names: a worker builds it with the engine's tune flags
  // and asks it. The list is cached for good per commit, so a commit read before fills the table at once; otherwise the job is
  // queued (once per commit) and this asks until it is done or failed. What is pasted still overrides.
  var statusBox = document.getElementById("read-status");
  var engineBox = document.getElementById("f-engine");
  var branchBox = document.getElementById("f-dev_branch");
  var filled = null;       // the text this put in the box, so that a list the person pasted or edited is never replaced
  var wanted = 0;          // which question is current: an answer to an older one is dropped
  var timer = null, typing = null;

  function say(nodes, bad) {
    statusBox.textContent = "";
    [].concat(nodes).forEach(function (n) { statusBox.appendChild(typeof n === "string" ? document.createTextNode(n) : n); });
    statusBox.className = "read-status" + (bad ? " bad" : "");
    statusBox.hidden = false;
  }

  function code(text) { var c = document.createElement("code"); c.textContent = text; return c; }

  function post(url, data) {
    var body = new URLSearchParams(data);
    body.append("csrfmiddlewaretoken", form.querySelector("[name=csrfmiddlewaretoken]").value);
    return fetch(url, { method: "POST", body: body, credentials: "same-origin" }).then(function (r) { return r.json(); });
  }

  function get(params) {
    return fetch(form.dataset.stateUrl + "?" + new URLSearchParams(params), { credentials: "same-origin" }).then(function (r) { return r.json(); });
  }

  function fill(answer) {
    var mine = area.value.trim() === "" || (filled !== null && area.value === filled);
    if (!mine) {
      say(["The parameters of ", code(answer.short), " are read, but the box holds a list of your own, which is kept. Clear it to use the engine's."]);
      return;
    }
    filled = answer.lines;
    area.value = answer.lines;
    parse(); render();
    var nodes = ["Read from ", code(answer.short), " by the engine's ", code(answer.command), " answer."];
    if (answer.command === "uci") nodes.push(" It has no spsa command, so the step at the end (c_end) is a guess: a twentieth of each range.");
    say(nodes);
  }

  function failed(answer) {
    var retry = document.createElement("a");
    retry.href = "#"; retry.textContent = "read again";
    retry.addEventListener("click", function (e) { e.preventDefault(); ask(true); });
    var log = document.createElement("pre");
    log.className = "log"; log.textContent = answer.error || "";
    say(["Reading the parameters from ", code(answer.short), " failed. Paste them below instead, or ", retry, ".", log], true);
  }

  function follow(answer, token) {
    if (token !== wanted) return;
    if (answer.error) { say([answer.error], true); return; }
    if (answer.state === "done") { fill(answer); return; }
    if (answer.state === "failed") { failed(answer); return; }
    var where = answer.state === "reading" && answer.worker ? " on " + answer.worker : "";
    say(["Reading the parameters from ", code(answer.short), "\u2026" + where + (answer.detail ? " (" + answer.detail + ")" : "")]);
    clearTimeout(timer);
    timer = setTimeout(function () {
      get({ engine: answer.engine, sha: answer.sha }).then(function (next) {
        next.short = next.short || answer.short; next.engine = answer.engine; next.sha = answer.sha;
        follow(next, token);
      }).catch(function () { follow(answer, token); });
    }, 3000);
  }

  function ask(retry) {
    var ref = branchBox ? branchBox.value.trim() : "";
    clearTimeout(timer);
    if (!engineBox || !ref) { statusBox.hidden = true; return; }
    var token = ++wanted;
    say(["Looking up ", code(ref), "\u2026"]);
    get({ engine: engineBox.value, ref: ref }).then(function (answer) {
      if (token !== wanted) return;
      // read already, or going on, or a failure nobody asked to read again: the cache's word
      if (answer.error || (answer.state !== "none" && !(answer.state === "failed" && retry))) return follow(answer, token);
      post(form.dataset.readUrl, { engine: engineBox.value, sha: answer.sha, ref: ref, retry: retry ? "1" : "" }).then(function (queued) {
        follow(queued, token);
      });
    }).catch(function () { say(["The parameters could not be asked for just now."], true); });
  }

  if (branchBox && engineBox) {
    branchBox.addEventListener("input", function () { clearTimeout(typing); typing = setTimeout(function () { ask(false); }, 700); });
    branchBox.addEventListener("change", function () { clearTimeout(typing); ask(false); });
    engineBox.addEventListener("change", function () { ask(false); });
    if (branchBox.value.trim() && area.value.trim() === "") ask(false);
  }

  parse();
  render();
})();
