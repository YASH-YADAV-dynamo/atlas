"use strict";
// Atlas Playground. Relative URLs only. Contracts used: GET demo/config, demo/presets, demo/map;
// POST demo/run (SSE: queued/started/step/result/error). Keys are handled by chrome.js (memory only).
(function () {
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  let presets = [];
  let mapView = { states: [], transitions: [] };
  let runSteps = [];
  let running = false;
  let timer = null;
  let t0 = 0;
  let baseEdges = new Set();
  const seenNodes = new Set(), seenEdges = new Set();
  let initialDrawn = false;
  const layoutPos = new Map();
  let budget = "ok";
  let runs = 0; // finished runs this page view
  let modalShown = false;
  let cur = null, lastRun = null, tourHl = null, tourSteps = [], tourI = 0, tourFrom = null;

  function mk(tag, cls, txt) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt !== undefined) e.textContent = txt;
    return e;
  }
  function clip(t, n) { return t.length > n ? t.slice(0, n - 1) + "…" : t; }
  function actionName(a) {
    return a.kind === "goto" ? "goto " + (a.value || "") : a.kind + (a.locator ? " " + a.locator.name : "");
  }
  function actionSummary(a) {
    if (a.kind === "goto") return "goto " + (a.value || "");
    let s = a.kind + (a.locator ? " " + a.locator.role + " '" + a.locator.name + "'" : "");
    if (a.param) s += " <" + a.param + ">";
    else if (a.value) s += " = " + a.value;
    return s;
  }

  // ---- graph: small force simulation, no library -------------------------------------
  function el(name, attrs, text) {
    const e = document.createElementNS(NS, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    return e;
  }
  const VW = 1000, VH = 600;

  function simulate(ids, links) {
    for (const id of ids) {
      if (!layoutPos.has(id)) layoutPos.set(id, { x: VW / 2 + (Math.random() - 0.5) * 300, y: VH / 2 + (Math.random() - 0.5) * 200, vx: 0, vy: 0 });
    }
    const n = ids.length;
    const k = Math.sqrt((VW * VH) / Math.max(n, 1)) * 0.6;
    for (let it = 0; it < 300; it++) {
      const cool = 1 - it / 300;
      for (let i = 0; i < n; i++) {
        const a = layoutPos.get(ids[i]);
        for (let j = i + 1; j < n; j++) {
          const b = layoutPos.get(ids[j]);
          let dx = a.x - b.x, dy = a.y - b.y;
          const d2 = dx * dx + dy * dy || 0.01, d = Math.sqrt(d2);
          const f = (k * k) / d2;
          dx /= d; dy /= d;
          a.vx += dx * f * 4; a.vy += dy * f * 4; b.vx -= dx * f * 4; b.vy -= dy * f * 4;
        }
      }
      for (const [s, t] of links) {
        const a = layoutPos.get(s), b = layoutPos.get(t);
        const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
        const f = (d - k) * 0.05;
        a.vx += (dx / d) * f; a.vy += (dy / d) * f; b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
      }
      for (const id of ids) {
        const p = layoutPos.get(id);
        p.vx += (VW / 2 - p.x) * 0.004; p.vy += (VH / 2 - p.y) * 0.004;
        const sp = Math.hypot(p.vx, p.vy) || 1, cap = 30 * cool + 1;
        if (sp > cap) { p.vx = (p.vx / sp) * cap; p.vy = (p.vy / sp) * cap; }
        p.x += p.vx; p.y += p.vy; p.vx *= 0.6; p.vy *= 0.6;
      }
    }
    // fit into the view box with padding
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const id of ids) { const p = layoutPos.get(id); x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); y0 = Math.min(y0, p.y); y1 = Math.max(y1, p.y); }
    const pad = 70, sx = (VW - 2 * pad) / Math.max(x1 - x0, 1), sy = (VH - 2 * pad) / Math.max(y1 - y0, 1);
    const sc = Math.min(sx, sy, 2.2);
    const out = new Map();
    for (const id of ids) {
      const p = layoutPos.get(id);
      out.set(id, { x: VW / 2 + (p.x - (x0 + x1) / 2) * sc, y: VH / 2 + (p.y - (y0 + y1) / 2) * sc });
    }
    return out;
  }

  function edgeKeys() {
    const out = [];
    for (const t of mapView.transitions) out.push(t.from_state + ">" + t.to_state);
    return out;
  }
  function reliability(ok, fail) { return ok + fail === 0 ? 1 : ok / (ok + fail); }

  function drawMap() {
    const svg = $("graph");
    svg.textContent = "";
    const { states, transitions } = mapView;
    $("mapinfo").textContent = states.length ? "· " + states.length + " pages, " + transitions.length + " actions" : "";
    $("map-empty").hidden = states.length > 0;
    $("shared").textContent = states.length ? "Shared map: " + states.length + " pages, " + transitions.length + " roads, learned by every visitor so far." : "Shared map: no pages yet. Every visitor adds to it.";
    if (!states.length) return;
    svg.setAttribute("viewBox", "0 0 " + VW + " " + VH);
    const ids = states.map((s) => s.id);
    const idset = new Set(ids);
    // merge parallel transitions into one edge per ordered pair
    const edges = new Map();
    for (const t of transitions) {
      if (!idset.has(t.from_state) || !idset.has(t.to_state)) continue;
      const key = t.from_state + ">" + t.to_state;
      const e = edges.get(key) || { from: t.from_state, to: t.to_state, ok: 0, fail: 0, names: [] };
      e.ok += t.ok; e.fail += t.fail; e.names.push(actionName(t.action));
      edges.set(key, e);
    }
    const pos = simulate(ids, [...edges.values()].filter((e) => e.from !== e.to).map((e) => [e.from, e.to]));

    const use = new Map(), nodeUse = new Map();
    const rank = (c) => ({ failed: 3, model: 2, path: 1 }[c] || 0);
    for (const s of runSteps) {
      const cls = !s.ok ? "failed" : s.source === "model" ? "model" : "path";
      if (s.to_state != null) {
        const k = s.from_state + ">" + s.to_state;
        if (rank(cls) > rank(use.get(k))) use.set(k, cls);
      }
      for (const nid of [s.from_state, s.to_state]) if (nid != null && cls !== "failed" && rank(cls) > rank(nodeUse.get(nid))) nodeUse.set(nid, cls);
    }
    const defs = el("defs", {});
    for (const c of ["good", "warn", "bad", "idle", "path", "model", "failed"]) {
      const m = el("marker", { id: "ar-" + c, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto" });
      const col = { good: "--good", warn: "--model", bad: "--bad", idle: "--idle", path: "--map", model: "--model", failed: "--bad" }[c];
      m.appendChild(el("path", { d: "M0 0L10 5L0 10z", fill: "var(" + col + ")" }));
      defs.appendChild(m);
    }
    svg.appendChild(defs);
    const R = 14;
    const drawn = [];
    for (const [key, e] of edges) {
      const a = pos.get(e.from), b = pos.get(e.to);
      const r = reliability(e.ok, e.fail);
      const cls = use.get(key) || (e.ok + e.fail === 0 ? "idle" : r >= 0.9 ? "good" : r >= 0.6 ? "warn" : "bad");
      const g = el("g", {});
      g.appendChild(el("title", {}, e.names.join(", ") + " (" + e.ok + " ok, " + e.fail + " failed)"));
      let d;
      if (e.from === e.to) {
        d = "M" + (a.x + 4) + " " + (a.y - R) + "C" + (a.x - 30) + " " + (a.y - 70) + " " + (a.x + 40) + " " + (a.y - 70) + " " + (a.x + 8) + " " + (a.y - R - 2);
      } else {
        const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
        const rev = edges.has(e.to + ">" + e.from) ? 18 : 6;
        const mx = (a.x + b.x) / 2 - (dy / len) * rev, my = (a.y + b.y) / 2 + (dx / len) * rev;
        const ux = (b.x - mx), uy = (b.y - my), ul = Math.hypot(ux, uy) || 1;
        const vx = (a.x - mx), vy = (a.y - my), vl = Math.hypot(vx, vy) || 1;
        d = "M" + (a.x - (vx / vl) * R) + " " + (a.y - (vy / vl) * R) + "Q" + mx + " " + my + " " + (b.x + (-ux / ul) * R) + " " + (b.y + (-uy / ul) * R);
      }
      const learned = (running && !baseEdges.has(key)) || cls === "model";
      const fresh = initialDrawn && !seenEdges.has(key);
      seenEdges.add(key);
      g.appendChild(el("path", { d, style: "--len:600", "data-key": key, class: "edge " + cls + (learned ? " learned" : "") + (fresh ? " enter" : ""), "marker-end": "url(#ar-" + cls + ")" }));
      drawn.push([cls, g]);
    }
    drawn.sort((x, y) => rank(x[0]) - rank(y[0])); // highlighted edges on top
    for (const [, g] of drawn) svg.appendChild(g);
    for (const s of states) {
      const p = pos.get(s.id);
      const g = el("g", { "data-id": s.id, class: "node " + (nodeUse.get(s.id) || "") + (initialDrawn && !seenNodes.has(s.id) ? " enter" : ""), transform: "translate(" + p.x + "," + p.y + ")" });
      g.appendChild(el("title", {}, s.url_tmpl + (s.title ? " - " + s.title : "")));
      seenNodes.add(s.id);
      g.appendChild(el("circle", { r: R }));
      g.appendChild(el("text", { x: 0, y: R + 14, "text-anchor": "middle" }, clip(s.url_tmpl, 22)));
      svg.appendChild(g);
    }
    if (tourOpen()) applyHl(tourHl);
  }

  async function refreshMap() {
    try {
      const r = await fetch("demo/map");
      if (!r.ok) {
        if (!mapView.states.length) $("map-empty").innerHTML = "<b>Map unavailable.</b><span>The server did not return a map (status " + r.status + ").</span>";
        return false;
      }
      const m = await r.json();
      mapView = { states: m.states || [], transitions: m.transitions || [] };
      drawMap();
      initialDrawn = true;
      return true;
    } catch (_) { $("shared").textContent = "The map could not be refreshed. It will update after the next run."; return false; }
  }


  // ---- run UI ---------------------------------------------------------------------
  function note(text, kind, withActions) {
    const n = $("notice");
    n.replaceChildren(mk("span", "", text));
    n.className = "notice" + (kind ? " " + kind : "");
    if (withActions) {
      const c = mk("button", "linkbtn", "Connect your agent");
      c.type = "button"; c.setAttribute("data-connect", "");
      n.append(" ", c);
    }
    n.hidden = !text;
  }
  function setStatus(text, state) {
    $("status-text").textContent = text;
    $("status-dot").className = "dot" + (state === "run" ? "" : state === "err" ? " err" : " off");
  }
  function updateCounters() {
    $("c-model").textContent = String(runSteps.filter((s) => s.source === "model").length);
    $("c-map").textContent = String(runSteps.filter((s) => s.source === "map").length);
  }
  function tick() { $("c-secs").textContent = ((performance.now() - t0) / 1000).toFixed(1); }
  function addStepRow(s) {
    const li = mk("li", s.ok ? "" : "fail");
    li.dataset.i = String(s.index);
    li.append(mk("span", "ms", "#" + s.index), mk("span", "badge " + s.source, s.source === "map" ? "MAP" : "MODEL"),
      mk("span", "act", actionSummary(s.action) + (s.ok ? "" : " (failed)")), mk("span", "ms", s.ms + " ms"));
    $("steps").appendChild(li);
    $("steps-empty").hidden = true;
    li.scrollIntoView({ block: "nearest" });
  }
  function setRunning(on) {
    running = on;
    $("start").disabled = on;
    $("replay").disabled = on;
  }

  function pickTask(replay) {
    const a = presets.find((p) => p.template_id === "refund_order") || presets[0];
    if (!a) return null;
    if (!replay) return a;
    return presets.find((p) => p.template_id === a.template_id && p.goal_text !== a.goal_text) || a;
  }

  async function finishRun(opened) {
    clearInterval(timer);
    setRunning(false);
    const mapOk = await refreshMap();
    runs++;
    $("replay").hidden = false;
    $("done-actions").hidden = false;
    if (cur && (opened || cur.busy)) {
      cur.after = mapOk ? snapMap() : null;
      lastRun = cur;
      openTour(cur.compare ? "compare" : 0);
    }
  }

  function handleEvent(ev, data) {
    if (ev === "queued") setStatus(data.position > 0 ? "Waiting in line: position " + data.position : "Queued.", "run");
    else if (ev === "started") setStatus("Working on it...", "run");
    else if (ev === "step") { runSteps.push(data); addStepRow(data); updateCounters(); refreshMap(); }
    else if (ev === "result") {
      runSteps = data.steps.slice();
      if (cur) cur.result = data;
      updateCounters();
      $("c-secs").textContent = (data.wall_ms / 1000).toFixed(1);
      const why = { explore_limit: "this task is new and live exploration is not available right now", budget_exceeded: "the live model budget is used up", step_limit: "the step limit was reached" }[data.stop_reason];
      if (data.success) setStatus("Finished: " + data.steps.length + " steps, " + data.model_calls + " model call" + (data.model_calls === 1 ? "" : "s") + ".", "off");
      else setStatus("The task did not complete" + (why ? ": " + why : "") + ".", "err");
      if (!data.success && why) note(why.charAt(0).toUpperCase() + why.slice(1) + ". You can still replay a map-only run, which costs nothing.", "warn", true);
      refreshMap();
    } else if (ev === "error") {
      const m = data && data.message ? data.message : "The run failed.";
      if (cur) cur.err = m;
      setStatus(m, "err");
      note(m + " A map-only replay costs nothing.", "warn", true);
    }
  }

  async function startRun(replay, compare) {
    if (running) return;
    const task = pickTask(replay);
    if (!task) { note("The sandbox has no tasks available right now. Try again shortly.", "warn"); return; }
    note("", "");
    $("steps").replaceChildren();
    $("steps-empty").hidden = false;
    $("goal-line").textContent = task.goal_text;
    $("goal-line").hidden = false;
    runSteps = [];
    updateCounters();
    $("c-secs").textContent = "0.0";
    cur = { goal: task.goal_text, before: snapMap(), after: null, result: null, err: null, busy: false, compare: compare || null };
    baseEdges = new Set(mapView.transitions.map((t) => t.from_state + ">" + t.to_state));
    setRunning(true);
    setStatus("Connecting...", "run");
    t0 = performance.now();
    clearInterval(timer);
    timer = setInterval(tick, 200);
    let opened = false;
    try {
      const r = await fetch("demo/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ goal_text: task.goal_text, params: task.params }) });
      if (r.status === 429) {
        cur.busy = true;
        setStatus("The sandbox is busy.", "err");
        note("The sandbox is busy or this visit's live allowance is used up. Wait a few seconds and try again, or replay a map-only run, which costs nothing.", "warn", true);
        $("replay").hidden = false;
        return;
      }
      if (!r.ok) { setStatus("The run did not start (error " + r.status + ").", "err"); note("The run did not start. Try again in a moment.", "warn"); return; }
      opened = true;
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, i);
          buf = buf.slice(i + 2);
          let ev = "message", data = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) ev = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
          }
          if (data) handleEvent(ev, JSON.parse(data));
        }
      }
    } catch (_) {
      opened = true;
      if (cur && !cur.result && !cur.err) cur.err = "The connection dropped before the run finished.";
      setStatus("The connection dropped before the run finished.", "err");
      note("The connection dropped before the run finished. A map-only replay costs nothing.", "warn", true);
    } finally {
      finishRun(opened);
    }
  }

  // ---- guided tour of the run that just finished ------------------------------------
  // Every sentence below is built from the run's own events and the before/after map; nothing is estimated.
  function transKey(t) { return t.from_state + ">" + t.to_state + "|" + JSON.stringify(t.action); }
  function snapMap() {
    return {
      nodes: new Set(mapView.states.map((x) => x.id)),
      labels: new Map(mapView.states.map((x) => [x.id, x.url_tmpl])),
      trans: new Set(mapView.transitions.map(transKey)),
      nodeN: mapView.states.length, transN: mapView.transitions.length,
    };
  }
  function tourOpen() { return !$("tour").hidden; }
  const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");
  const rangeTxt = (a, b) => (a === b ? "Step #" + a : "Steps #" + a + " to #" + b);
  const stopWhy = { explore_limit: "live exploration was not available for this new task", budget_exceeded: "the live model budget ran out", step_limit: "the step limit was reached" };

  function runFacts(run) {
    const steps = run.result ? run.result.steps : runSteps;
    const mapSteps = steps.filter((x) => x.source === "map");
    const modelSteps = steps.filter((x) => x.source === "model");
    const calls = run.result ? run.result.model_calls : modelSteps.length;
    return { steps, mapSteps, modelSteps, calls };
  }
  function segments(steps) {
    const out = [];
    for (const x of steps) {
      const last = out[out.length - 1];
      if (last && last.src === x.source) last.to = x.index; else out.push({ src: x.source, from: x.index, to: x.index });
    }
    return out;
  }
  function P(text, cls) { return mk("p", cls || "", text); }
  function chips(steps) {
    const ul = mk("ul", "chips");
    ul.setAttribute("aria-label", "Route steps");
    for (const x of steps.slice(0, 8)) ul.appendChild(mk("li", "", actionSummary(x.action)));
    if (steps.length > 8) ul.appendChild(mk("li", "", "+" + (steps.length - 8) + " more"));
    return ul;
  }

  function buildTour(run) {
    const f = runFacts(run), T = [], after = run.after, before = run.before;
    const first = f.steps[0];
    const label = (id) => (after && after.labels.get(id)) || before.labels.get(id) || "";
    T.push({ title: "You asked", hl: { els: ["goal-line"] }, body: () => [P("“" + run.goal + "”"),
      P(run.busy ? "The sandbox declined the live run, so nothing was executed for this request." : "This tour follows what Atlas did for this one request, using the numbers from the run itself.", "dim")] });
    if (run.busy) {
      T.push({ title: "Nothing ran", hl: { els: ["status-text"] }, body: () => [P("The sandbox was busy or this visit’s live allowance was used up, so it answered before any step ran. The map did not change and no model call was made."),
        P("A map-only replay of a known task needs no model call and is still available.", "dim")] });
    } else if (!first) {
      T.push({ title: "The run ended early", hl: { els: ["status-text"] }, body: () => [P(run.err || (run.result && stopWhy[run.result.stop_reason] ? "It stopped because " + stopWhy[run.result.stop_reason] + "." : "No step was executed.")),
        P("Because no step ran, there was no route to follow and nothing to add to the map.", "dim")] });
    } else {
      const known = before.nodes.has(first.from_state), nm = label(first.from_state);
      T.push({ title: "Where your agent was", hl: { nodes: [first.from_state], rows: [first.index] }, body: () => known
        ? [P("Atlas looked up where your agent was. It matched the page state " + (nm || "(unnamed)") + " in the shared map."), P("A known page means Atlas can look for a route from here instead of starting from scratch.", "dim")]
        : [P("Atlas looked up where your agent was and found no match: this page was not in the shared map yet. That makes it new territory."), P("The run begins by exploring, and the page is added to the map afterwards.", "dim")] });
      if (f.mapSteps.length) {
        T.push({ title: "One query, one route", hl: { rows: f.mapSteps.map((x) => x.index), edgesOf: f.mapSteps }, body: () => [
          P("One shortest-path query over the HydraDB graph returned a route. " + plural(f.mapSteps.length, "step") + " of it ran" + (run.result && run.result.replans ? ", after " + plural(run.result.replans, "replan") + " when a step did not land where expected" : "") + "."),
          chips(f.mapSteps), P("Each chip is an action the map already knew how to take.", "dim")] });
      } else {
        T.push({ title: "No route to follow", hl: { rows: [first.index] }, body: () => [P(known ? "Atlas knew the page but the graph held no route from it to this goal, so there was nothing to follow." : "With no known page to start from, the graph held no route for this goal."),
          P("The first step had to come from the model.", "dim")] });
      }
      T.push({ title: "Map steps and model steps", hl: { els: ["c-map", "c-model"], allRows: true }, body: () => {
        const out = [];
        for (const g of segments(f.steps)) {
          if (g.src === "map") out.push(P(rangeTxt(g.from, g.to) + " came from the map: no model call."));
          else if (g.from === g.to && f.modelSteps.length === 1 && f.calls === 1) out.push(P("Step #" + g.from + " needed the model because the map ended here. It cost one call."));
          else out.push(P(rangeTxt(g.from, g.to) + " needed the model because the map ended there."));
        }
        if (f.modelSteps.length > 1 || (f.modelSteps.length && f.calls !== 1)) out.push(P("Model calls in total: " + f.calls + "."));
        if (!f.modelSteps.length) out.push(P("Total: " + plural(f.calls, "model call") + "."));
        const bad = f.steps.filter((x) => !x.ok).length;
        if (bad) out.push(P(plural(bad, "step") + " did not work as expected; the graph records that against those actions.", "dim"));
        if (run.result && !run.result.success) out.push(P("The task did not complete" + (stopWhy[run.result.stop_reason] ? ": " + stopWhy[run.result.stop_reason] : "") + ". The run is not counted as a success."));
        if (run.err && !run.result) out.push(P("The run ended with an error before it reported totals: " + run.err));
        if (run.result && run.result.success && run.result.answer) out.push(P("It returned: " + run.result.answer, "dim"));
        return out;
      } });
    }
    if (!run.busy) {
      T.push({ title: "What got saved", saved: true, body: () => {
        if (!after) return [P("Atlas could not read the map after the run, so this tour cannot say what was added. Reload the page to see the current map.")];
        const nn = [...after.nodes].filter((id) => !before.nodes.has(id)).length;
        const ne = [...after.trans].filter((k) => !before.trans.has(k)).length;
        const out = [];
        if (!nn && !ne) out.push(P("Nothing new was added: the map already knew every page and action this run used. Known routes stay as they are."));
        else out.push(P("The shared map grew by " + plural(nn, "page") + " and " + plural(ne, "action") + ". The new ones are outlined in the graph."));
        out.push(P("Map before: " + plural(before.nodeN, "page") + ", " + plural(before.transN, "action") + ". Map now: " + plural(after.nodeN, "page") + ", " + plural(after.transN, "action") + ".", "dim"));
        if (run.result && !run.result.success) out.push(P("Even though the task did not finish, what the run did learn is kept.", "dim"));
        else if (run.err) out.push(P("Whatever was recorded before the error is kept.", "dim"));
        return out;
      } });
    }
    if (run.compare) {
      T.push({ title: "Run it again: the difference", hl: { els: ["c-map", "c-model"] }, cmp: true, body: () => {
        const c = run.compare;
        if (!run.result) return [P(run.busy ? "The sandbox was busy, so the map-only run did not happen. Nothing changed." : "The map-only run did not finish" + (run.err ? ": " + run.err : "") + ", so there is no comparison to show.")];
        const now = { calls: f.calls, steps: f.steps.length };
        const out = [mk("p", "big-num", plural(c.calls, "model call") + " → " + now.calls)];
        out.push(P("First run: " + plural(c.calls, "model call") + " across " + plural(c.steps, "step") + ". This run: " + plural(now.calls, "model call") + " across " + plural(now.steps, "step") + "."));
        if (c.calls === now.calls) out.push(P("The count did not drop this time" + (c.calls === 0 ? ": the first run already followed the map end to end." : ".") + (run.result.success ? "" : " This run also did not complete.")));
        else if (now.calls < c.calls) out.push(P("What the first run had to ask the model is now in the map, so this run followed it." + (run.result.success ? "" : " It still did not complete.")));
        if (c.goal !== run.goal) out.push(P("This was a different example of the same kind of task, run map-only: “" + run.goal + "”", "dim"));
        return out;
      } });
    } else if (presets.length > 0) {
      T.push({ title: "Run it again", rerun: true, body: () => [P("Run the same kind of task again, map-only, and compare. A map-only run makes no model call, so it costs nothing."),
        P("Numbers are compared only if the run finishes.", "dim")] });
    }
    T.push({ title: "Now plug in your own agent", connect: true, body: () => [P("Your agent can read and add to this same graph over MCP. The next screen shows how to connect it.")] });
    return T;
  }

  function clearHl() {
    document.querySelectorAll(".tour-on").forEach((e) => e.classList.remove("tour-on"));
    $("graphbox").classList.remove("tour-dim");
  }
  function applyHl(h) {
    clearHl();
    if (!h || !lastRun) return [];
    const els = [];
    for (const id of h.els || []) { const e = $(id); els.push(id.startsWith("c-") ? e.closest(".ctr") : e); }
    let rows = h.rows || [];
    if (h.allRows) rows = runFacts(lastRun).steps.map((x) => x.index);
    for (const i of rows) { const e = $("steps").querySelector('li[data-i="' + i + '"]'); if (e) els.push(e); }
    const nodeIds = new Set(h.nodes || []);
    const edgeKeys = new Set();
    for (const x of h.edgesOf || []) { if (x.to_state != null) { edgeKeys.add(x.from_state + ">" + x.to_state); nodeIds.add(x.from_state); nodeIds.add(x.to_state); } }
    if (h.newOnly && lastRun.after) {
      for (const id of lastRun.after.nodes) if (!lastRun.before.nodes.has(id)) nodeIds.add(id);
      for (const k of lastRun.after.trans) if (!lastRun.before.trans.has(k)) edgeKeys.add(k.split("|")[0]);
    }
    let graph = false;
    for (const n of $("graph").querySelectorAll(".node")) if (nodeIds.has(n.getAttribute("data-id"))) { els.push(n); graph = true; }
    for (const p of $("graph").querySelectorAll(".edge")) if (edgeKeys.has(p.getAttribute("data-key"))) { els.push(p); graph = true; }
    els.forEach((e) => e && e.classList.add("tour-on"));
    if (graph) $("graphbox").classList.add("tour-dim");
    return els;
  }

  function showTour(i) {
    if (i < 0) return;
    if (i >= tourSteps.length) { closeTour(true); return; }
    tourI = i;
    const t = tourSteps[i];
    tourHl = t.saved ? { newOnly: true } : t.hl || null;
    $("tour-title").textContent = t.title;
    $("tour-body").replaceChildren(...t.body());
    $("tour-count").textContent = "Step " + (i + 1) + " of " + tourSteps.length;
    $("tour-dots").replaceChildren(...tourSteps.map((_, k) => mk("i", k === i ? "on" : k < i ? "past" : "")));
    $("tour-back").disabled = i === 0;
    $("tour-next").textContent = t.connect ? "Open connect steps" : "Next";
    if (t.rerun) {
      const row = mk("div", "act-row"), b = mk("button", "btn primary", "Run it again, map only");
      b.type = "button";
      b.addEventListener("click", () => {
        const f = runFacts(lastRun);
        const base = { calls: f.calls, steps: f.steps.length, goal: lastRun.goal };
        closeTour(false);
        startRun(true, base);
      });
      row.appendChild(b);
      $("tour-body").appendChild(row);
    }
    const els = applyHl(tourHl);
    const target = els[0];
    if (target && target.scrollIntoView) {
      const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      try { target.scrollIntoView({ block: "center", behavior: calm ? "auto" : "smooth" }); } catch (_) { /* ignore */ }
    }
  }
  function openTour(start, from) {
    if (!lastRun) return;
    tourFrom = from || document.activeElement;
    tourSteps = buildTour(lastRun);
    const idx = start === "compare" ? tourSteps.findIndex((t) => t.cmp) : start;
    $("tour").hidden = false;
    showTour(Math.max(0, idx));
    $("tour-next").focus({ preventScroll: true });
  }
  // The connect modal opens once, and only after the tour ends or is skipped.
  function closeTour(thenConnect) {
    if (!tourOpen()) return;
    $("tour").hidden = true;
    tourHl = null;
    clearHl();
    if (thenConnect && !modalShown && window.AtlasConnect) {
      modalShown = true;
      window.AtlasConnect.open({ after: true });
      return;
    }
    const back = tourFrom && tourFrom.isConnected && tourFrom !== document.body && !tourFrom.hidden ? tourFrom : $("tour-replay");
    if (thenConnect && back && back.focus) back.focus({ preventScroll: true });
  }
  function tourKey(e) {
    if (!tourOpen()) return;
    if (e.key === "Escape") { e.preventDefault(); closeTour(true); }
    else if (e.key === "ArrowRight") { e.preventDefault(); showTour(tourI + 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); showTour(tourI - 1); }
    else if (e.key === "Tab") {
      const f = [...$("tour").querySelectorAll("button:not([disabled])")];
      if (!f.length) return;
      const a = document.activeElement;
      if (!$("tour").contains(a)) { e.preventDefault(); f[0].focus(); }
      else if (e.shiftKey && a === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && a === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    }
  }

  async function init() {
    $("start").addEventListener("click", () => startRun(false));
    $("replay").addEventListener("click", () => startRun(true));
    $("tour-replay").addEventListener("click", (e) => { if (lastRun) openTour(0, e.currentTarget); });
    $("tour-skip").addEventListener("click", () => closeTour(true));
    $("tour-back").addEventListener("click", () => showTour(tourI - 1));
    $("tour-next").addEventListener("click", () => showTour(tourI + 1));
    document.addEventListener("keydown", tourKey);
    try {
      const c = await (await fetch("demo/config")).json();
      budget = c.budget;
      if (c.budget === "exhausted") {
        note("Live runs are paused: the model budget is used up for now. Map-only replays still work and cost nothing.", "warn");
        $("replay").hidden = false;
      } else if (c.budget === "low") note("The live model budget is running low. Map-only replays cost nothing.", "warn");
    } catch (_) { note("Atlas could not be reached. Check your connection and reload.", "warn"); }
    try {
      const r = await fetch("demo/presets");
      if (r.ok) presets = await r.json();
    } catch (_) { /* tasks stay empty; handled on click */ }
    if (budget === "exhausted") { $("start").hidden = true; }
    refreshMap();
  }
  init();
})();
