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
  view = location.hash.slice(1).split(/[/?]/)[0] || "overview";
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#" + view));
  try {
    await ({ overview, cameras, clips, storage, settings, logs }[view] || overview)();
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

const DRIVERS = { auto: "Detect automatically", novatek: "Novatek", viidure_app: "Viidure app (/app)", http_index: "Plain HTTP file index" };
const CAMERA_IPS = { "192.168.1.254": "Novatek-based cameras (most common)", "192.168.169.1": "cameras set up with the Viidure app" };
const SCAN_REASONS = {
  integration_not_connected: "Scanning goes through Home Assistant, and the Dashcam Sync integration is not connected. Type the camera's SSID.",
  no_answer: "Home Assistant did not answer the scan. Type the camera's SSID.",
};
const JOIN_REASONS = {
  integration_not_connected: "The Dashcam Sync integration is not connected to this app, so Home Assistant cannot be asked to join.",
  no_answer: "Home Assistant did not answer the join request.",
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
const ago = (iso) => {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : when(iso);
};
const OVERLAP = {
  no_overlap: ["ok", "No overlap with your home network."],
  overlap_block_free: ["ok", "Overlaps your home network, but the reserved addresses are free."],
  device_in_block: ["bad", "A device on your network uses an address in the reserved block."],
  address_in_use: ["bad", "Home Assistant's address on the camera network is in use on your network."],
  camera_collides: ["bad", "Something on your network has the camera's address."],
};

async function cameras() {
  const list = await api("cameras");
  main.innerHTML = `
    <div class="row"><h2>Cameras</h2><button class="primary" id="add">Add camera</button></div>
    ${list.length ? "" : `<div class="card muted">No camera yet.</div>`}
    ${list.map(({ options: o, status: s }) => `
      <div class="card" data-id="${esc(o.id)}">
        <div class="row"><div><b>${esc(o.name || o.id)}</b>
            <span class="pill ${!o.enabled ? "warn" : s?.connected ? "ok" : ""}">${!o.enabled ? "paused" : s?.connected ? "connected" : "not in range"}</span>
            ${s && s.sync_status !== "idle" ? `<span class="pill">${esc(s.sync_status)}</span>` : ""}</div>
          <div>
            <button data-act="sync" ${s?.connected && o.enabled ? "" : "disabled"}>Sync now</button>
            <button data-act="edit">Edit</button>
            ${o.connection === "hotspot" ? '<button data-act="overlap">Network check</button>' : ""}
            <details class="more"><summary>More</summary>
              <button data-act="restore">Restore recording</button>
              <button data-act="toggle">${o.enabled ? "Pause" : "Resume"}</button>
              <button data-act="profile">Capture device profile</button>
              <button data-act="delete" class="danger">Remove</button>
            </details>
          </div></div>
        <div class="muted">${o.connection === "hotspot"
          ? `Camera hotspot · ${esc(o.interface || "?")} · ${esc(o.ssid || "?")}${o.static_address ? ` · HA ${esc(o.static_address)}` : ""}`
          : `Local network · ${esc(o.host || "auto-detect")}`}
          · last sync ${ago(s?.last_sync)}${s?.error ? ` · <span class="bad-text">${esc(s.error)}</span>` : ""}</div>
        <div class="out" hidden></div>
      </div>`).join("")}
    <div id="wizard"></div>`;
  $("#add").onclick = () => wizard();
  main.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = async () => {
      const card = b.closest(".card"), id = card.dataset.id, out = $(".out", card);
      const show = (html) => { out.hidden = false; out.innerHTML = html; };
      try {
        const act = b.dataset.act;
        if (act === "sync") { await api(`cameras/${id}/sync`, { method: "POST" }); show("Sync requested."); }
        else if (act === "restore") { await api(`cameras/${id}/restore`, { method: "POST" }); show("Restore requested."); }
        else if (act === "edit") wizard(list.find((x) => x.options.id === id).options);
        else if (act === "toggle") { await api(`cameras/${id}`, { method: "PATCH", body: { enabled: b.textContent !== "Pause" } }); return cameras(); }
        else if (act === "overlap") {
          const r = await api(`cameras/${id}/overlap`, { method: "POST", body: {} });
          const [cls, text] = OVERLAP[r.result] || ["warn", r.result];
          show(`<span class="pill ${cls}">${esc(text)}</span> <span class="muted">Reserved ${esc(r.block || "")}${r.conflicts?.length ? ` · in use: ${esc(r.conflicts.join(", "))}` : ""}</span>`);
        } else if (act === "profile") {
          const host = list.find((x) => x.options.id === id).options.host || prompt("Camera address (IP or host:port)");
          if (!host) return;
          const bundle = await api("wizard/capture-profile", { method: "POST", body: { host } });
          const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }));
          show(`Captured. <a href="${url}" download="device-profile-${esc(id)}.json">Download the profile</a> and attach it to a GitHub issue.`);
        } else if (act === "delete" && confirm(`Remove ${id}? Downloaded clips are kept.`)) { await api(`cameras/${id}`, { method: "DELETE" }); return cameras(); }
      } catch (e) { show(`<span class="bad-text">${esc(e.message)}</span>`); }
    };
  });
}

// ------------------------------------------------------------------ setup wizard

async function wizard(existing) {
  const box = $("#wizard");
  const edit = !!existing;
  const st = {
    id: existing?.id || "", name: existing?.name || "", connection: existing?.connection || "hotspot",
    interface: existing?.interface || "", ssid: existing?.ssid || "", password: "",
    host: existing?.host || "", driver: existing?.driver || "auto", ipv4: existing?.ipv4 || "static",
    static_address: existing?.static_address || "", step: edit ? 2 : 1, detection: null,
  };
  let ifaces = { known: false, wireless: [] };
  try { ifaces = await api("wizard/interfaces"); } catch (e) { /* listed as unavailable below */ }
  if (!st.interface) {
    const free = ifaces.wireless.filter((i) => !i.in_use_by || i.in_use_by === st.id);
    if (free.length) st.interface = free[0].name;
  }
  const STEPS = ["Connection", "Details", "Name", "Connect"];
  const camIp = () => (!st.host ? "192.168.1.254" : CAMERA_IPS[st.host.split(":")[0]] ? st.host.split(":")[0] : "other");
  const slug = (n) => (n || "camera").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 32) || "camera";
  const read = () => {
    const v = (id) => { const el = $("#" + id); return el ? el.value.trim() : undefined; };
    for (const [k, id] of [["interface", "w-if"], ["ssid", "w-ssid"], ["host", "w-host"], ["name", "w-name"], ["driver", "w-driver"], ["ipv4", "w-ipv4"], ["static_address", "w-static"]]) {
      const val = v(id); if (val !== undefined) st[k] = val;
    }
    const pw = $("#w-pass"); if (pw) st.password = pw.value;
    const ip = $("#w-camip");
    if (ip) st.host = ip.value === "other" ? $("#w-camip-other").value.trim() : ip.value;
    const conn = box.querySelector("input[name=w-conn]:checked"); if (conn) st.connection = conn.value;
  };
  const err = (msg) => { $("#w-err").innerHTML = msg ? `<div class="card notice bad">${msg}</div>` : ""; };
  const note = (msg) => { $("#w-err").innerHTML = msg ? `<div class="card notice info">${msg}</div>` : ""; };
  const ifaceOptions = () => ifaces.wireless.map((i) => {
    const note = i.in_use_by && i.in_use_by !== st.id ? `used by ${i.in_use_by}` : i.connected ? `connected${i.ssid ? " to " + i.ssid : ""}` : i.enabled ? "not connected" : "not configured · free";
    return `<option value="${esc(i.name)}" ${i.name === st.interface ? "selected" : ""}>${esc(i.name)} · ${esc(note)}</option>`;
  }).join("");
  const summary = (r, b) => {
    const caps = Object.entries(r.capabilities || {}).filter(([, on]) => on).map(([k]) => `<span class="pill ok">${esc(k.replace(/_/g, " "))}</span>`).join(" ");
    return `<div class="card"><b>Detected a ${esc(r.driver)} camera</b> at ${esc(r.host)}${r.firmware ? ` · firmware ${esc(r.firmware)}` : ""}${r.model ? ` · ${esc(r.model)}` : ""}
      <div>${caps || ""}</div>
      ${b ? `<div class="muted">Reserved address block ${esc(b.block)} · Home Assistant uses ${esc(b.static_address)}
        ${b.overlaps_home_network ? '<span class="pill warn">overlaps your home network: only these 4 addresses are claimed</span>' : '<span class="pill ok">no overlap with your home network</span>'}</div>` : ""}</div>`;
  };

  const draw = () => {
    clearInterval(timer);
    const step = st.step;
    let body = "";
    if (step === 1) {
      body = `<label>How does the camera connect?</label>
        <p><label class="choice"><input type="radio" name="w-conn" value="hotspot" ${st.connection === "hotspot" ? "checked" : ""}> <b>Camera hotspot</b> <span class="muted">· the camera runs its own Wi-Fi; Home Assistant joins it with a Wi-Fi interface</span></label>
        <label class="choice"><input type="radio" name="w-conn" value="lan" ${st.connection === "lan" ? "checked" : ""}> <b>Local network</b> <span class="muted">· the camera is a client on your LAN</span></label></p>`;
    } else if (step === 2 && st.connection === "hotspot") {
      body = `<label for="w-if">Wi-Fi interface</label>
        ${ifaces.wireless.length ? `<select id="w-if">${ifaceOptions()}</select>`
          : `<input id="w-if" value="${esc(st.interface)}" placeholder="wlan0"><div class="muted">${ifaces.known ? "Home Assistant reports no Wi-Fi interface. A supported USB Wi-Fi adapter is the usual fix." : "Interfaces are not available here; type the name shown under Settings › System › Network."}</div>`}
        <label for="w-ssid">Camera SSID</label>
        <div class="row" style="justify-content:flex-start"><input id="w-ssid" list="aps" value="${esc(st.ssid)}"><datalist id="aps"></datalist> <button id="w-scan">Scan</button></div>
        <div id="w-aps"></div>
        <label for="w-pass">Camera Wi-Fi password</label>
        <input id="w-pass" type="password" autocomplete="off" placeholder="${edit ? "leave blank to keep the current one" : ""}">
        <label for="w-camip">Camera IP on its hotspot</label>
        <select id="w-camip">${Object.entries(CAMERA_IPS).map(([ip, t]) => `<option value="${ip}" ${camIp() === ip ? "selected" : ""}>${ip} · ${t}</option>`).join("")}
          <option value="other" ${camIp() === "other" ? "selected" : ""}>Other…</option></select>
        <input id="w-camip-other" value="${camIp() === "other" ? esc(st.host) : ""}" placeholder="e.g. 192.168.0.1" ${camIp() === "other" ? "" : "hidden"}>
        <div class="muted">Shown in the camera's phone app or manual. Home Assistant's address on the camera network is picked next to it.</div>`;
    } else if (step === 2) {
      body = `<label for="w-host">Camera IP or host</label>
        <div class="row" style="justify-content:flex-start"><input id="w-host" value="${esc(st.host)}" placeholder="192.168.2.50"> <button id="w-probe">Test</button></div>
        <div class="muted">Leave empty to look for the camera automatically. Give it a DHCP reservation in your router so the address does not change.</div>
        <div id="w-det">${st.detection ? summary(st.detection) : ""}</div>`;
    } else if (step === 3) {
      body = `<label for="w-name">Name</label><input id="w-name" value="${esc(st.name)}" placeholder="Polestar 2">
        <details id="w-adv"><summary>Advanced</summary>
          <label for="w-driver">Driver</label><select id="w-driver">${Object.entries(DRIVERS).map(([k, t]) => `<option value="${k}" ${k === st.driver ? "selected" : ""}>${t}</option>`).join("")}</select>
          ${st.connection === "hotspot" ? `
          <label for="w-ipv4">IPv4 on the camera network</label><select id="w-ipv4"><option value="static" ${st.ipv4 !== "auto" ? "selected" : ""}>Static, small block around the camera (recommended)</option><option value="auto" ${st.ipv4 === "auto" ? "selected" : ""}>DHCP from the camera</option></select>
          <label for="w-static">Home Assistant address (CIDR)</label><input id="w-static" value="${esc(st.static_address)}" placeholder="computed, e.g. 192.168.1.253/30">` : ""}
          <label>ID</label><input value="${esc(edit ? st.id : slug(st.name))}" disabled>
          <div class="muted">Used in folder names and entity IDs${edit ? "; it cannot change" : ""}.</div>
        </details>`;
    } else {
      body = `<div id="w-connect"></div>`;
    }
    box.innerHTML = `<div class="card"><div class="row"><h2>${edit ? "Edit " + esc(st.name || st.id) : "Add camera"}</h2><button id="w-close">Close</button></div>
      <div class="steps">${STEPS.map((t, i) => `<span class="${i + 1 === step ? "on" : i + 1 < step ? "done" : ""}">${i + 1}. ${t}</span>`).join("")}</div>
      ${body}
      <div id="w-err"></div>
      ${step < 4 ? `<p>${step > (edit ? 2 : 1) ? '<button id="w-back">Back</button> ' : ""}<button id="w-next" class="primary">${step === 3 ? "Save and connect" : "Next"}</button></p>` : ""}</div>`;
    $("#w-close").onclick = () => { clearInterval(timer); box.innerHTML = ""; cameras(); };
    if ($("#w-back")) $("#w-back").onclick = () => { read(); st.step -= 1; draw(); };
    if ($("#w-next")) $("#w-next").onclick = next;
    if ($("#w-name")) $("#w-name").oninput = (e) => { const id = $("#w-adv input[disabled]"); if (id && !edit) id.value = slug(e.target.value); };
    if ($("#w-camip")) $("#w-camip").onchange = (e) => { $("#w-camip-other").hidden = e.target.value !== "other"; };
    if ($("#w-scan")) $("#w-scan").onclick = async () => {
      read();
      const btn = $("#w-scan"); btn.disabled = true; btn.textContent = "Scanning…";
      note(`Asking Home Assistant which networks ${esc(st.interface)} can see…`);
      try {
        const r = await api("wizard/wifi-scan?interface=" + encodeURIComponent(st.interface));
        if (!r.available) return note(SCAN_REASONS[r.reason] || `Scanning did not work (${esc(r.reason || "unknown")}). Type the camera's SSID.`);
        $("#aps").innerHTML = r.accesspoints.map((a) => `<option value="${esc(a.ssid)}">`).join("");
        $("#w-aps").innerHTML = r.accesspoints.length
          ? `<div class="aps">${r.accesspoints.map((a) => `<button class="ap" data-ssid="${esc(a.ssid)}">${esc(a.ssid)} <span class="muted">${a.signal == null ? "" : a.signal + "%"}</span></button>`).join("")}</div>`
          : `<div class="muted">No networks found. Is the camera on and in range?</div>`;
        $("#w-aps").querySelectorAll("button.ap").forEach((b) => { b.onclick = () => { $("#w-ssid").value = b.dataset.ssid; }; });
        note("");
      } catch (e) { err(esc(e.message)); }
      finally { btn.disabled = false; btn.textContent = "Scan"; }
    };
    if ($("#w-probe")) $("#w-probe").onclick = async () => {
      read();
      try {
        const r = await api("wizard/probe", { method: "POST", body: { host: st.host } });
        if (!r.supported) return err("No driver recognised a camera there. Check the address, or capture a device profile.");
        st.host = r.host; st.detection = r; $("#w-host").value = r.host; $("#w-det").innerHTML = summary(r); err("");
      } catch (e) { err(esc(e.message)); }
    };
    if (step === 4) connect();
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
      const body = { id: edit ? st.id : slug(st.name), name: st.name, connection: st.connection, driver: st.driver, enabled: existing ? existing.enabled : true };
      if (existing?.sync_mode) body.sync_mode = existing.sync_mode;
      if (st.host) body.host = st.host;
      if (st.connection === "hotspot") Object.assign(body, { interface: st.interface, ssid: st.ssid, wifi_password: st.password, ipv4: st.ipv4 });
      if (st.connection === "hotspot" && st.static_address) body.static_address = st.static_address;
      try { const r = await api("wizard/camera", { method: "POST", body }); st.id = r.camera; }
      catch (e) { return err(esc(e.message)); }
    }
    st.step += 1; draw();
  };

  const connect = async () => {
    const out = $("#w-connect");
    const detect = async (host) => {
      try {
        const r = await api("wizard/probe", { method: "POST", body: { host } });
        if (!r.supported) return null;
        let b = null;
        if (st.connection === "hotspot") b = await api("wizard/block", { method: "POST", body: { camera_ip: r.host.split(":")[0] } }).catch(() => null);
        return summary(r, b);
      } catch (e) { return null; }
    };
    const done = (html) => { clearInterval(timer); out.innerHTML = `${html}<p><button id="w-done" class="primary">Done</button></p>`; $("#w-done").onclick = () => { box.innerHTML = ""; cameras(); }; };
    if (st.connection === "lan") {
      out.innerHTML = `<div class="muted">Looking for the camera…</div>`;
      const html = await detect(st.host);
      return done(html || `<div class="card notice">Saved. The camera did not answer yet; it is picked up as soon as it is reachable.</div>`);
    }
    const manual = (p, why) => `<div class="card notice"><b>Join the camera's Wi-Fi in Home Assistant</b>${why ? `<div class="muted">${why}</div>` : ""}
      <div>Open <a href="/config/network" target="_top">Settings › System › Network</a>, choose <b>${esc(p.interface)}</b>, then:</div>
      <table class="kv">${[["Wi-Fi mode", "Infrastructure"], ["SSID", p.ssid], ["Security", "WPA-PSK, with the camera password you entered"],
        ["IPv4", p.ipv4 === "auto" ? "Automatic (DHCP)" : `Static · ${p.address || "?"}`], ["Gateway", "none"], ["DNS servers", "none"], ["IPv6", "Disabled"]]
        .map(([k, v]) => `<tr><td class="muted">${k}</td><td><code>${esc(v)}</code></td><td>${["SSID", "IPv4"].includes(k) && v ? `<button class="copy" data-v="${esc(k === "IPv4" ? p.address || "" : v)}">Copy</button>` : ""}</td></tr>`).join("")}</table>
      <div class="muted">No gateway and no DNS keep your default route and name resolution on Ethernet.</div></div>`;
    const started = Date.now();
    const poll = async () => {
      let j;
      try { j = await api(`cameras/${st.id}/join`); } catch (e) { return; }
      const ifState = j.interface ? `<div class="muted">${esc(j.profile.interface)}: ${j.interface.connected ? `connected${j.interface.ssid ? " to " + esc(j.interface.ssid) : ""}` : j.interface.enabled ? "not connected" : "not configured"}</div>` : "";
      const slow = Date.now() - started > 90000 && ["joined", "manual"].includes(j.state)
        ? `<div class="card notice info"><b>Still no answer from the camera.</b><ul>
            <li>Is the camera on and the car within range? Check the signal under <a href="/config/network" target="_top">Settings › System › Network</a> › ${esc(j.profile.interface)} › Search networks.</li>
            <li>${j.interface && !j.interface.connected ? "The interface is not connected: the SSID or password may be wrong (Edit the camera)." : "The interface is connected: check the camera IP (Edit › Details); Home Assistant uses " + esc(j.profile.address || "?") + "."}</li>
            <li>Waiting continues; the camera is also picked up later, whenever it is in range.</li></ul></div>` : "";
      let html = "";
      if (j.state === "requested") html = `<div class="muted">Asking Home Assistant to join <b>${esc(j.profile.ssid)}</b> on ${esc(j.profile.interface)}…</div>`;
      else if (j.state === "joined") html = `<div><span class="pill ok">joined</span> Waiting for the camera to answer…</div>${ifState}${slow}`;
      else if (j.state === "failed") html = `<div class="card notice bad"><b>Could not join.</b> ${joinReason(j.reason)}</div><p><button id="w-retry">Try again</button></p>${manual(j.profile)}`;
      else if (j.state === "manual") html = manual(j.profile, joinReason(j.reason)) + `<div class="muted">Waiting for the camera to answer…</div>${ifState}${slow}`;
      if (j.camera_connected) {
        const det = await detect(st.host || j.profile.camera || "");
        return done(det || `<div><span class="pill ok">connected</span> The camera answers.</div>`);
      }
      out.innerHTML = html;
      out.querySelectorAll("button.copy").forEach((b) => { b.onclick = () => navigator.clipboard?.writeText(b.dataset.v); });
      if ($("#w-retry")) $("#w-retry").onclick = async () => { await api(`cameras/${st.id}/join`, { method: "POST" }); poll(); };
    };
    try { await api(`cameras/${st.id}/join`, { method: "POST" }); } catch (e) { out.innerHTML = `<div class="card notice bad">${esc(e.message)}</div>`; return; }
    await poll();
    timer = setInterval(poll, 2000);
  };

  draw();
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
    <div class="card muted">Mode: <b>${esc(s.mode)}</b>${s.last_purge ? ` · last purge removed ${s.last_purge.count} copies (${bytes(s.last_purge.bytes_freed)})` : ""}</div>
    ${Object.values(s.targets).map((t) => {
      const used = t.total_bytes ? 100 - (100 * t.free_bytes) / t.total_bytes : 0;
      return `<div class="card"><div class="row"><b>${esc(t.name)}</b><span class="pill ${t.online ? "ok" : "bad"}">${t.online ? "online" : esc(t.reason || "offline")}</span></div>
        <div class="muted">${esc(t.root)}</div><div class="bar" title="${used.toFixed(0)}% used"><div style="width:${used}%"></div></div>
        <div>Clips ${bytes(t.clip_bytes)} · free ${bytes(t.free_bytes)} of ${bytes(t.total_bytes)}</div></div>`;
    }).join("")}
    <div class="card">Clips are kept on <b>${esc(TARGETS[cur.target])}</b>${cur.target !== "network" ? ` · local folder <code>/media/${esc(cur.local_folder)}</code>` : ""}${cur.target !== "local" ? ` · <code>${esc(cur.network_folder)}</code>` : ""}. <a href="#settings">Change in Settings</a></div>
    <div id="move"></div>
    <h2>Retention</h2>
    <p><button id="dry">Dry run</button> <button id="purge" class="danger">Purge now</button></p>
    <div id="purge-out"></div>`;
  const show = (r) => { $("#purge-out").innerHTML = `<div class="card">${r.dry_run ? "Would remove" : "Removed"} <b>${r.count}</b> copies, ${bytes(r.bytes_freed)}.
    ${r.skipped_targets.length ? `<div class="muted">Skipped (offline): ${esc(r.skipped_targets.join(", "))}</div>` : ""}
    <table>${(r.clips || []).slice(0, 50).map((c) => `<tr><td>${esc(c.camera)}</td><td>${esc(c.kind)}</td><td>${when(c.start)}</td><td>${esc(c.target)}</td><td class="muted">${esc(c.reason)}</td></tr>`).join("")}</table></div>`; };
  $("#dry").onclick = async () => show(await api("purge", { method: "POST", body: { dry_run: true } }));
  $("#purge").onclick = async () => { if (confirm("Run the purge now?")) show(await api("purge", { method: "POST", body: {} })); };
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
      <div class="muted">${esc(job.current || "")}</div></div>`;
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
      <p><button id="m-start" class="primary" ${plan.blocked ? "disabled" : ""}>Move clips</button></p></div>`;
    $("#m-start").onclick = async () => {
      try { drawMove(await api("storage/migration", { method: "POST" })); }
      catch (e) { box.insertAdjacentHTML("beforeend", `<div class="card notice bad">${esc(BLOCKED[e.message] || e.message)}</div>`); }
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
  const del = so && sy.delete_from_camera ? (sy.delete_events_from_camera ? "all" : "normal") : "keep";
  const num = (id, v, unit, hint) => `<label for="${id}">${unit}</label><input id="${id}" type="number" min="0" ${id.endsWith("min_free_percent") ? 'max="90"' : ""} step="1" required value="${v}">${hint ? `<div class="muted">${hint}</div>` : ""}`;
  const retention = (p, title) => `<div class="card" id="${p}ret"><b>${title}</b>
    <div class="grid">
      <div>${num(p + "max_total_gb", re[p + "max_total_gb"], "Size cap (GB)", "0 = no cap")}</div>
      <div>${num(p + "min_free_percent", re[p + "min_free_percent"], "Minimum free space (%)", "Oldest clips go first when below")}</div>
      <div>${num(p + "normal_days", re[p + "normal_days"], "Normal clips (days)")}</div>
      <div>${num(p + "parking_days", re[p + "parking_days"], "Parking clips (days)")}</div>
      <div>${num(p + "event_days", re[p + "event_days"], "Event clips (days)")}</div>
      <div>${num(p + "photo_days", re[p + "photo_days"], "Photos (days)")}</div>
    </div><div class="muted">0 days = no age limit (kept until space is needed). Clips marked Keep are never deleted.</div></div>`;
  main.innerHTML = `<h2>Sync</h2>
    <div class="card">
      <label>What to download when the camera is in range</label>
      ${Object.entries(MODES).map(([k, [t, d]]) => `<label class="choice"><input type="radio" name="s-mode" value="${k}" ${sy.mode === k ? "checked" : ""}> <b>${t}</b> <span class="muted">· ${d}</span></label>`).join("")}
      <div class="grid"><div>${num("s-recent", sy.recent_minutes, "Recent driving to include (minutes)", "With Events first: the minutes before arrival")}</div>
      <div><label for="s-del">After a verified download</label><select id="s-del">
        <option value="keep" ${del === "keep" ? "selected" : ""}>Keep everything on the camera</option>
        <option value="normal" ${del === "normal" ? "selected" : ""}>Delete normal clips from the camera</option>
        <option value="all" ${del === "all" ? "selected" : ""}>Delete normal and event clips from the camera</option></select>
        <div class="muted">Locked clips stay on the card unless they are events and events are deleted too.</div></div></div>
      <details><summary>Advanced</summary>
        <label class="choice"><input type="checkbox" id="s-remux" ${sy.remux_to_mp4 ? "checked" : ""}> Remux <code>.ts</code> to <code>.mp4</code> (no re-encoding)</label>
        <label class="choice"><input type="checkbox" id="s-keep" ${so.keep_original ? "checked" : ""}> Keep the original <code>.ts</code> next to the <code>.mp4</code></label>
        <label class="choice"><input type="checkbox" id="s-thumbs" ${sy.thumbnails ? "checked" : ""}> Make thumbnails</label>
      </details>
    </div>
    <h2>Storage</h2>
    <div class="card">
      <label for="l-target">Keep clips on</label>
      <select id="l-target">${Object.entries({ local: "This Home Assistant", network: "A disk or share", both: "Both" }).map(([v, t]) => `<option value="${v}" ${v === so.target ? "selected" : ""}>${t}</option>`).join("")}</select>
      <div id="l-local-row"><label for="l-local">Local folder (under /media)</label><input id="l-local" value="${esc(so.local_folder)}"></div>
      <div id="l-net-row">
        <label for="l-net">Disk or share</label>
        ${netOptions ? `<select id="l-net">${netOptions}</select>` : `<div class="muted" id="l-net-none">None attached. Add a network share or a local disk (USB drive, SD card) under <a href="/config/storage" target="_top">Settings › System › Storage</a>, with usage Media or Share.</div>`}
        <label class="choice"><input type="checkbox" id="l-spool" ${so.spool_when_network_down ? "checked" : ""}> Keep new clips locally while it is offline, and move them over when it is back</label>
      </div>
      <div class="muted">A change applies to new clips. Existing clips can be moved afterwards.</div>
    </div>
    <div id="move"></div>
    <h2>Retention</h2>
    ${retention("", "This Home Assistant")}
    ${retention("network_", "Disk or share")}
    <div class="card"><label for="r-time">Daily purge at</label><input id="r-time" type="time" value="${esc(re.purge_time)}"></div>
    <p><button id="save" class="primary">Save settings</button> <span id="s-out" class="muted"></span></p>`;
  const sync = () => {
    const t = $("#l-target").value;
    $("#l-net-row").hidden = t === "local";
    $("#l-local-row").hidden = t === "network";
    $("#ret").hidden = t === "network";
    $("#network_ret").hidden = t === "local";
  };
  $("#l-target").onchange = sync; sync();
  $("#save").onclick = async () => {
    const bad = [...main.querySelectorAll("input")].find((el) => !el.closest("[hidden]") && !el.checkValidity());
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
    try {
      const r = await api("settings", { method: "PATCH", body });
      $("#s-out").textContent = "Saved.";
      if (r.migration) drawMove(r.migration);
    } catch (e) { $("#s-out").innerHTML = `<span class="bad-text">${esc(e.message)}</span>`; }
  };
  drawMove(await api("storage/migration"));
}

async function logs() {
  const draw = async () => { const r = await api("logs?lines=400"); main.innerHTML = `<h2>Logs</h2><pre>${esc(r.lines.join("\n"))}</pre>`; };
  await draw();
  timer = setInterval(() => draw().catch(() => {}), 5000);
}

render();
