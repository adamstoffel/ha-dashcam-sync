"use strict";
// All URLs are relative: Home Assistant serves this page under an ingress prefix.
const $ = (sel, el = document) => el.querySelector(sel);
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
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const bytes = (n) => {
  if (n == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
};
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "never");
const eta = (s) => (s == null ? "–" : s < 90 ? `${s} s` : `${Math.round(s / 60)} min`);

const ISSUES = {
  no_wifi_adapter: "No Wi-Fi adapter was found. Home Assistant OS needs a supported USB Wi-Fi adapter to join a dashcam hotspot.",
  camera_invalid: "A camera entry is invalid and was disabled.",
  subnet_overlap: "The dashcam's address block overlaps something on your home network. The camera is paused.",
  default_route_changed: "The default route moved to the dashcam network. The Wi-Fi connection must be removed.",
  camera_playback_mode: "A camera may be stuck in playback mode. Press Restore recording.",
  unsupported_camera: "No driver recognises this camera. Capture a device profile and open an issue.",
  listing_keeps_failing: "The camera answers but listing its files keeps failing.",
  network_target_offline: "The network storage has been offline for over an hour.",
  storage_low: "A storage target is below its minimum free space.",
};

let view = "overview", timer = null, es = null;

async function render() {
  clearInterval(timer);
  view = location.hash.slice(1).split("/")[0] || "overview";
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#" + view));
  try {
    await ({ overview, cameras, clips, storage, logs }[view] || overview)();
  } catch (e) {
    main.innerHTML = `<div class="card notice bad">${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", render);

async function overview() {
  const draw = async () => {
    const s = await api("status");
    const cams = Object.values(s.cameras);
    main.innerHTML = `
      ${s.issues.map((i) => `<div class="card notice ${i.key === "subnet_overlap" ? "bad" : ""}"><b>${esc(i.camera || "")}</b> ${esc(ISSUES[i.key] || i.key)}
        ${i.placeholders.conflicts?.length ? `<div class="muted">In conflict: ${esc(i.placeholders.conflicts.join(", "))}</div>` : ""}</div>`).join("")}
      <h2>Cameras</h2>
      ${cams.length ? "" : `<div class="card">No camera yet. <a href="#cameras">Add one</a> with the setup wizard.</div>`}
      <div class="grid">${cams.map((c) => `
        <div class="card">
          <div class="row"><b>${esc(c.name)}</b>
            <span class="pill ${c.connected ? "ok" : ""}">${c.connected ? "connected" : "away"}</span></div>
          <div class="muted">${esc(c.driver || "not detected")} ${esc(c.model || "")}</div>
          <div>Status: <b>${esc(c.sync_status)}</b>${c.current_file ? ` <span class="muted">${esc(c.current_file)}</span>` : ""}</div>
          ${c.clips_remaining ? `<div>${c.clips_remaining} clips, ${bytes(c.bytes_remaining)} left · ${bytes(c.speed_bps)}/s · ETA ${eta(c.eta_seconds)}</div>` : ""}
          <div class="muted">Last sync: ${when(c.last_sync)}</div>
          ${c.blocked_reasons.length ? `<div class="pill bad">paused: ${esc(c.blocked_reasons.join(", "))}</div>` : ""}
          ${c.restore_pending ? `<div class="pill warn">restore pending</div>` : ""}
        </div>`).join("")}</div>
      <h2>Storage</h2>
      ${Object.values(s.storage.targets).map((t) => `<div class="card"><div class="row"><b>${esc(t.name)}</b>
        <span class="pill ${t.online ? "ok" : "bad"}">${t.online ? "online" : "offline: " + esc(t.reason)}</span></div>
        <div class="muted">${esc(t.root)}</div>
        <div>Clips ${bytes(t.clip_bytes)} · free ${bytes(t.free_bytes)} of ${bytes(t.total_bytes)}</div></div>`).join("")}`;
  };
  await draw();
  timer = setInterval(() => draw().catch(() => {}), 3000);
}

async function cameras() {
  const list = await api("cameras");
  main.innerHTML = `
    <div class="row"><h2>Cameras</h2><button class="primary" id="add">Add camera</button></div>
    ${list.map(({ options: o, status: s }) => `
      <div class="card" data-id="${esc(o.id)}">
        <div class="row"><div><b>${esc(o.name || o.id)}</b> <span class="muted">${esc(o.connection)} · ${esc(o.id)}</span></div>
          <div>
            <button data-act="sync" ${s?.connected ? "" : "disabled"}>Sync now</button>
            <button data-act="restore">Restore recording</button>
            <button data-act="toggle">${o.enabled ? "Pause" : "Resume"}</button>
            ${o.connection === "hotspot" ? '<button data-act="overlap">Network check</button>' : ""}
            <button data-act="profile">Capture device profile</button>
            <button data-act="delete" class="danger">Remove</button>
          </div></div>
        <div class="muted">${esc(o.ssid || o.host || "")} ${o.static_address ? "· HA address " + esc(o.static_address) : ""}</div>
        <pre class="out" hidden></pre>
      </div>`).join("")}
    <div id="wizard"></div>`;
  $("#add").onclick = wizard;
  main.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = async () => {
      const card = b.closest(".card"), id = card.dataset.id, out = $(".out", card);
      const show = (v) => { out.hidden = false; out.textContent = typeof v === "string" ? v : JSON.stringify(v, null, 2); };
      try {
        const act = b.dataset.act;
        if (act === "sync") await api(`cameras/${id}/sync`, { method: "POST" });
        else if (act === "restore") await api(`cameras/${id}/restore`, { method: "POST" });
        else if (act === "toggle") { await api(`cameras/${id}`, { method: "PATCH", body: { enabled: b.textContent !== "Pause" } }); return cameras(); }
        else if (act === "overlap") show(await api(`cameras/${id}/overlap`, { method: "POST", body: {} }));
        else if (act === "profile") {
          const host = list.find((x) => x.options.id === id).options.host || prompt("Camera address");
          if (host) show(await api("wizard/capture-profile", { method: "POST", body: { host } }));
        } else if (act === "delete" && confirm(`Remove ${id}?`)) { await api(`cameras/${id}`, { method: "DELETE" }); return cameras(); }
        if (["sync", "restore"].includes(act)) show("Requested.");
      } catch (e) { show("Error: " + e.message); }
    };
  });
}

function wizard() {
  const w = $("#wizard");
  w.innerHTML = `<div class="card"><h2>Setup wizard</h2>
    <label>Name</label><input id="w-name" placeholder="Polestar 2">
    <label>Connection</label><select id="w-conn"><option value="hotspot">Camera's own Wi-Fi (hotspot)</option><option value="lan">Camera on my home network</option></select>
    <div id="w-hot">
      <label>Wi-Fi adapter</label><input id="w-if" value="wlan0">
      <label>Camera Wi-Fi name (SSID)</label><input id="w-ssid" list="aps"><datalist id="aps"></datalist>
      <label>Camera Wi-Fi password</label><input id="w-pass" type="password" autocomplete="off">
    </div>
    <label>Camera address (optional, auto-detected otherwise)</label><input id="w-host" placeholder="192.168.1.254">
    <p><button id="w-scan">Scan Wi-Fi</button> <button id="w-probe">Probe camera</button> <button id="w-save" class="primary">Save</button></p>
    <pre id="w-out" hidden></pre></div>`;
  const out = (v) => { const el = $("#w-out"); el.hidden = false; el.textContent = typeof v === "string" ? v : JSON.stringify(v, null, 2); };
  const conn = $("#w-conn");
  conn.onchange = () => { $("#w-hot").hidden = conn.value !== "hotspot"; };
  $("#w-scan").onclick = async () => {
    try {
      const r = await api("wizard/wifi-scan?interface=" + encodeURIComponent($("#w-if").value));
      if (!r.available) return out("Scanning is not available here. Type the camera's Wi-Fi name.");
      $("#aps").innerHTML = r.accesspoints.map((a) => `<option value="${esc(a.ssid)}">`).join("");
      out(`${r.accesspoints.length} networks found.`);
    } catch (e) { out(e.message); }
  };
  $("#w-probe").onclick = async () => {
    try {
      const r = await api("wizard/probe", { method: "POST", body: { host: $("#w-host").value } });
      if (!r.supported) return out("No driver recognised a camera. Join its Wi-Fi from Home Assistant first, or capture a device profile.");
      $("#w-host").value = r.host;
      const b = await api("wizard/block", { method: "POST", body: { camera_ip: r.host.split(":")[0] } });
      out({ ...r, address_block: b.block, home_address: b.home_address, overlaps_home_network: b.overlaps_home_network });
    } catch (e) { out(e.message); }
  };
  $("#w-save").onclick = async () => {
    const name = $("#w-name").value.trim();
    const id = (name || "camera").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 32) || "camera";
    const body = { id, name, connection: conn.value, driver: "auto", enabled: true };
    if (conn.value === "hotspot") Object.assign(body, { interface: $("#w-if").value, ssid: $("#w-ssid").value, wifi_password: $("#w-pass").value });
    if ($("#w-host").value) body.host = $("#w-host").value;
    try { await api("wizard/camera", { method: "POST", body }); out("Saved. Open the Dashcam Sync integration in Home Assistant to join the Wi-Fi."); setTimeout(cameras, 900); }
    catch (e) { out(e.message); }
  };
}

async function clips() {
  const list = await api("cameras");
  const camId = new URLSearchParams(location.hash.split("?")[1] || "").get("camera") || list[0]?.options.id || "";
  const data = camId ? await api(`clips?camera=${encodeURIComponent(camId)}&limit=60`) : [];
  main.innerHTML = `
    <div class="row"><h2>Clips</h2>
      <select id="cam">${list.map(({ options: o }) => `<option value="${esc(o.id)}" ${o.id === camId ? "selected" : ""}>${esc(o.name || o.id)}</option>`).join("")}</select></div>
    <div id="player"></div>
    ${data.length ? "" : `<div class="card muted">No clips yet.</div>`}
    ${data.map((c) => `<div class="card row" data-id="${c.id}">
      ${c.post_status === "done" ? `<img class="thumb" loading="lazy" src="api/v1/clips/${c.id}/thumb" alt="" onerror="this.style.visibility='hidden'">` : `<div class="thumb" role="img" aria-label="Preparing thumbnail"></div>`}
      <div style="flex:1"><b>${esc(c.kind)}</b> · ${esc(c.channel)} · ${when(c.start)}<br><span class="muted">${bytes(c.size)}${c.duration ? " · " + Math.round(c.duration) + " s" : ""} · ${c.copies.map((x) => esc(x.target)).join(", ")}</span></div>
      <div><button data-a="play">Play</button> <button data-a="keep">${c.keep ? "Unkeep" : "Keep"}</button> <button data-a="del" class="danger">Delete</button></div></div>`).join("")}`;
  $("#cam").onchange = (e) => { location.hash = "#clips?camera=" + e.target.value; };
  // Clips are post-processed in the background: refresh until their thumbnails exist.
  if (data.some((c) => c.post_status === "pending") && !$("video")) timer = setTimeout(() => { if (view === "clips") clips().catch(() => {}); }, 3000);
  main.querySelectorAll("[data-a]").forEach((b) => {
    b.onclick = async () => {
      const card = b.closest(".card"), id = card.dataset.id;
      if (b.dataset.a === "play") $("#player").innerHTML = `<video controls autoplay src="api/v1/clips/${id}/file"></video>`;
      else if (b.dataset.a === "keep") { await api(`clips/${id}/${b.textContent === "Keep" ? "keep" : "unkeep"}`, { method: "POST" }); clips(); }
      else if (b.dataset.a === "del" && confirm("Delete this clip from all storage?")) { await api(`clips/${id}`, { method: "DELETE" }); clips(); }
    };
  });
}

async function storage() {
  const s = await api("storage");
  main.innerHTML = `<h2>Storage</h2>
    <div class="card muted">Mode: <b>${esc(s.mode)}</b>${s.last_purge ? ` · last purge removed ${s.last_purge.count} copies (${bytes(s.last_purge.bytes_freed)})` : ""}</div>
    ${Object.values(s.targets).map((t) => {
      const used = t.total_bytes ? 100 - (100 * t.free_bytes) / t.total_bytes : 0;
      return `<div class="card"><div class="row"><b>${esc(t.name)}</b><span class="pill ${t.online ? "ok" : "bad"}">${t.online ? "online" : esc(t.reason || "offline")}</span></div>
        <div class="muted">${esc(t.root)}</div><div class="bar" title="${used.toFixed(0)}% used"><div style="width:${used}%"></div></div>
        <div>Clips ${bytes(t.clip_bytes)} · free ${bytes(t.free_bytes)} of ${bytes(t.total_bytes)}</div></div>`;
    }).join("")}
    <h2>Retention</h2>
    <p><button id="dry">Dry run</button> <button id="purge" class="danger">Purge now</button></p>
    <div id="purge-out"></div>`;
  const show = (r) => { $("#purge-out").innerHTML = `<div class="card">${r.dry_run ? "Would remove" : "Removed"} <b>${r.count}</b> copies, ${bytes(r.bytes_freed)}.
    ${r.skipped_targets.length ? `<div class="muted">Skipped (offline): ${esc(r.skipped_targets.join(", "))}</div>` : ""}
    <table>${(r.clips || []).slice(0, 50).map((c) => `<tr><td>${esc(c.camera)}</td><td>${esc(c.kind)}</td><td>${when(c.start)}</td><td>${esc(c.target)}</td><td class="muted">${esc(c.reason)}</td></tr>`).join("")}</table></div>`; };
  $("#dry").onclick = async () => show(await api("purge", { method: "POST", body: { dry_run: true } }));
  $("#purge").onclick = async () => { if (confirm("Run the purge now?")) show(await api("purge", { method: "POST", body: {} })); };
}

async function logs() {
  const draw = async () => { const r = await api("logs?lines=400"); main.innerHTML = `<h2>Logs</h2><pre>${esc(r.lines.join("\n"))}</pre>`; };
  await draw();
  timer = setInterval(() => draw().catch(() => {}), 5000);
}

render();
