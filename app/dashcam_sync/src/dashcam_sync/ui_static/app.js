"use strict";
// All URLs are relative: Home Assistant serves this page under an ingress prefix.
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const main = $("#main");
const api = async (path, opts = {}) => {
  const res = await fetch("api/v1/" + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
};
// HTML-escape, as a plain replace chain: the form static analysers (CodeQL) recognise as escaping.
const esc = (s) => String(s ?? "")
  .replace(/&/g, "&amp;")
  .replace(/</g, "&lt;")
  .replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;")
  .replace(/'/g, "&#39;");
const NUM = [new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }), new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 })];
const bytes = (n) => {
  if (n == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return NUM[i ? 1 : 0].format(n) + "\u00a0" + u[i];
};
const DATE = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const when = (iso) => (iso ? DATE.format(new Date(iso)) : "never");
const eta = (s) => (s == null ? "–" : s < 90 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);
const ago = (iso) => {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : when(iso);
};

// ------------------------------------------------------------------ Home Assistant theme

const THEME = {
  "--primary-color": "--accent", "--text-primary-color": "--on-accent",
  "--primary-background-color": "--bg", "--card-background-color": "--card", "--ha-card-background": "--card",
  "--primary-text-color": "--fg", "--secondary-text-color": "--fg2", "--divider-color": "--line",
  "--error-color": "--bad", "--success-color": "--ok", "--warning-color": "--warn",
  "--ha-card-border-radius": "--radius", "--ha-card-box-shadow": "--shadow",
};
function inheritTheme() {
  const root = document.documentElement;
  if (window.parent === window) return;
  root.classList.add("embedded");
  let parentRoot;
  try { parentRoot = window.parent.document.documentElement; } catch (e) { return; } // not same origin
  const read = () => {
    const cs = window.parent.getComputedStyle(parentRoot);
    let themed = false;
    for (const [theirs, ours] of Object.entries(THEME)) {
      const v = cs.getPropertyValue(theirs).trim();
      if (v) { root.style.setProperty(ours, v); themed = true; }
    }
    const font = cs.getPropertyValue("--ha-font-family-body").trim() || cs.getPropertyValue("--paper-font-body1_-_font-family").trim();
    if (font) root.style.setProperty("--font", font);
    root.classList.toggle("themed", themed);
    const bg = getComputedStyle(root).getPropertyValue("--bg").trim();
    const m = bg.match(/\d+(\.\d+)?/g);
    if (m && m.length >= 3) root.style.colorScheme = (0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2]) < 128 ? "dark" : "light";
    else if (/^#[0-9a-f]{6}$/i.test(bg)) {
      const n = parseInt(bg.slice(1), 16);
      root.style.colorScheme = (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) < 128 ? "dark" : "light";
    }
  };
  read();
  new MutationObserver(read).observe(parentRoot, { attributes: true, attributeFilter: ["style", "class"] });
  window.parent.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => setTimeout(read, 50));
}

// ------------------------------------------------------------------ building blocks

let toastTimer = null;
function toast(text, kind = "") {
  const t = $("#toast");
  t.textContent = text; t.className = kind; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, kind === "bad" ? 6000 : 3000);
}

/** A dialog: `sheet` makes it full screen on phones. Returns {dlg, body, close}. */
function openDialog(title, { sheet = false, small = false, id = "" } = {}) {
  const dlg = document.createElement("dialog");
  dlg.className = [sheet ? "sheet" : "", small ? "small" : ""].join(" ").trim();
  if (id) dlg.id = id;
  dlg.innerHTML = `<div class="dlg-head"><h2>${esc(title)}</h2><button class="icon dlg-close" aria-label="Close">✕</button></div><div class="dlg-body"></div>`;
  document.body.append(dlg);
  const close = () => { dlg.close(); };
  dlg.addEventListener("close", () => { dlg.dispatchEvent(new Event("closed")); dlg.remove(); });
  $(".dlg-close", dlg).onclick = close;
  dlg.showModal();
  return { dlg, body: $(".dlg-body", dlg), close, setTitle: (t) => { $(".dlg-head h2", dlg).textContent = t; } };
}

function confirmDialog(title, text, okLabel = "OK", danger = false) {
  return new Promise((resolve) => {
    const d = openDialog(title, { small: true, id: "confirm" });
    let answer = false;
    d.body.innerHTML = `<div>${text}</div><div class="actions end"><button class="text" id="c-no">Cancel</button>
      <button class="${danger ? "primary danger-fill" : "primary"}" id="c-yes">${esc(okLabel)}</button></div>`;
    if (danger) $("#c-yes", d.dlg).style.background = $("#c-yes", d.dlg).style.borderColor = "var(--bad)";
    $("#c-no", d.dlg).onclick = d.close;
    $("#c-yes", d.dlg).onclick = () => { answer = true; d.close(); };
    d.dlg.addEventListener("closed", () => resolve(answer));
  });
}

/** A ⋮ menu: items are [label, handler, {danger, hidden, divider}] */
function menuButton(items, label = "More actions") {
  const wrap = document.createElement("span");
  wrap.className = "menu-wrap";
  wrap.innerHTML = `<button class="icon menu-btn" aria-label="${esc(label)}" aria-haspopup="menu" aria-expanded="false">⋮</button><div class="menu" role="menu" hidden></div>`;
  const btn = $(".menu-btn", wrap), menu = $(".menu", wrap);
  for (const [text, handler, opts = {}] of items) {
    if (opts.hidden) continue;
    if (opts.divider) menu.append(document.createElement("hr"));
    const b = document.createElement("button");
    b.setAttribute("role", "menuitem"); b.textContent = text;
    if (opts.danger) b.className = "danger";
    if (opts.act) b.dataset.act = opts.act;
    b.onclick = (e) => { e.stopPropagation(); hide(); handler(); };
    menu.append(b);
  }
  const hide = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
  btn.onclick = (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    $$(".menu").forEach((m) => { m.hidden = true; });
    menu.hidden = !open; btn.setAttribute("aria-expanded", String(open));
    if (open) menu.querySelector("button")?.focus();
  };
  wrap.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { hide(); btn.focus(); return; }
    if (menu.hidden || !["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const items = $$("button", menu), i = items.indexOf(document.activeElement);
    const next = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: items.length - 1 }[e.key];
    items[(next + items.length) % items.length].focus();
  });
  return wrap;
}
document.addEventListener("click", () => $$(".menu").forEach((m) => { m.hidden = true; }));

const sig = (pct) => {
  const n = pct == null ? 0 : pct >= 75 ? 4 : pct >= 50 ? 3 : pct >= 25 ? 2 : 1;
  return `<span class="sig" title="${pct == null ? "" : pct + "%"}" aria-label="signal ${pct ?? "unknown"}%">${[1, 2, 3, 4].map((i) => `<i class="${i <= n ? "on" : ""}"></i>`).join("")}</span>`;
};

const ISSUES = {
  no_wifi_adapter: "No Wi-Fi adapter was found. Home Assistant OS needs a supported Wi-Fi adapter to join a dashcam hotspot.",
  camera_invalid: "A camera entry is invalid and was disabled.",
  subnet_overlap: "The dashcam's address block overlaps something on your home network. The camera is paused.",
  default_route_changed: "The default route moved to the dashcam network. The Wi-Fi connection must be removed.",
  camera_playback_mode: "A camera may be stuck in playback mode. Use Restore recording.",
  unsupported_camera: "No driver recognises this camera. Capture a device profile and open an issue.",
  listing_keeps_failing: "The camera answers but listing its files keeps failing.",
  network_target_offline: "The disk or share has been offline for over an hour.",
  storage_low: "A storage target is below its minimum free space.",
};
const STATUS_TEXT = { idle: "idle", detecting: "detecting", preparing: "preparing", listing: "listing files", downloading: "downloading", restoring: "restoring recording", error: "error" };

// ------------------------------------------------------------------ pairing

// The app and the integration are released together. Joining camera Wi-Fi, entities and
// clips in Media all need the integration connected, and on the same release.
const pairingProblem = (p) => {
  if (!p) return "";  // an app too old to tell: nothing to say
  const setup = `<a href="/config/integrations/integration/dashcam_sync" target="_top">Settings › Devices &amp; services</a>`;
  if (!p.connected) return `<b>The Dashcam Sync integration is not connected.</b> Home Assistant cannot join camera Wi-Fi, and has no Dashcam Sync entities or clips in Media, until it is. Add it under ${setup} (a discovered Dashcam Sync is usually waiting there), or install it from HACS first.`;
  if (!p.matches) return `<b>The app and the integration are different releases.</b> App ${esc(p.app_release)}, integration ${p.version ? esc(p.version) : "older than 2026.11.0b2"}. Update the integration in HACS and the app under <a href="/config/apps" target="_top">Settings › Apps</a> to the same release, then restart Home Assistant.`;
  return "";
};
async function checkPairing() {
  const el = $("#pairing");
  const info = await api("info").catch(() => null);
  // Just after the app starts, the integration takes a moment to reconnect.
  const problem = info && info.uptime > 60 ? pairingProblem(info.integration) : "";
  el.hidden = !problem;
  el.innerHTML = problem ? `<div class="card notice bad">${problem}</div>` : "";
}

// ------------------------------------------------------------------ routing

let view = "overview", timer = null, unsaved = false, lastHash = location.hash;
window.addEventListener("beforeunload", (e) => { if (unsaved) e.preventDefault(); });
const route = () => {
  const [path, query] = location.hash.slice(1).split("?");
  const [name, sub] = path.split("/");
  return { name: name || "overview", sub: sub ? decodeURIComponent(sub) : "", query: new URLSearchParams(query || "") };
};
async function render() {
  if (unsaved && location.hash !== lastHash && !confirm("Leave without saving your changes?")) {
    history.replaceState(null, "", lastHash); // stay on the page with the unsaved changes
    return;
  }
  unsaved = false; lastHash = location.hash;
  clearInterval(timer); clearTimeout(timer);
  main.oninput = main.onchange = null; // page-specific listeners never outlive their page
  const r = route();
  view = r.name;
  $$("nav a").forEach((a) => {
    const on = a.getAttribute("href") === "#" + view;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  try {
    if (view === "cameras" && r.sub) await cameraDetail(r.sub);
    else await ({ overview, cameras, clips, storage, settings, logs }[view] || overview)();
  } catch (e) {
    main.innerHTML = `<div class="card notice bad">${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", render);

// ------------------------------------------------------------------ overview

async function overview() {
  const draw = async () => {
    const s = await api("status");
    const cams = Object.values(s.cameras);
    main.innerHTML = `
      ${s.issues.map((i) => `<div class="card notice ${i.key === "subnet_overlap" ? "bad" : ""}"><b>${esc(i.camera || "")}</b> ${esc(ISSUES[i.key] || i.key)}
        ${i.placeholders.conflicts?.length ? `<div class="muted">In conflict: ${esc(i.placeholders.conflicts.join(", "))}</div>` : ""}</div>`).join("")}
      <h2>Cameras</h2>
      ${cams.length ? "" : `<div class="card empty">No camera yet. <a href="#cameras">Add one</a>.</div>`}
      <div class="grid">${cams.map((c) => `
        <a class="card cam-card" href="#cameras/${encodeURIComponent(c.id)}" style="color:inherit;text-decoration:none;display:block">
          <div class="head"><span class="name">${esc(c.name)}</span>${statusBadge(c)}</div>
          <div class="sub">${esc(STATUS_TEXT[c.sync_status] || c.sync_status)}${c.driver ? " · " + esc(c.driver) : ""} · last sync ${ago(c.last_sync)}</div>
          ${progress(c)}
          ${c.blocked_reasons.length ? `<div class="sub"><span class="badge bad">paused: ${esc(c.blocked_reasons.join(", "))}</span></div>` : ""}
          ${c.restore_pending ? `<div class="sub"><span class="badge warn">restore pending</span></div>` : ""}
        </a>`).join("")}</div>
      <h2>Storage</h2>
      <div class="grid">${Object.values(s.storage.targets).map(targetCard).join("")}</div>`;
  };
  await draw();
  timer = setInterval(() => draw().catch(() => {}), 3000);
}

function targetCard(t) {
  const used = t.total_bytes ? 100 - (100 * t.free_bytes) / t.total_bytes : 0;
  return `<div class="card"><div class="row"><span class="title">${t.name === "local" ? "This Home Assistant" : t.name === "network" ? "Disk or share" : esc(t.name)}</span>
      <span class="badge ${t.online ? "ok" : "bad"}">${t.online ? "online" : esc(t.reason || "offline")}</span></div>
    <div class="muted small">${esc(t.root)}</div>
    <div class="bar" title="${used.toFixed(0)}% used"><div style="width:${used}%"></div></div>
    <div>Clips ${bytes(t.clip_bytes)} · free ${bytes(t.free_bytes)} of ${bytes(t.total_bytes)}</div></div>`;
}

function statusBadge(s, enabled = true) {
  if (!enabled) return `<span class="badge warn">paused</span>`;
  if (s?.connected) return `<span class="badge ok">connected</span>`;
  return `<span class="badge">not in range</span>`;
}
function progress(s) {
  if (!s || !s.clips_remaining || s.sync_status !== "downloading") return "";
  return `<div class="progress"><div class="bar"><div style="width:0%"></div></div>
    <div class="small muted">${[
      `${s.clips_remaining} ${s.clips_remaining === 1 ? "clip" : "clips"} left`,
      s.bytes_remaining > 0 ? bytes(s.bytes_remaining) : null,
      s.speed_bps > 0 ? `${bytes(s.speed_bps)}/s` : null,
      s.eta_seconds > 0 ? `about ${eta(s.eta_seconds)}` : null,
    ].filter(Boolean).join(" · ")}</div></div>`;
}

// ------------------------------------------------------------------ cameras

const DRIVERS = { auto: "Detect automatically", novatek: "Novatek", viidure_app: "Viidure app (/app)", http_index: "Plain HTTP file index" };
const CAMERA_IPS = { "192.168.1.254": "Novatek (most common)", "192.168.169.1": "Viidure app cameras", "192.168.0.1": "Viidure / SGK (GK720X)" };
const SCAN_REASONS = {
  integration_not_connected: "Scanning goes through Home Assistant, and the Dashcam Sync integration is not connected. Type the camera's SSID.",
  no_answer: "Home Assistant did not answer the scan. Type the camera's SSID.",
};
const JOIN_REASONS = {
  integration_not_connected: "The Dashcam Sync integration is not connected to this app, so Home Assistant cannot be asked to join.",
  no_answer: "Home Assistant did not answer the join request. If the integration was just updated, restart Home Assistant.",
  wrong_password: "Home Assistant sees the camera's Wi-Fi but could not connect to it. The Wi-Fi password is most likely wrong.",
  not_in_range: "Home Assistant cannot see the camera's Wi-Fi. Is the camera on, and the car within range? Check the SSID too.",
  not_connected: "Home Assistant could not connect to the camera's Wi-Fi.",
  device_in_block: "A device on your network uses an address in the camera's reserved block. Run Network check on the camera for details.",
  address_in_use: "Home Assistant's address on the camera network is already used on your network. Run Network check for details.",
  camera_collides: "Something on your network has the camera's address. See Overlapping addresses in the documentation.",
};
const joinReason = (r) => {
  if (!r) return "";
  const key = r.split(":")[0];
  if (key === "no_adapter") return `Home Assistant has no Wi-Fi interface named ${esc(r.split(":")[1] || "")}.`;
  if (key === "app_unavailable") return "Home Assistant could not read the camera's Wi-Fi settings from the app.";
  return esc(JOIN_REASONS[key] || r);
};
const ADDRESSING = {
  auto: "Automatic (recommended)",
  block: "Small block of 4 addresses",
  point_to_point: "Point-to-point (2 addresses)",
};
const OVERLAP = {
  no_overlap: ["ok", "No overlap with your home network."],
  overlap_block_free: ["ok", "Overlaps your home network, but the reserved addresses are free."],
  device_in_block: ["bad", "A device on your network uses an address in the reserved block."],
  address_in_use: ["bad", "Home Assistant's address on the camera network is in use on your network."],
  camera_collides: ["bad", "Something on your network has the camera's address."],
};
const connLine = (o) => (o.connection === "hotspot"
  ? `Camera hotspot · ${esc(o.ssid || "?")} on ${esc(o.interface || "?")}`
  : `Local network · ${esc(o.host || "auto-detect")}`);

/** Actions shared by the camera list and the detail view. */
function cameraMenu(o, after) {
  return menuButton([
    ["Edit", () => wizard(o), { act: "edit" }],
    ["Network check", () => networkCheck(o.id), { act: "overlap", hidden: o.connection !== "hotspot" }],
    ["Restore recording", async () => { await act(() => api(`cameras/${o.id}/restore`, { method: "POST" }), "Restore requested"); }, { act: "restore" }],
    [o.enabled ? "Pause" : "Resume", async () => { await act(() => api(`cameras/${o.id}`, { method: "PATCH", body: { enabled: !o.enabled } }), o.enabled ? "Paused" : "Resumed"); after(); }, { act: "toggle" }],
    ["Capture device profile", () => captureProfile(o), { act: "profile" }],
    ["Remove", async () => {
      if (await confirmDialog(`Remove ${o.name || o.id}?`, "The camera is removed from Dashcam Sync. Clips already downloaded are kept.", "Remove", true)) {
        await act(() => api(`cameras/${o.id}`, { method: "DELETE" }), "Camera removed");
        location.hash = "#cameras"; after();
      }
    }, { act: "delete", danger: true, divider: true }],
  ]);
}
async function act(fn, okText) {
  try { await fn(); toast(okText); } catch (e) { toast(e.message, "bad"); }
}
async function syncNow(id) { await act(() => api(`cameras/${id}/sync`, { method: "POST" }), "Sync requested"); }

async function networkCheck(id) {
  const d = openDialog("Network check", { small: true, id: "overlap-dlg" });
  d.body.innerHTML = `<div class="muted"><span class="spinner"></span> Checking the camera's address block…</div>`;
  try {
    const r = await api(`cameras/${id}/overlap`, { method: "POST", body: {} });
    const [cls, text] = OVERLAP[r.result] || ["warn", r.result];
    d.body.innerHTML = `<p><span class="badge ${cls}">${cls === "ok" ? "OK" : "Problem"}</span> ${esc(text)}</p>
      <dl class="kv">${r.mode === "point_to_point"
        ? `<dt>Shape</dt><dd>Point-to-point</dd><dt>Claimed</dt><dd><code>${esc((r.claimed || []).join(", "))}</code></dd>`
        : `<dt>Reserved block</dt><dd><code>${esc(r.block || "?")}</code></dd>`}
      <dt>Home Assistant</dt><dd><code>${esc(r.static_address || "?")}</code></dd>
      ${r.conflicts?.length ? `<dt>In use</dt><dd>${r.conflicts.map((c) => `<code>${esc(c)}</code>${r.evidence?.[c] ? ` · ${esc(r.evidence[c])}` : ""}`).join("<br>")}</dd>` : ""}</dl>
      <div class="actions end"><button class="primary" id="o-ok">OK</button></div>`;
    $("#o-ok", d.dlg).onclick = d.close;
  } catch (e) { d.body.innerHTML = `<div class="notice card bad">${esc(e.message)}</div>`; }
}

function captureProfile(o) {
  const d = openDialog("Capture device profile", { small: true, id: "profile-dlg" });
  d.body.innerHTML = `<p class="muted">Records how the camera answers, so a driver can be written or fixed. Wi-Fi passwords are not included.</p>
    <label for="p-host">Camera address</label><input id="p-host" name="host" autocomplete="off" spellcheck="false" inputmode="url" value="${esc(o.host || "")}" placeholder="192.168.1.254">
    <div id="p-out"></div><div class="actions end"><button class="text" id="p-cancel">Cancel</button><button class="primary" id="p-go">Capture</button></div>`;
  $("#p-cancel", d.dlg).onclick = d.close;
  $("#p-go", d.dlg).onclick = async () => {
    const host = $("#p-host", d.dlg).value.trim();
    if (!host) return;
    $("#p-out", d.dlg).innerHTML = `<p class="muted"><span class="spinner"></span> Capturing…</p>`;
    try {
      const bundle = await api("wizard/capture-profile", { method: "POST", body: { host } });
      const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }));
      $("#p-out", d.dlg).innerHTML = `<p><a href="${url}" download="device-profile-${esc(o.id)}.json">Download the profile</a> and attach it to a GitHub issue.</p>`;
    } catch (e) { $("#p-out", d.dlg).innerHTML = `<div class="card notice bad">${esc(e.message)}</div>`; }
  };
}

async function cameras() {
  const draw = async () => {
    const list = await api("cameras");
    main.innerHTML = `<div class="toolbar"><h2>Cameras</h2><button class="primary" id="add">Add camera</button></div>
      ${list.length ? "" : `<div class="card empty">No camera yet. Add one to start syncing.</div>`}
      <div id="cam-list"></div>`;
    $("#add").onclick = () => wizard();
    for (const { options: o, status: s } of list) {
      const card = document.createElement("div");
      card.className = "card cam-card"; card.dataset.id = o.id;
      card.innerHTML = `<div class="head"><a class="name" href="#cameras/${encodeURIComponent(o.id)}">${esc(o.name || o.id)}</a></div>
        <div class="sub">${statusBadge(s, o.enabled)}${s && !["idle", "error"].includes(s.sync_status) ? ` <span class="badge info">${esc(STATUS_TEXT[s.sync_status] || s.sync_status)}</span>` : ""}</div>
        <div class="sub">${connLine(o)} · last sync ${ago(s?.last_sync)}</div>
        ${s?.error ? `<div class="sub bad-text">${esc(s.error)}</div>` : ""}
        ${progress(s)}
        <div class="actions" style="margin-top:12px"><button class="sync-now" data-act="sync" ${s?.connected && o.enabled ? "" : "disabled"}>Sync now</button></div>`;
      $(".head", card).append(cameraMenu(o, draw));
      $(".sync-now", card).onclick = (e) => { e.stopPropagation(); syncNow(o.id); };
      // The name is the keyboard and middle-click link; the rest of the card is a larger tap target.
      card.onclick = (e) => { if (!e.target.closest("button, a, .menu-wrap")) location.hash = "#cameras/" + encodeURIComponent(o.id); };
      $("#cam-list").append(card);
    }
  };
  await draw();
  timer = setInterval(() => { if (!document.querySelector("dialog[open]") && !$$(".menu").some((m) => !m.hidden)) draw().catch(() => {}); }, 5000);
}

async function cameraDetail(id) {
  const draw = async () => {
    const r = await api(`cameras/${encodeURIComponent(id)}`);
    const o = r.options, s = r.status || {};
    main.innerHTML = `<p><a href="#cameras">‹ Cameras</a></p>
      <div class="card cam-card" style="cursor:default"><div class="head"><span class="name">${esc(o.name || o.id)}</span></div>
        <div class="sub">${statusBadge(s, o.enabled)}</div>
        <div class="sub">${connLine(o)}</div>
        <div class="actions" style="margin-top:12px"><button class="sync-now primary" ${s.connected && o.enabled ? "" : "disabled"}>Sync now</button></div>
        ${progress(s)}
        ${s.error ? `<div class="card notice bad" style="margin:12px 0 0">${esc(s.error)}</div>` : ""}
      </div>
      <div class="card"><dl class="kv">
        <dt>Status</dt><dd>${esc(STATUS_TEXT[s.sync_status] || s.sync_status || "–")}</dd>
        <dt>Last sync</dt><dd>${when(s.last_sync)}</dd>
        <dt>Last event clip</dt><dd>${when(s.last_event_clip)}</dd>
        <dt>Camera</dt><dd>${esc([s.driver, s.model, s.firmware ? "fw " + s.firmware : ""].filter(Boolean).join(" · ") || "not detected yet")}</dd>
        <dt>SD card free</dt><dd>${bytes(s.sd_free_bytes)}</dd>
        ${o.connection === "hotspot" ? `<dt>Home Assistant address</dt><dd><code>${esc(o.static_address || "–")}</code></dd>` : ""}
        ${s.blocked_reasons?.length ? `<dt>Paused because</dt><dd>${esc(s.blocked_reasons.join(", "))}</dd>` : ""}
      </dl></div>
      <h2>Recent syncs</h2>
      ${r.sessions.length ? `<div class="card" style="padding:0 8px;overflow-x:auto"><table><tr><th>Started</th><th>Result</th><th>Clips</th><th>Data</th></tr>
        ${r.sessions.map((x) => `<tr><td>${when(x.started_ts ? new Date(x.started_ts * 1000).toISOString() : null)}</td><td>${x.result === "ok" ? '<span class="badge ok">ok</span>' : `<span class="badge ${x.result ? "bad" : ""}">${esc(x.result || "running")}</span>${x.error ? `<div class="small muted">${esc(x.error)}</div>` : ""}`}</td><td>${x.clips}</td><td>${bytes(x.bytes)}</td></tr>`).join("")}</table></div>`
        : `<div class="card empty">No sync yet.</div>`}`;
    $(".head", main).append(cameraMenu(o, draw));
    $(".sync-now", main).onclick = () => syncNow(o.id);
  };
  await draw();
  timer = setInterval(() => { if (!document.querySelector("dialog[open]") && !$$(".menu").some((m) => !m.hidden)) draw().catch(() => {}); }, 4000);
}

// ------------------------------------------------------------------ setup wizard

const LIKELY_CAMERA = /cardv|dash|dvr|cam|viofo|70mai|vantrue|blackvue|nextbase|thinkware|garmin|redtiger|azdome|rexing|wolfbox|ddpai|(?:dc[-_ ]?\w*$)/i;

async function wizard(existing) {
  const edit = !!existing;
  const d = openDialog(edit ? `Edit ${existing.name || existing.id}` : "Add camera", { sheet: true, id: "wizard" });
  const box = d.body;
  const st = {
    id: existing?.id || "", name: existing?.name || "", connection: existing?.connection || "hotspot",
    interface: existing?.interface || "", ssid: existing?.ssid || "", password: "",
    host: existing?.host || "", driver: existing?.driver || "auto", ipv4: existing?.ipv4 || "static",
    static_address: existing?.static_address || "", addressing: existing?.addressing || "auto", step: edit ? 2 : 1, detection: null, aps: null,
  };
  let poller = null;
  d.dlg.addEventListener("closed", () => { clearInterval(poller); render(); });
  let ifaces = { known: false, wireless: [] };
  try { ifaces = await api("wizard/interfaces"); } catch (e) { /* listed as unavailable below */ }
  if (!st.interface) {
    const free = ifaces.wireless.filter((i) => !i.in_use_by || i.in_use_by === st.id);
    if (free.length) st.interface = free[0].name;
  }
  const STEPS = ["Connection", "Details", "Name", "Connect"];
  const camIp = () => (!st.host ? "192.168.1.254" : CAMERA_IPS[st.host.split(":")[0]] ? st.host.split(":")[0] : "other");
  const slug = (n) => (n || "camera").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 32) || "camera";
  const ifaceNote = (i) => (i.in_use_by && i.in_use_by !== st.id ? `used by ${i.in_use_by}` : i.connected ? `connected${i.ssid ? " to " + i.ssid : ""}` : i.enabled ? "not connected" : "not configured · free");
  const read = () => {
    const v = (id) => { const el = $("#" + id, box); return el ? el.value.trim() : undefined; };
    // One assignment per field (no st[k] = …), so it is plain which state holds typed text.
    const iface = v("w-if"); if (iface !== undefined) st.interface = iface;
    const ssid = v("w-ssid"); if (ssid !== undefined) st.ssid = ssid;
    const host = v("w-host"); if (host !== undefined) st.host = host;
    const name = v("w-name"); if (name !== undefined) st.name = name;
    const driver = v("w-driver"); if (driver !== undefined) st.driver = driver;
    const addressing = v("w-addressing"); if (addressing !== undefined) st.addressing = addressing;
    const staticAddress = v("w-static"); if (staticAddress !== undefined) st.static_address = staticAddress;
    const pw = $("#w-pass", box); if (pw) st.password = pw.value;
    const ip = $("#w-camip", box);
    if (ip) st.host = ip.value === "other" ? $("#w-camip-other", box).value.trim() : ip.value;
    const conn = box.querySelector("input[name=w-conn]:checked"); if (conn) st.connection = conn.value;
  };
  const err = (msg) => { $("#w-err", box).innerHTML = msg ? `<div class="card notice bad" style="margin:16px 0 0">${msg}</div>` : ""; };
  const note = (msg) => { $("#w-err", box).innerHTML = msg ? `<div class="card notice info" style="margin:16px 0 0">${msg}</div>` : ""; };
  const summary = (r, b) => {
    const caps = Object.entries(r.capabilities || {}).filter(([, on]) => on).map(([k]) => `<span class="badge ok">${esc(k.replace(/_/g, " "))}</span>`).join(" ");
    return `<div class="card" style="margin:12px 0 0"><b>Detected a ${esc(r.driver)} camera</b> at ${esc(r.host)}${r.firmware ? ` · firmware ${esc(r.firmware)}` : ""}${r.model ? ` · ${esc(r.model)}` : ""}
      <div style="margin-top:6px;display:flex;gap:4px;flex-wrap:wrap">${caps}</div>
      ${b ? `<div class="muted small" style="margin-top:8px">${b.mode === "point_to_point"
        ? `Point-to-point · Home Assistant uses ${esc(b.static_address)}, only ${esc((b.claimed || []).join(" and "))} are routed to the car`
        : `Reserved address block ${esc(b.block)} · Home Assistant uses ${esc(b.static_address)}`}
        ${b.blocked ? '<span class="badge bad">conflict: run Network check</span>'
          : b.overlaps_home_network || b.result === "overlap_block_free" ? `<span class="badge info">inside your home network's range; the claimed addresses are free</span>`
          : '<span class="badge ok">no overlap with your home network</span>'}</div>` : ""}</div>`;
  };
  const apList = () => {
    if (!st.aps) return "";
    if (!st.aps.length) return `<div class="hint">No networks found. Is the camera on and in range?</div>`;
    return `<div class="aps" role="listbox" aria-label="Networks">${st.aps.map((a) => {
      const likely = LIKELY_CAMERA.test(a.ssid);
      return `<button class="ap ${likely ? "likely" : ""} ${a.ssid === st.ssid ? "chosen" : ""}" role="option" aria-selected="${a.ssid === st.ssid}" data-ssid="${esc(a.ssid)}">
        <span class="ssid">${esc(a.ssid)}</span>${likely ? '<span class="badge info">dashcam?</span>' : ""}${sig(a.signal)}</button>`;
    }).join("")}</div>`;
  };

  const draw = () => {
    clearInterval(poller);
    const step = st.step;
    let body = "";
    if (step === 1) {
      body = `<div class="choices">
        <label class="choice"><input type="radio" name="w-conn" value="hotspot" ${st.connection === "hotspot" ? "checked" : ""}><span><b>Camera hotspot</b>
          <span class="muted">The camera runs its own Wi-Fi; Home Assistant joins it with a Wi-Fi interface.</span></span></label>
        <label class="choice"><input type="radio" name="w-conn" value="lan" ${st.connection === "lan" ? "checked" : ""}><span><b>Local network</b>
          <span class="muted">The camera is a client on your LAN.</span></span></label></div>`;
    } else if (step === 2 && st.connection === "hotspot") {
      const one = ifaces.wireless.length === 1 ? ifaces.wireless[0] : null;
      body = `<label for="w-if">Wi-Fi interface</label>
        ${one ? `<input type="hidden" id="w-if" value="${esc(one.name)}"><div><b>${esc(one.name)}</b> <span class="muted">· ${esc(ifaceNote(one))}</span></div>`
          : ifaces.wireless.length ? `<select id="w-if">${ifaces.wireless.map((i) => `<option value="${esc(i.name)}" ${i.name === st.interface ? "selected" : ""}>${esc(i.name)} · ${esc(ifaceNote(i))}</option>`).join("")}</select>`
          : `<input id="w-if" value="${esc(st.interface)}" placeholder="wlan0"><div class="hint">${ifaces.known ? "Home Assistant reports no Wi-Fi interface. A supported USB Wi-Fi adapter is the usual fix." : "Interfaces are not available here; type the name shown under Settings › System › Network."}</div>`}
        <label for="w-ssid">Camera SSID</label>
        <div class="field-row"><input id="w-ssid" name="ssid" value="${esc(st.ssid)}" autocomplete="off" autocapitalize="off" spellcheck="false"><button id="w-scan">Scan</button></div>
        <div id="w-aps">${apList()}</div>
        <label for="w-pass">Camera Wi-Fi password</label>
        <input id="w-pass" name="wifi-password" type="password" autocomplete="new-password" placeholder="${edit ? "leave empty to keep the current one" : ""}">
        <label for="w-camip">Camera IP on its hotspot</label>
        <select id="w-camip">${Object.entries(CAMERA_IPS).map(([ip, t]) => `<option value="${ip}" ${camIp() === ip ? "selected" : ""}>${ip} · ${t}</option>`).join("")}
          <option value="other" ${camIp() === "other" ? "selected" : ""}>Other…</option></select>
        <input id="w-camip-other" name="camera-ip" autocomplete="off" spellcheck="false" inputmode="url" value="${camIp() === "other" ? esc(st.host) : ""}" placeholder="e.g. 192.168.0.1" style="margin-top:8px" ${camIp() === "other" ? "" : "hidden"}>
        <div class="hint">Shown in the camera's phone app or manual. Home Assistant's address on the camera network is picked next to it.</div>`;
    } else if (step === 2) {
      body = `<label for="w-host">Camera IP or host</label>
        <div class="field-row"><input id="w-host" name="host" value="${esc(st.host)}" placeholder="192.168.2.50" autocomplete="off" autocapitalize="off" spellcheck="false" inputmode="url"><button id="w-probe">Test</button></div>
        <div class="hint">Leave empty to look for the camera automatically. Give it a DHCP reservation in your router so the address does not change.</div>
        <div id="w-det">${st.detection ? summary(st.detection) : ""}</div>`;
    } else if (step === 3) {
      body = `<label for="w-name">Name</label><input id="w-name" name="camera-name" autocomplete="off" value="${esc(st.name)}" placeholder="Family car">
        <details id="w-adv"><summary>Advanced</summary>
          <label for="w-driver">Driver</label><select id="w-driver">${Object.entries(DRIVERS).map(([k, t]) => `<option value="${k}" ${k === st.driver ? "selected" : ""}>${t}</option>`).join("")}</select>
          ${st.connection === "hotspot" ? `
          <label for="w-addressing">Address shape on the camera network</label><select id="w-addressing">${Object.entries(ADDRESSING).map(([k, t]) => `<option value="${k}" ${k === st.addressing ? "selected" : ""}>${t}</option>`).join("")}</select>
          <div class="hint">Automatic uses a small block of 4 addresses around the camera, and point-to-point (only 2 addresses) when one of them is taken at home.</div>
          <label for="w-static">Home Assistant address (CIDR)</label><input id="w-static" name="static-address" autocomplete="off" spellcheck="false" value="${esc(st.static_address)}" placeholder="picked by the app, e.g. 192.168.1.253/30 or 192.168.1.250/32">
          <div class="hint">Leave empty to let the app pick a free one. /32 means point-to-point.</div>` : ""}
          <label for="w-id">ID</label><input id="w-id" value="${esc(edit ? st.id : slug(st.name))}" disabled>
          <div class="hint">Used in folder names and entity IDs${edit ? "; it cannot change" : ""}.</div>
        </details>`;
    } else {
      body = `<div id="w-connect"></div>`;
    }
    box.innerHTML = `<div class="steps" aria-hidden="true">${STEPS.map((t, i) => `<span class="${i + 1 === step ? "on" : i + 1 < step ? "done" : ""}"></span>`).join("")}</div>
      <p class="step-title">Step ${Number(step)} of 4 · ${esc(STEPS[step - 1])}</p>
      ${body}
      <div id="w-err"></div>
      ${step < 4 ? `<div class="actions end">${step > (edit ? 2 : 1) ? '<button class="text" id="w-back">Back</button>' : ""}<button id="w-next" class="primary">${step === 3 ? "Save and connect" : "Next"}</button></div>` : ""}`;
    if ($("#w-back", box)) $("#w-back", box).onclick = () => { read(); st.step -= 1; draw(); };
    if ($("#w-next", box)) $("#w-next", box).onclick = next;
    if ($("#w-name", box)) $("#w-name", box).oninput = (e) => { if (!edit) $("#w-id", box).value = slug(e.target.value); };
    if ($("#w-camip", box)) $("#w-camip", box).onchange = (e) => { $("#w-camip-other", box).hidden = e.target.value !== "other"; };
    const bindAps = () => $$("button.ap", box).forEach((b) => {
      b.onclick = () => { st.ssid = b.dataset.ssid; $("#w-ssid", box).value = st.ssid; $("#w-aps", box).innerHTML = apList(); bindAps(); $("#w-pass", box).focus(); };
    });
    bindAps();
    if ($("#w-scan", box)) $("#w-scan", box).onclick = async () => {
      read();
      const btn = $("#w-scan", box); btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>`;
      note(`Asking Home Assistant which networks ${esc(st.interface)} can see… this takes a few seconds.`);
      try {
        const r = await api("wizard/wifi-scan?interface=" + encodeURIComponent(st.interface));
        if (!r.available) return note(SCAN_REASONS[r.reason] || `Scanning did not work (${esc(r.reason || "unknown")}). Type the camera's SSID.`);
        st.aps = r.accesspoints;
        $("#w-aps", box).innerHTML = apList(); bindAps();
        note("");
      } catch (e) { err(esc(e.message)); }
      finally { btn.disabled = false; btn.textContent = st.aps ? "Scan again" : "Scan"; }
    };
    if ($("#w-probe", box)) $("#w-probe", box).onclick = async () => {
      read();
      const btn = $("#w-probe", box); btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>`;
      try {
        const r = await api("wizard/probe", { method: "POST", body: { host: st.host } });
        if (!r.supported) return err("No driver recognised a camera there. Check the address, or capture a device profile.");
        st.host = r.host; st.detection = r; $("#w-host", box).value = r.host; $("#w-det", box).innerHTML = summary(r); err("");
      } catch (e) { err(esc(e.message)); }
      finally { btn.disabled = false; btn.textContent = "Test"; }
    };
    if (step === 4) connect();
    else (box.querySelector("input:not([type=hidden]):not([disabled]), select") || $("#w-next", box))?.focus();
  };

  const next = async () => {
    read();
    if (st.step === 2 && st.connection === "hotspot") {
      if (!st.interface) return err("Choose the Wi-Fi interface that reaches the car.");
      if (!st.ssid) return err("Enter the camera's SSID.");
      if (!edit && !st.password) return err("Enter the camera's Wi-Fi password.");
      if (!st.host) return err("Enter the camera's IP on its hotspot.");
    }
    if (st.step === 3) {
      if (!st.name) return err("Give the camera a name.");
      // Once saved (a step back to fix the password, say), the camera keeps its id.
      const body = { id: edit || st.id ? st.id : slug(st.name), name: st.name, connection: st.connection, driver: st.driver, enabled: existing ? existing.enabled : true };
      if (existing?.sync_mode) body.sync_mode = existing.sync_mode;
      if (st.host) body.host = st.host;
      // Always static: a DHCP profile from the camera would also take its gateway and DNS.
      if (st.connection === "hotspot") Object.assign(body, { interface: st.interface, ssid: st.ssid, wifi_password: st.password, ipv4: "static" });
      if (st.connection === "hotspot" && st.static_address) body.static_address = st.static_address;
      if (st.connection === "hotspot") body.addressing = st.addressing || "auto";
      const btn = $("#w-next", box); btn.disabled = true;
      try { const r = await api("wizard/camera", { method: "POST", body }); st.id = r.camera; }
      catch (e) { btn.disabled = false; return err(esc(e.message)); }
    }
    st.step += 1; draw();
  };

  const connect = async () => {
    const out = $("#w-connect", box);
    const detect = async (host) => {
      try {
        const r = await api("wizard/probe", { method: "POST", body: { host } });
        if (!r.supported) return null;
        let b = null;
        // The camera is saved by now: show the address actually in use, not a preview.
        if (st.connection === "hotspot") b = await api(`cameras/${st.id}/overlap`).catch(() => null)
          || await api("wizard/block", { method: "POST", body: { camera_ip: r.host.split(":")[0] } }).catch(() => null);
        return summary(r, b);
      } catch (e) { return null; }
    };
    const done = (html) => {
      clearInterval(poller);
      out.innerHTML = `${html}<div class="actions end"><button id="w-done" class="primary">Done</button></div>`;
      $("#w-done", box).onclick = d.close;
      $("#w-done", box).focus();
    };
    if (st.connection === "lan") {
      out.innerHTML = `<p class="muted"><span class="spinner"></span> Looking for the camera…</p>`;
      const html = await detect(st.host);
      return done(html || `<div class="card notice info">Saved. The camera did not answer yet; it is picked up as soon as it is reachable.</div>`);
    }
    const manual = (p, why) => `<div class="card notice" style="margin:12px 0 0"><b>Join the camera's Wi-Fi in Home Assistant</b>${why ? `<div class="muted">${why}</div>` : ""}
      <p>Open <a href="/config/network" target="_top">Settings › System › Network</a>, choose <b>${esc(p.interface)}</b>, then:</p>
      <table class="kv">${[["Wi-Fi mode", "Infrastructure"], ["SSID", p.ssid], ["Security", "WPA-PSK, with the camera password you entered"],
        ["IPv4", `Static · ${p.address || "?"}`], ["Gateway", p.gateway || "none"], ["DNS servers", "none"], ["IPv6", "Disabled"]]
        .map(([k, v]) => `<tr><td>${k}</td><td><code>${esc(v)}</code></td><td>${["SSID", "IPv4"].includes(k) && v ? `<button class="text copy" data-v="${esc(k === "IPv4" ? p.address || "" : v)}">Copy</button>` : ""}</td></tr>`).join("")}</table>
      <div class="hint">${p.gateway
        ? "Point-to-point: the camera as gateway gives Home Assistant a direct route to it. That route ranks below your wired connection, and no DNS keeps name resolution on Ethernet."
        : "No gateway and no DNS keep your default route and name resolution on Ethernet."}</div></div>`;
    const started = Date.now();
    const poll = async () => {
      let j;
      try { j = await api(`cameras/${st.id}/join`); } catch (e) { return; }
      const ifState = j.interface ? `<div class="hint">${esc(j.profile.interface)}: ${j.interface.connected ? `connected${j.interface.ssid ? " to " + esc(j.interface.ssid) : ""}` : j.interface.enabled ? "not connected" : "not configured"}</div>` : "";
      const slow = Date.now() - started > 90000 && ["joined", "manual"].includes(j.state)
        ? `<div class="card notice info" style="margin:12px 0 0"><b>Still no answer from the camera.</b><ul>
            <li>Is the camera on and the car within range? Check the signal under <a href="/config/network" target="_top">Settings › System › Network</a> › ${esc(j.profile.interface)} › Search networks.</li>
            <li>${j.interface && !j.interface.connected ? "The interface is not connected: the SSID or password may be wrong (Edit the camera)." : "The interface is connected: check the camera IP (Edit › Details); Home Assistant uses " + esc(j.profile.address || "?") + "."}</li>
            <li>You can close this; the camera is picked up whenever it is in range.</li></ul></div>` : "";
      const waiting = `<p class="muted"><span class="spinner"></span> Waiting for the camera to answer…</p>`;
      let html = "";
      if (j.state === "connecting") html = `<p class="muted"><span class="spinner"></span> Connecting to <b>${esc(j.profile.ssid)}</b> on ${esc(j.profile.interface)}…</p>`;
      else if (j.state === "requested") html = `<p class="muted"><span class="spinner"></span> Asking Home Assistant to join <b>${esc(j.profile.ssid)}</b> on ${esc(j.profile.interface)}…</p>`;
      else if (j.state === "joined") html = `<p><span class="badge ok">joined</span> Home Assistant joined <b>${esc(j.profile.ssid)}</b>.</p>${waiting}${ifState}${slow}`;
      else if (j.state === "failed") {
        // A wrong password or a camera out of range is fixed here, not by typing the profile by hand.
        const fixable = ["wrong_password", "not_in_range"].includes(j.reason);
        html = `<div class="card notice bad"><b>Could not join.</b> ${joinReason(j.reason)}</div>
          <div class="actions">${fixable ? `<button id="w-back">${j.reason === "wrong_password" ? "Change the password" : "Check the SSID"}</button>` : ""}<button id="w-retry">Try again</button></div>${fixable ? "" : manual(j.profile)}`;
      }
      else if (j.state === "manual") html = manual(j.profile, joinReason(j.reason)) + waiting + ifState + slow;
      if (j.camera_connected) {
        const det = await detect(st.host || j.profile.camera || "");
        return done(det || `<p><span class="badge ok">connected</span> The camera answers.</p>`);
      }
      out.innerHTML = html + `<div class="actions end"><button class="text" id="w-later">Close, finish later</button></div>`;
      $$("button.copy", out).forEach((b) => { b.onclick = () => { navigator.clipboard?.writeText(b.dataset.v); toast("Copied"); }; });
      $("#w-later", out).onclick = d.close;
      if ($("#w-retry", out)) $("#w-retry", out).onclick = async () => { await api(`cameras/${st.id}/join`, { method: "POST" }); poll(); };
      if ($("#w-back", out)) $("#w-back", out).onclick = () => { clearInterval(poller); st.password = ""; st.step = 2; draw(); };
    };
    const start = async () => {
      try { await api(`cameras/${st.id}/join`, { method: "POST" }); } catch (e) { out.innerHTML = `<div class="card notice bad">${esc(e.message)}</div>`; return; }
      await poll();
      poller = setInterval(poll, 2000);
    };
    // Joining goes through the integration: without it (or with another release of it)
    // the request is never answered, so say so up front instead of waiting for nothing.
    const gate = async () => {
      const info = await api("info").catch(() => null);
      const problem = info && pairingProblem(info.integration);
      if (!problem) return start();
      const j = await api(`cameras/${st.id}/join`).catch(() => null);
      out.innerHTML = `<div class="card notice bad">${problem}</div>
        <div class="actions"><button id="w-recheck" class="primary">Check again</button>${info.integration.connected ? `<button id="w-anyway">Continue anyway</button>` : ""}<button class="text" id="w-later">Close, finish later</button></div>
        ${j?.profile ? `<details><summary>Join the camera's Wi-Fi by hand instead</summary>${manual(j.profile)}</details>` : ""}`;
      $("#w-recheck", out).onclick = gate;
      $("#w-later", out).onclick = d.close;
      if ($("#w-anyway", out)) $("#w-anyway", out).onclick = start;
      $$("button.copy", out).forEach((b) => { b.onclick = () => { navigator.clipboard?.writeText(b.dataset.v); toast("Copied"); }; });
      // Poll while the user fixes it elsewhere, and carry on by itself once it is fixed.
      clearInterval(poller);
      poller = setInterval(async () => {
        const again = await api("info").catch(() => null);
        if (again && !pairingProblem(again.integration)) { clearInterval(poller); start(); }
      }, 5000);
    };
    await gate();
  };

  draw();
}

// ------------------------------------------------------------------ clips

async function clips() {
  const list = await api("cameras");
  const camId = route().query.get("camera") || list[0]?.options.id || "";
  const data = camId ? await api(`clips?camera=${encodeURIComponent(camId)}&limit=60`) : [];
  main.innerHTML = `<div class="toolbar"><h2>Clips</h2>
      ${list.length > 1 ? `<select id="cam" aria-label="Camera" style="width:auto">${list.map(({ options: o }) => `<option value="${esc(o.id)}" ${o.id === camId ? "selected" : ""}>${esc(o.name || o.id)}</option>`).join("")}</select>` : ""}</div>
    ${data.length ? "" : `<div class="card empty">No clips yet.</div>`}
    ${data.map((c) => `<div class="card clip" data-id="${c.id}">
      ${c.post_status === "done" ? `<img class="thumb" loading="lazy" width="128" height="72" src="api/v1/clips/${c.id}/thumb" alt="" onerror="this.style.visibility='hidden'">` : `<div class="thumb" role="img" aria-label="Preparing thumbnail"></div>`}
      <div class="meta"><div><span class="badge ${c.kind === "event" ? "warn" : ""}">${esc(c.kind)}</span> ${c.keep ? '<span class="badge info">kept</span>' : ""} <span class="muted">${esc(c.channel)}</span></div>
        <div>${when(c.start)}</div>
        <div class="small muted">${bytes(c.size)}${c.duration ? " · " + Math.round(c.duration) + " s" : ""} · ${c.copies.map((x) => (x.target === "local" ? "this HA" : x.target === "network" ? "disk/share" : esc(x.target))).join(", ")}</div></div>
      <div class="actions" style="margin:0"><button data-a="play">${c.kind === "photo" || /\.(jpe?g|png)$/i.test(c.path || "") ? "View" : "Play"}</button><button class="text" data-a="keep">${c.keep ? "Unkeep" : "Keep"}</button><button class="text danger" data-a="del">Delete</button></div></div>`).join("")}`;
  if ($("#cam")) $("#cam").onchange = (e) => { location.hash = "#clips?camera=" + e.target.value; };
  // Clips are post-processed in the background: refresh until their thumbnails exist.
  if (data.some((c) => c.post_status === "pending")) timer = setTimeout(() => { if (view === "clips" && !document.querySelector("dialog[open]")) clips().catch(() => {}); }, 3000);
  $$("[data-a]", main).forEach((b) => {
    b.onclick = async () => {
      const id = b.closest(".card").dataset.id, c = data.find((x) => String(x.id) === id);
      if (b.dataset.a === "play") {
        const d = openDialog(`${c.kind} · ${when(c.start)}`, { id: "player" });
        const photo = c.kind === "photo" || /\.(jpe?g|png)$/i.test(c.path || "");
        d.body.innerHTML = photo
          ? `<img class="photo" src="api/v1/clips/${id}/file" alt="Photo, ${esc(when(c.start))}">`
          : `<video controls autoplay playsinline src="api/v1/clips/${id}/file"></video>`;
      } else if (b.dataset.a === "keep") {
        await act(() => api(`clips/${id}/${c.keep ? "unkeep" : "keep"}`, { method: "POST" }), c.keep ? "No longer kept" : "Kept: never deleted by retention");
        clips();
      } else if (b.dataset.a === "del" && await confirmDialog("Delete this clip?", "It is deleted from every storage location. This cannot be undone.", "Delete", true)) {
        await act(() => api(`clips/${id}`, { method: "DELETE" }), "Clip deleted");
        clips();
      }
    };
  });
}

// ------------------------------------------------------------------ storage

const BLOCKED = {
  network_not_mounted: "The network share or disk is not attached. Check it under Settings › System › Storage.",
  network_not_writable: "The network share or disk cannot be written to.",
  local_not_writable: "The local folder cannot be written to.",
  already_running: "Clips are already being moved.",
  nothing_to_move: "Every clip is already where the settings put it.",
};
const TARGETS = { local: "this Home Assistant", network: "a disk or share", both: "this Home Assistant and a disk or share" };

async function storage() {
  const [s, loc] = await Promise.all([api("storage"), api("storage/location")]);
  const cur = loc.current;
  main.innerHTML = `<h2>Storage</h2>
    <div class="grid">${Object.values(s.targets).map(targetCard).join("")}</div>
    <div class="card" style="margin-top:12px">Clips are kept on <b>${esc(TARGETS[cur.target])}</b>${cur.target !== "network" ? ` · local folder <code>/media/${esc(cur.local_folder)}</code>` : ""}${cur.target !== "local" ? ` · <code>${esc(cur.network_folder)}</code>` : ""}. <a href="#settings">Change in Settings</a></div>
    <div id="move"></div>
    <h2>Retention</h2>
    <div class="card"><div>${s.last_purge ? `Last purge removed ${s.last_purge.count} copies (${bytes(s.last_purge.bytes_freed)}), ${ago(s.last_purge.ts ? new Date(s.last_purge.ts * 1000).toISOString() : null)}.` : "No purge has run yet."}</div>
      <div class="actions"><button id="dry">Dry run</button><button class="text danger" id="purge">Purge now</button></div>
      <div id="purge-out"></div></div>`;
  const show = (r) => { $("#purge-out").innerHTML = `<div style="margin-top:12px">${r.dry_run ? "Would remove" : "Removed"} <b>${r.count}</b> copies, ${bytes(r.bytes_freed)}.
    ${r.skipped_targets.length ? `<div class="muted">Skipped (offline): ${esc(r.skipped_targets.join(", "))}</div>` : ""}
    ${(r.clips || []).length ? `<div style="overflow-x:auto"><table><tr><th>Camera</th><th>Kind</th><th>Start</th><th>Where</th><th>Why</th></tr>${r.clips.slice(0, 50).map((c) => `<tr><td>${esc(c.camera)}</td><td>${esc(c.kind)}</td><td>${when(c.start)}</td><td>${esc(c.target)}</td><td class="muted">${esc(c.reason)}</td></tr>`).join("")}</table></div>` : ""}</div>`; };
  $("#dry").onclick = async () => show(await api("purge", { method: "POST", body: { dry_run: true } }));
  $("#purge").onclick = async () => {
    if (await confirmDialog("Run the purge now?", "Clips past their retention rules are deleted now instead of at the daily purge time.", "Purge now", true)) show(await api("purge", { method: "POST", body: {} }));
  };
  drawMove(await api("storage/migration"));
}

function drawMove(m) {
  const box = $("#move");
  if (!box) return;
  clearInterval(timer);
  const job = m.job, plan = m.plan;
  if (job.status === "running") {
    const pct = job.bytes_total ? (100 * job.bytes_done) / job.bytes_total : 0;
    box.innerHTML = `<div class="card"><div class="row"><b>Moving clips</b><button id="m-cancel">Cancel</button></div>
      <div class="bar"><div style="width:${pct}%"></div></div>
      <div>${job.clips_done} of ${job.clips_total} clips · ${bytes(job.bytes_done)} of ${bytes(job.bytes_total)}</div>
      <div class="muted small">${esc(job.current || "")}</div></div>`;
    $("#m-cancel").onclick = async () => drawMove(await api("storage/migration", { method: "DELETE" }));
    timer = setInterval(async () => { try { drawMove(await api("storage/migration")); } catch (e) { clearInterval(timer); } }, 2000);
    return;
  }
  const last = job.status === "done" ? `Last move: ${job.clips_done} clips, ${bytes(job.bytes_done)}${job.errors ? `, ${job.errors} could not be moved (see Logs)` : ""}.`
    : job.status === "cancelled" ? `The last move was cancelled after ${job.clips_done} of ${job.clips_total} clips. Start it again to continue.`
    : job.status === "failed" ? `The last move stopped: ${esc(job.error || "error")}. Start it again to continue.` : "";
  const notes = plan ? [
    plan.unavailable ? `${plan.unavailable} clips cannot be moved because their files cannot be reached.` : "",
    plan.stranded ? `${plan.stranded} copies are on a disk or share that is not attached; they are left as they are.` : "",
    plan.processing ? `${plan.processing} clips are still being processed and will be moved next time.` : "",
  ].filter(Boolean) : [];
  if (plan && plan.needed) {
    const where = Object.values(plan.destinations).map(esc).join(" and ");
    box.innerHTML = `<div class="card notice"><b>${plan.clips} clips (${bytes(plan.bytes)}) are not where the settings put them.</b>
      <div>Moving copies them to ${where}, checks each copy, and only then deletes the old one${plan.copies_to_remove ? ` (${plan.copies_to_remove} old copies)` : ""}.</div>
      ${plan.blocked ? `<div class="muted" id="m-blocked">${esc(BLOCKED[plan.blocked] || plan.blocked)}</div>` : ""}
      ${notes.map((n) => `<div class="muted">${n}</div>`).join("")}
      ${last ? `<div class="muted">${last}</div>` : ""}
      <div class="actions"><button id="m-start" class="primary" ${plan.blocked ? "disabled" : ""}>Move clips</button></div></div>`;
    $("#m-start").onclick = async () => {
      try { drawMove(await api("storage/migration", { method: "POST" })); }
      catch (e) { toast(BLOCKED[e.message] || e.message, "bad"); }
    };
    return;
  }
  box.innerHTML = last || notes.length ? `<div class="card" id="m-done">${[last || "Every clip is where the settings put it.", ...notes].map((n) => `<div>${n}</div>`).join("")}</div>` : "";
}

// ------------------------------------------------------------------ settings

const MODES = {
  events_only: ["Events only", "Event (incident) and parking clips. Best for slow hotspots."],
  events_first: ["Events first", "Events and parking clips, then the last few minutes of driving."],
  everything: ["Everything", "Every clip and photo, events first."],
};

async function settings() {
  const [cfg, loc] = await Promise.all([api("settings"), api("storage/location")]);
  const { sync: sy, storage: so, retention: re } = cfg;
  const names = loc.attached.map((a) => a.name);
  const netOptions = loc.attached.map((a) => `<option value="${esc(a.name)}" ${a.name === so.network_folder ? "selected" : ""}>${esc(a.name)} · ${a.kind} · ${esc(a.fstype)}</option>`).join("")
    + (so.network_folder && !names.includes(so.network_folder) ? `<option value="${esc(so.network_folder)}" selected>${esc(so.network_folder)} · not attached</option>` : "");
  const del = sy.delete_from_camera ? (sy.delete_events_from_camera ? "all" : "normal") : "keep";
  const num = (id, v, unit, hint) => `<label for="${id}">${unit}</label><input id="${id}" type="number" inputmode="numeric" min="0" ${id.endsWith("min_free_percent") ? 'max="90"' : ""} step="1" required value="${v}">${hint ? `<div class="hint">${hint}</div>` : ""}`;
  const retention = (p, title) => `<div class="card" id="${p}ret"><div class="title">${title}</div>
    <div class="grid">
      <div>${num(p + "max_total_gb", re[p + "max_total_gb"], "Size cap (GB)", "0 = no cap")}</div>
      <div>${num(p + "min_free_percent", re[p + "min_free_percent"], "Minimum free space (%)", "Oldest clips go first when below")}</div>
      <div>${num(p + "normal_days", re[p + "normal_days"], "Normal clips (days)")}</div>
      <div>${num(p + "parking_days", re[p + "parking_days"], "Parking clips (days)")}</div>
      <div>${num(p + "event_days", re[p + "event_days"], "Event clips (days)")}</div>
      <div>${num(p + "photo_days", re[p + "photo_days"], "Photos (days)")}</div>
    </div><div class="hint">0 days = no age limit (kept until space is needed). Clips marked Keep are never deleted.</div></div>`;
  main.innerHTML = `<h2>Sync</h2>
    <div class="card">
      <label>What to download when the camera is in range</label>
      <div class="choices">${Object.entries(MODES).map(([k, [t, d]]) => `<label class="choice"><input type="radio" name="s-mode" value="${k}" ${sy.mode === k ? "checked" : ""}><span><b>${t}</b><span class="muted">${d}</span></span></label>`).join("")}</div>
      <div class="grid" style="margin-top:4px"><div>${num("s-recent", sy.recent_minutes, "Recent driving to include (minutes)", "With Events first: the minutes before arrival")}</div>
      <div><label for="s-del">After a verified download</label><select id="s-del">
        <option value="keep" ${del === "keep" ? "selected" : ""}>Keep everything on the camera</option>
        <option value="normal" ${del === "normal" ? "selected" : ""}>Delete normal clips</option>
        <option value="all" ${del === "all" ? "selected" : ""}>Delete normal and event clips</option></select>
        <div class="hint">From the camera's card. Locked clips stay unless they are events and events are deleted too.</div></div></div>
      <details><summary>Advanced</summary>
        <label class="check"><input type="checkbox" id="s-remux" ${sy.remux_to_mp4 ? "checked" : ""}> Remux <code>.ts</code> to <code>.mp4</code> (no re-encoding)</label>
        <label class="check"><input type="checkbox" id="s-keep" ${so.keep_original ? "checked" : ""}> Keep the original <code>.ts</code> next to the <code>.mp4</code></label>
        <label class="check"><input type="checkbox" id="s-thumbs" ${sy.thumbnails ? "checked" : ""}> Make thumbnails</label>
      </details>
    </div>
    <h2>Storage</h2>
    <div class="card">
      <label for="l-target">Keep clips on</label>
      <select id="l-target">${Object.entries({ local: "This Home Assistant", network: "A disk or share", both: "Both" }).map(([v, t]) => `<option value="${v}" ${v === so.target ? "selected" : ""}>${t}</option>`).join("")}</select>
      <div id="l-local-row"><label for="l-local">Local folder (under /media)</label><input id="l-local" name="local-folder" autocomplete="off" spellcheck="false" value="${esc(so.local_folder)}"></div>
      <div id="l-net-row">
        <label for="l-net">Disk or share</label>
        ${netOptions ? `<select id="l-net">${netOptions}</select>` : `<div class="hint" id="l-net-none">None attached. Add a network share or a local disk (USB drive, SD card) under <a href="/config/storage" target="_top">Settings › System › Storage</a>, with usage Media or Share.</div>`}
        <label class="check"><input type="checkbox" id="l-spool" ${so.spool_when_network_down ? "checked" : ""}> Keep new clips locally while it is offline, then move them over</label>
      </div>
      <div class="hint" style="margin-top:12px">A change applies to new clips. Existing clips can be moved afterwards.</div>
    </div>
    <div id="move"></div>
    <h2>Retention</h2>
    ${retention("", "This Home Assistant")}
    ${retention("network_", "Disk or share")}
    <div class="card"><label for="r-time">Daily purge at</label><input id="r-time" type="time" value="${esc(re.purge_time)}"></div>
    <div class="savebar" id="savebar" hidden><span class="status" id="s-out">Unsaved changes</span>
      <button class="text" id="discard" disabled>Discard</button><button id="save" class="primary" disabled>Save</button></div>`;
  const vis = () => {
    const t = $("#l-target").value;
    $("#l-net-row").hidden = t === "local";
    $("#l-local-row").hidden = t === "network";
    $("#ret").hidden = t === "network";
    $("#network_ret").hidden = t === "local";
  };
  const snapshot = () => JSON.stringify($$("input, select", main).map((el) => (el.type === "checkbox" || el.type === "radio" ? el.checked : el.value)));
  let clean = snapshot();
  const dirty = () => {
    const changed = snapshot() !== clean;
    unsaved = changed;
    $("#savebar").classList.toggle("dirty", changed);
    if (changed) $("#savebar").hidden = false;
    $("#save").disabled = $("#discard").disabled = !changed;
    if (changed) $("#s-out").textContent = "Unsaved changes";
  };
  main.oninput = dirty;
  main.onchange = () => { vis(); dirty(); };
  $("#l-target").onchange = vis; vis();
  $("#discard").onclick = () => { unsaved = false; settings(); };
  $("#save").onclick = async () => {
    const bad = $$("input", main).find((el) => !el.closest("[hidden]") && !el.checkValidity());
    if (bad) {
      const label = main.querySelector(`label[for="${bad.id}"]`)?.textContent || bad.id;
      $("#s-out").innerHTML = `<span class="bad-text">${esc(label)}: ${esc(bad.validationMessage)}</span>`;
      bad.focus();
      return;
    }
    const n = (id) => Number($("#" + id).value);
    const delSel = $("#s-del").value;
    const body = {
      sync: {
        mode: main.querySelector("input[name=s-mode]:checked").value,
        recent_minutes: n("s-recent"),
        delete_from_camera: delSel !== "keep",
        delete_events_from_camera: delSel === "all",
        remux_to_mp4: $("#s-remux").checked,
        thumbnails: $("#s-thumbs").checked,
      },
      storage: { target: $("#l-target").value, local_folder: $("#l-local").value.trim(), spool_when_network_down: $("#l-spool").checked, keep_original: $("#s-keep").checked },
      retention: { purge_time: $("#r-time").value },
    };
    if ($("#l-net")) body.storage.network_folder = $("#l-net").value;
    for (const p of ["", "network_"]) for (const k of ["max_total_gb", "min_free_percent", "normal_days", "parking_days", "event_days", "photo_days"]) body.retention[p + k] = n(p + k);
    $("#save").disabled = true;
    try {
      const r = await api("settings", { method: "PATCH", body });
      clean = snapshot(); dirty();
      $("#s-out").textContent = "Saved.";
      setTimeout(() => { if (snapshot() === clean && $("#savebar")) $("#savebar").hidden = true; }, 2500);
      toast("Settings saved");
      if (r.migration) drawMove(r.migration);
    } catch (e) { $("#save").disabled = false; $("#s-out").innerHTML = `<span class="bad-text">${esc(e.message)}</span>`; }
  };
  drawMove(await api("storage/migration"));
}

// ------------------------------------------------------------------ logs

async function logs() {
  main.innerHTML = `<div class="toolbar"><h2>Logs</h2><label class="check" style="margin:0"><input type="checkbox" id="follow" checked> Follow</label></div>
    <div class="card" style="padding:12px"><pre id="log"></pre></div>`;
  let last = "";
  const draw = async () => {
    const r = await api("logs?lines=400");
    const text = r.lines.join("\n");
    if (text === last) return;
    last = text;
    const pre = $("#log");
    if (!pre) return;
    pre.textContent = text;
    if ($("#follow").checked) pre.scrollTop = pre.scrollHeight;
  };
  await draw();
  timer = setInterval(() => draw().catch(() => {}), 5000);
}

// ------------------------------------------------------------------ start

inheritTheme();
api("info").then((i) => { $("#version").textContent = `Dashcam Sync ${i.app_version}`; }).catch(() => {});
checkPairing();
setInterval(checkPairing, 15000);
render();
