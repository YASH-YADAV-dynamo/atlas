"use strict";
// Shared chrome: copy buttons and the "Connect your agent" modal, available on every page.
// Keys live in memory only (never localStorage, never URLs). Relative fetches only.
(function () {
  const DEF_URL = "https://atlas.yashdynamo.fun/mcp";
  const state = { url: DEF_URL, key: "", demo: "", priv: false, info: false, mode: "remote" };
  const listeners = [];
  const mk = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt !== undefined) e.textContent = txt; return e; };

  function copyText(text, btn) {
    const done = () => { const o = btn.dataset.l || btn.innerHTML; btn.dataset.l = o; btn.textContent = "Copied"; setTimeout(() => { btn.innerHTML = o; }, 1500); };
    const fail = () => { btn.textContent = "Press Ctrl+C"; };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fail);
    else {
      const t = mk("textarea"); t.value = text; document.body.appendChild(t); t.select();
      try { document.execCommand("copy"); done(); } catch (_) { fail(); }
      t.remove();
    }
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("button.copy[data-copy]");
    if (!b) return;
    const el = document.getElementById(b.dataset.copy);
    if (el) copyText(el.textContent, b);
  });

  const keyOrPlaceholder = () => state.key || "<your-key>";
  const origin = () => { try { return new URL(state.url).origin; } catch (_) { return "https://atlas.yashdynamo.fun"; } };
  const pathUrl = () => origin() + "/mcp/k/" + keyOrPlaceholder();
  const headerCfg = () => ({ url: state.url, headers: { Authorization: "Bearer " + keyOrPlaceholder() } });
  const bridgeArgs = () => ["-y", "mcp-remote", state.url, "--header", "Authorization: Bearer " + keyOrPlaceholder()];
  const snippets = () => ({
    path: pathUrl(),
    remoteJson: JSON.stringify({ mcpServers: { atlas: { type: "http", url: state.url, headers: { Authorization: "Bearer " + keyOrPlaceholder() } } } }, null, 2),
    remoteCli: "curl -si " + state.url + ' -H "Authorization: Bearer ' + keyOrPlaceholder() + '" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d \'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}\'',
    localJson: JSON.stringify({ mcpServers: { atlas: { command: "npx", args: bridgeArgs() } } }, null, 2),
    localCli: "npx -y mcp-remote " + state.url + ' --header "Authorization: Bearer ' + keyOrPlaceholder() + '"',
    localPath: "npx -y mcp-remote " + pathUrl(),
    exClaudeCode: "claude mcp add --transport http atlas " + state.url + ' --header "Authorization: Bearer ' + keyOrPlaceholder() + '"',
    exConnector: pathUrl(),
    exCodex: "codex mcp add atlas --url " + pathUrl(),
  });
  const b64 = (s) => btoa(unescape(encodeURIComponent(s)));
  const cursorLink = () => "cursor://anysphere.cursor-deeplink/mcp/install?name=atlas&config=" + encodeURIComponent(b64(JSON.stringify(headerCfg())));
  const vscodeLink = () => "vscode:mcp/install?" + encodeURIComponent(JSON.stringify({ name: "atlas", type: "http", url: state.url, headers: { Authorization: "Bearer " + keyOrPlaceholder() } }));

  function json(r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); }
  let infoP = null;
  function loadInfo() {
    if (!infoP) infoP = fetch("v1/mcp-info", { headers: { Accept: "application/json" } }).then(json).then((d) => {
      if (d && typeof d.mcp_url === "string") state.url = d.mcp_url;
      if (d && typeof d.demo_key === "string") { state.demo = d.demo_key; if (!state.priv) state.key = d.demo_key; }
      state.info = true; changed();
    }).catch(() => { changed(); });
    return infoP;
  }
  function changed() { render(); listeners.forEach((f) => f(state)); }

  // ---- modal ----------------------------------------------------------------------
  let dlg = null, ui = null, lastFocus = null;
  function card(id, title, hint, copyLabel) {
    const c = mk("div", "cm-card");
    const pre = mk("pre"); pre.id = "cm-" + id; pre.tabIndex = 0;
    const btn = mk("button", "copy", copyLabel || "Copy"); btn.type = "button"; btn.dataset.copy = "cm-" + id;
    btn.setAttribute("aria-label", "Copy " + title);
    c.append(mk("b", "", title), btn, mk("small", "", hint), pre);
    return c;
  }
  function build() {
    dlg = mk("dialog", "cm");
    dlg.setAttribute("aria-labelledby", "cm-title"); dlg.setAttribute("aria-describedby", "cm-desc");
    const h = mk("div", "cm-h"), hd = mk("div");
    const t = mk("h2", "", "Now wire it into your agent"); t.id = "cm-title";
    const p = mk("p", "", "Add Atlas as an MCP server over HTTP or stdio. Your agent then pulls only the skills, routes and entities each task needs."); p.id = "cm-desc";
    hd.append(t, p);
    const x = mk("button", "cm-x", "×"); x.type = "button"; x.setAttribute("aria-label", "Close");
    h.append(hd, x);
    const b = mk("div", "cm-b");
    const keybox = mk("div", "cm-key");
    const kc = mk("code"); kc.id = "cm-keyshow";
    const gk = mk("button", "btn", "Get a private key"); gk.type = "button";
    const kn = mk("p", "cm-note"); kn.setAttribute("role", "status"); kn.setAttribute("aria-live", "polite");
    keybox.append(kc, gk, kn);
    const one = mk("div", "cm-one");
    const cu = mk("button", "btn primary", "Cursor"); cu.type = "button";
    const vs = mk("button", "btn primary", "VS Code"); vs.type = "button";
    one.append(cu, vs);
    const fb = mk("p", "cm-fb"); fb.hidden = true; fb.setAttribute("role", "status");
    const fbt = mk("span"); const fbb = mk("button", "copy", "Copy config"); fbb.type = "button";
    fb.append(fbt, fbb);
    const sw = mk("div", "cm-sw"); sw.setAttribute("role", "tablist"); sw.setAttribute("aria-label", "Connection transport");
    const tr = mk("button", "", "Remote (HTTP)"), tl = mk("button", "", "Local (stdio)");
    [[tr, "remote"], [tl, "local"]].forEach(([el, m]) => { el.type = "button"; el.setAttribute("role", "tab"); el.dataset.mode = m; el.id = "cm-tab-" + m; el.setAttribute("aria-controls", "cm-panel"); });
    sw.append(tr, tl);
    const panel = mk("div", "cm-panel"); panel.id = "cm-panel"; panel.setAttribute("role", "tabpanel");
    const mean = mk("p", "cm-mean");
    const note = mk("p", "cm-warn");
    panel.append(mean, card("json", "Config file", "", "Copy"),
      card("cli", "Command line", "", "Copy"), card("path", "No header support? Use this URL", "", "Copy URL"), note);
    const edRow = mk("div", "cm-ed"); edRow.append(mk("span", "cm-sub", "Open in your editor"), one);
    const ex = mk("details", "cm-ex"); ex.append(mk("summary", "", "Examples"),
      card("ex-claude", "Claude Code", "Run in your terminal.", "Copy"),
      card("ex-connector", "Claude.ai, Claude Desktop, ChatGPT", "Custom connector or custom MCP server: paste this URL, authentication none.", "Copy URL"),
      card("ex-codex", "Codex", "Run in your terminal.", "Copy"));
    b.append(keybox, sw, panel, edRow, fb, ex);
    dlg.append(h, b);
    document.body.appendChild(dlg);
    ui = { kc, gk, kn, cu, vs, fb, fbt, fbb, t, p, mean, note, sw };
    sw.addEventListener("click", (e) => { const m = e.target.closest && e.target.closest("button[data-mode]"); if (m) { state.mode = m.dataset.mode; render(); } });
    sw.addEventListener("keydown", (e) => { if (e.key === "ArrowLeft" || e.key === "ArrowRight") { state.mode = state.mode === "remote" ? "local" : "remote"; render(); document.getElementById("cm-tab-" + state.mode).focus(); } });
    x.addEventListener("click", () => dlg.close());
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener("close", () => { if (lastFocus && lastFocus.focus) lastFocus.focus(); document.documentElement.style.overflow = ""; });
    gk.addEventListener("click", getPrivate);
    const link = (url, what) => () => {
      ui.fb.hidden = true;
      const cfg = JSON.stringify(headerCfg(), null, 2);
      ui.fbb.onclick = () => copyText(what === "Cursor" ? cfg : JSON.stringify({ name: "atlas", type: "http", url: state.url, headers: { Authorization: "Bearer " + keyOrPlaceholder() } }, null, 2), ui.fbb);
      let left = false;
      const onBlur = () => { left = true; };
      window.addEventListener("blur", onBlur, { once: true });
      document.addEventListener("visibilitychange", onBlur, { once: true });
      location.href = url();
      setTimeout(() => {
        window.removeEventListener("blur", onBlur);
        if (!left) { ui.fbt.textContent = "Nothing opened? " + what + " may not be installed, or your browser blocked the link. Copy the server config instead."; ui.fb.hidden = false; }
      }, 1600);
    };
    ui.cu.addEventListener("click", link(cursorLink, "Cursor"));
    ui.vs.addEventListener("click", link(vscodeLink, "VS Code"));
  }
  function render() {
    if (!ui) return;
    const s = snippets();
    const set = (id, v) => { document.getElementById("cm-" + id).textContent = v; };
    const hint = (id, t) => { document.getElementById("cm-" + id).parentNode.querySelector("small").textContent = t; };
    const remote = state.mode === "remote";
    ["remote", "local"].forEach((m) => { const t = document.getElementById("cm-tab-" + m); t.setAttribute("aria-selected", String(m === state.mode)); t.tabIndex = m === state.mode ? 0 : -1; });
    document.getElementById("cm-panel").setAttribute("aria-labelledby", "cm-tab-" + state.mode);
    ui.mean.textContent = remote
      ? "Your client connects straight to the hosted Atlas URL over streamable HTTP and sends the key in an Authorization header. Nothing to install."
      : "Your client launches a command and talks to it over stdio. For hosted Atlas that command is the standard mcp-remote bridge (needs Node.js), which forwards to the hosted URL. A self-hosted Atlas can instead run python -m atlas.mcp.";
    set("json", remote ? s.remoteJson : s.localJson);
    set("cli", remote ? s.remoteCli : s.localCli);
    set("path", remote ? s.path : s.localPath);
    hint("json", remote ? "Add to your client's MCP config. Some clients name the transport field differently or omit it." : "Add to your client's MCP config. The client starts this command itself.");
    document.getElementById("cm-cli").parentNode.querySelector("b").textContent = remote ? "Check from a terminal" : "Run it by hand";
    hint("cli", remote ? "Sends an MCP initialize request; a JSON result means the URL and key work." : "The same command your client runs.");
    hint("path", remote ? "The key is in the path, so no header is needed. Same key, same map." : "The same bridge, with the key in the path instead of a header.");
    ui.note.textContent = "Treat any URL that contains your key like a password. Atlas serves streamable HTTP only; SSE is not offered.";
    set("ex-claude", s.exClaudeCode); set("ex-connector", s.exConnector); set("ex-codex", s.exCodex);
    ui.kc.textContent = state.key ? state.key : "(key unavailable: replace <your-key>)";
    ui.gk.textContent = state.priv ? "New private key" : "Get a private key";
    if (!ui.noteLocked) ui.kn.textContent = state.priv ? "Shown once; your map is private to this key." : state.demo ? "Public read-only demo key: plans and recalls on the shared map. Get a private key to let your agent write." : "";
  }
  function getPrivate() {
    ui.gk.disabled = true; ui.noteLocked = true; ui.kn.textContent = "Creating your key...";
    fetch("v1/keys", { method: "POST" }).then(json).then((d) => {
      if (!d || typeof d.api_key !== "string") throw new Error("bad");
      state.key = d.api_key; state.priv = true;
      if (typeof d.mcp_url === "string") state.url = d.mcp_url;
      ui.noteLocked = false; changed();
    }).catch((e) => {
      ui.kn.textContent = String(e && e.message) === "429" ? "Key limit reached for today. Use the demo key, or try again later." : "Could not create a key right now. Try again in a moment.";
    }).then(() => { ui.gk.disabled = false; });
  }
  function open(opts) {
    opts = opts || {};
    if (!dlg) build();
    lastFocus = document.activeElement;
    ui.t.textContent = opts.after ? "Now wire it into your agent" : "Connect your agent";
    ui.fb.hidden = true;
    render();
    if (!dlg.open) { dlg.showModal(); document.documentElement.style.overflow = "hidden"; }
    loadInfo();
  }
  document.addEventListener("click", (e) => {
    const t = e.target.closest && e.target.closest("[data-connect]");
    if (t) { e.preventDefault(); open({}); }
  });
  loadInfo();
  window.AtlasConnect = { open: (o) => open(o), state, onChange: (f) => listeners.push(f), loadInfo };
})();
