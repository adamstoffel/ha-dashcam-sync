"use strict";
/*
 * Browser tests for the ingress panel (Playwright + Chromium).
 *
 *   node e2e/ui/panel.cjs [--screens docs/images]
 *
 * Starts `scripts/dev.py --demo` (fake camera + real daemon + generated video),
 * drives every page, asserts real behaviour (clips listed, thumbnails decode,
 * video plays, Keep toggles, retention dry run, wizard probe) and fails on any
 * console error or failed request. With --screens it also writes the PNGs used
 * in the README.
 */
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");
const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..", "..");
const PY = process.env.PYTHON || path.join(ROOT, ".venv-app", "bin", "python");
const screensIdx = process.argv.indexOf("--screens");
const SCREENS = screensIdx > 0 ? path.resolve(process.argv[screensIdx + 1]) : null;
const PANEL = "http://127.0.0.1:8099/";

function startDaemon() {
  const child = spawn(PY, [path.join(ROOT, "scripts", "dev.py"), "--demo", "--fresh"], { cwd: ROOT, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, PYTHONUNBUFFERED: "1" } });
  let log = "";
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("daemon did not start:\n" + log)), 60000);
    const onData = (buf) => {
      log += buf.toString();
      if (log.includes("Ctrl+C to stop")) { clearTimeout(timer); resolve(child); }
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("exit", (code) => reject(new Error(`daemon exited (${code}):\n${log}`)));
  });
}

async function waitFor(fn, what, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const v = await fn();
    if (v) return v;
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("timed out waiting for " + what);
}

(async () => {
  const daemon = await startDaemon();
  const browser = await chromium.launch();
  const failures = [];
  try {
    const ctx = await browser.newContext({ viewport: { width: 1100, height: 760 }, deviceScaleFactor: SCREENS ? 2 : 1 });
    const page = await ctx.newPage();
    page.on("console", (m) => { if (m.type() === "error") failures.push("console error: " + m.text()); });
    page.on("pageerror", (e) => failures.push("page error: " + e.message));
    page.on("requestfailed", (r) => failures.push("request failed: " + r.url() + " " + (r.failure() || {}).errorText));
    page.on("response", (r) => { if (r.status() >= 400 && !r.url().endsWith("/thumb")) failures.push(`HTTP ${r.status()} ${r.url()}`); });
    // Documentation screenshots show what a real install shows, not the test harness:
    // the fake camera's loopback address, the temporary media folder, a spare interface.
    const realistic = () => page.evaluate(() => {
      const swaps = [[/127\.0\.0\.1:\d+/g, "192.168.1.254"], [/\/tmp\/dashcam-demo\/media/g, "/media"], [/\bwlan9\b/g, "wlan0"]];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        let t = n.nodeValue; for (const [re, to] of swaps) t = t.replace(re, to);
        if (t !== n.nodeValue) n.nodeValue = t;
      }
      for (const el of document.querySelectorAll("input")) { for (const [re, to] of swaps) el.value = el.value.replace(re, to); }
      const toast = document.getElementById("toast"); if (toast) toast.hidden = true;
    });
    const shot = async (name) => { if (SCREENS) { fs.mkdirSync(SCREENS, { recursive: true }); await realistic(); await page.screenshot({ path: path.join(SCREENS, name + ".png") }); } };

    // ---- Overview: the fake camera syncs on its own.
    await page.goto(PANEL);
    assert.equal(await page.title(), "Dashcam Sync");
    await page.waitForSelector(".cam-card .name:text('Family car')");
    await waitFor(async () => /last sync (?!never)/.test(await page.locator("main").innerText()), "the first sync to finish");
    const overview = await page.locator("main").innerText();
    assert.match(overview, /connected/);
    assert.match(overview, /idle/);
    assert.match(overview, /This Home Assistant/);
    await shot("overview");

    // ---- Clips: three real clips, thumbnails decode, a clip plays.
    await page.click("nav a[href='#clips']");
    await page.waitForSelector("img.thumb");
    assert.equal(await page.locator("main .card[data-id]").count(), 3);
    await waitFor(async () => (await page.$$eval("img.thumb", (imgs) => imgs.filter((i) => i.naturalWidth > 0).length)) === 3, "three decoded thumbnails");
    const kinds = await page.locator("main .card[data-id] .meta .badge").allInnerTexts();
    assert.deepEqual(kinds.sort(), ["event", "normal", "normal"]);
    await page.click("main .card[data-id] button:text('Play') >> nth=0");
    const video = page.locator("dialog#player video");
    await video.waitFor();
    await waitFor(() => video.evaluate((v) => v.readyState >= 2 && v.videoWidth > 0), "the video to load");
    await page.click("dialog#player .dlg-close");
    await shot("clips");

    // Keep toggles and persists.
    const first = page.locator("main .card[data-id]").first();
    await first.locator("button:text('Keep')").click();
    await waitFor(async () => (await page.locator("main .card[data-id]").first().locator("button").allInnerTexts()).includes("Unkeep"), "Keep to stick");

    // Deleting asks first, in the page (no browser confirm()); cancelling keeps the clip.
    await page.locator("main .card[data-id]").nth(2).locator("button:text('Delete')").click();
    await page.waitForSelector("dialog#confirm");
    await page.click("#c-no");
    assert.equal(await page.locator("main .card[data-id]").count(), 3, "cancel keeps the clip");

    // ---- Storage: bars and a retention dry run.
    await page.click("nav a[href='#storage']");
    await page.waitForSelector(".bar");
    await page.click("#dry");
    await page.waitForSelector("#purge-out div");
    assert.match(await page.locator("#purge-out").innerText(), /Would remove\s+0/);  // everything is fresh or kept
    await shot("storage");

    // ---- Settings: sync and retention persist; bad values are explained.
    await page.click("nav a[href='#settings']");
    await page.waitForSelector("#l-target");
    assert.equal(await page.locator("#savebar").isHidden(), true, "no save bar until something changes");
    assert.equal(await page.locator("#l-net-row").isHidden(), true, "no disk picker while local");
    assert.equal(await page.locator("#network_ret").isHidden(), true, "no disk retention while local");
    await page.check("input[name=s-mode][value=events_only]");
    // Leaving with unsaved changes asks first; declining keeps the page and the edits.
    page.once("dialog", (d) => d.dismiss());
    await page.click("nav a[href='#logs']");
    await page.waitForTimeout(300);
    assert.equal(await page.evaluate(() => location.hash), "#settings", "stayed on unsaved settings");
    assert.equal(await page.locator("input[name=s-mode][value=events_only]").isChecked(), true);
    await page.fill("#normal_days", "3");
    await page.selectOption("#s-del", "normal");
    await page.click("#save");
    await waitFor(async () => (await page.locator("#s-out").innerText()) === "Saved.", "settings to save");
    const saved = await page.evaluate(async () => (await fetch("api/v1/settings")).json());
    assert.equal(saved.sync.mode, "events_only");
    assert.equal(saved.retention.normal_days, 3);
    assert.equal(saved.sync.delete_from_camera, true);
    assert.equal(saved.sync.delete_events_from_camera, false);
    await page.fill("#min_free_percent", "95");
    await page.click("#save");
    await waitFor(async () => /Minimum free space \(%\):/.test(await page.locator("#s-out").innerText()), "a readable validation error");
    await page.fill("#min_free_percent", "10");
    await page.check("input[name=s-mode][value=everything]");
    await page.selectOption("#s-del", "keep");
    await shot("settings");

    // ---- Changing the location, then moving the existing clips there.
    await page.fill("#l-local", "dashcam_moved");
    await page.click("#save");
    await page.waitForSelector("#m-start");
    assert.match(await page.locator("#move").innerText(), /clips \(.+\) are not where the settings put them/);
    await shot("storage-move");
    await page.click("#m-start");
    await page.waitForSelector("#m-done", { timeout: 60000 });
    assert.match(await page.locator("#m-done").innerText(), /Last move: \d+ clips/);
    const paths = await page.evaluate(async () => (await (await fetch("api/v1/clips?limit=500")).json()).map((c) => c.path || ""));
    assert.ok(paths.length > 0 && paths.every((p) => p.includes("/dashcam_moved/")), "every clip moved: " + paths.join(", "));
    const firstId = await page.evaluate(async () => (await (await fetch("api/v1/clips?limit=1")).json())[0].id);
    const res = await ctx.request.get(PANEL + `api/v1/clips/${firstId}/file`);
    assert.equal(res.status(), 200);
    await page.click("nav a[href='#storage']");
    await page.waitForSelector(".bar");
    assert.match(await page.locator("main").innerText(), /local folder \/media\/dashcam_moved/);

    // ---- Cameras + the setup wizard (local network camera, detected for real).
    await page.click("nav a[href='#cameras']");
    await page.waitForSelector("button:text('Add camera')");
    assert.equal(await page.locator("button:text('Network check')").count(), 0, "LAN cameras have no address block to check");
    const camPort = await page.evaluate(async () => { const r = await (await fetch("api/v1/cameras")).json(); return r[0].options.host.split(":")[1]; });
    await page.click("#add");
    await page.check("input[name=w-conn][value=lan]");
    await page.click("#w-next");
    await page.fill("#w-host", "127.0.0.1:" + camPort);
    await page.click("#w-probe");
    await waitFor(async () => /Detected a novatek camera/.test(await page.locator("#w-det").innerText().catch(() => "")), "the wizard to detect the driver");
    await page.click("#w-next");
    await page.fill("#w-name", "Second car");
    assert.equal(await page.locator("#w-adv input[disabled]").inputValue(), "second_car", "the ID follows the name");
    await page.click("#w-next");
    await page.waitForSelector("#w-done");
    assert.match(await page.locator("#w-connect").innerText(), /Detected a novatek camera/);
    await shot("cameras-wizard");
    await page.click("#w-done");
    await waitFor(async () => /Second car/.test(await page.locator("main").innerText()), "the new camera to be listed");
    assert.match(await page.locator("main").innerText(), /Local network · 127\.0\.0\.1:\d+/);

    // A hotspot camera: no Home Assistant to ask here, so the exact profile is shown.
    await page.click("#add");
    await page.click("#w-next");  // Camera hotspot is the default
    await page.fill("#w-if", "wlan9");
    // Scanning goes through Home Assistant; with no integration it says so calmly.
    await page.click("#w-scan");
    // An interim "Asking Home Assistant…" note comes first: wait for the answer itself.
    await waitFor(async () => /integration is not connected/.test(await page.locator("#w-err").innerText()), "the scan answer");
    assert.equal(await page.locator("#w-err .notice.bad").count(), 0, "not shown as an error");
    await page.fill("#w-ssid", "CARDV-1234");
    await page.fill("#w-pass", "secret-pass");
    assert.equal(await page.locator("#w-camip").inputValue(), "192.168.1.254", "the usual camera IP is preselected");
    await page.selectOption("#w-camip", "other");
    await page.fill("#w-camip-other", "127.0.0.1:9");  // a closed port: nothing answers there
    await page.click("#w-next");
    await page.fill("#w-name", "Hotspot cam");
    await page.click("#w-next");
    // Joining goes through the integration, so with none connected the wizard stops and
    // says so, instead of asking Home Assistant and waiting for an answer that never comes.
    await page.waitForSelector("#w-recheck", { timeout: 8000 }).catch(async (e) => {
      throw new Error(e.message + "\nwizard shows: " + (await page.locator("#wizard").innerText()));
    });
    assert.match(await page.locator("#w-connect .notice.bad").innerText(), /integration is not connected/);
    assert.equal(await page.locator("#w-anyway").count(), 0, "nothing to continue to without an integration");
    await page.click("#w-connect details summary");  // the by-hand steps stay one click away
    await page.waitForSelector("#w-connect table.kv", { state: "visible" });
    const manualText = await page.locator("#w-connect").innerText();
    assert.match(manualText, /CARDV-1234/);
    assert.match(manualText, /Static · \d+\.\d+\.\d+\.\d+\/30/);
    assert.ok(!/secret-pass/.test(manualText), "the password is never shown");
    await shot("cameras-hotspot-manual");
    await page.click("dialog#wizard .dlg-close");
    await waitFor(async () => /Camera hotspot · CARDV-1234 on wlan9/.test(await page.locator("main").innerText()), "the hotspot camera to be listed");

    // Actions live in a ⋮ menu; Edit keeps the stored password when it is left blank.
    const hot = page.locator("main .card[data-id=hotspot_cam]");
    await hot.locator(".menu-btn").click();
    await page.keyboard.press("ArrowDown");
    assert.equal(await page.evaluate(() => document.activeElement.textContent), "Network check", "arrow keys move through the menu");
    const menuItems = await hot.locator(".menu button").allInnerTexts();
    assert.deepEqual(menuItems, ["Edit", "Network check", "Restore recording", "Pause", "Capture device profile", "Remove"]);
    await hot.locator(".menu button[data-act=edit]").click();
    assert.equal(await page.locator("#w-camip").inputValue(), "other");
    assert.equal(await page.locator("#w-camip-other").inputValue(), "127.0.0.1:9");
    await page.fill("#w-ssid", "CARDV-5678");
    await page.click("#w-next");
    await page.click("#w-next");
    await page.waitForSelector("#w-recheck");  // saved; the join step is reached
    await page.click("dialog#wizard .dlg-close");
    const stored = await page.evaluate(async () => (await (await fetch("api/v1/cameras/hotspot_cam")).json()).options);
    assert.equal(stored.ssid, "CARDV-5678");
    assert.equal(stored.wifi_password, "********", "the password was kept, not cleared");
    await shot("cameras");

    // A camera's detail view: status and its recent syncs.
    await page.locator("main .card[data-id=family_car]").click({ position: { x: 20, y: 20 } }).catch(async () => {
      await page.locator("main .cam-card").first().click({ position: { x: 20, y: 20 } });
    });
    await page.waitForSelector("h2:text('Recent syncs')");
    assert.match(await page.locator("main").innerText(), /ok/);
    await page.click("a:text('‹ Cameras')");
    await page.waitForSelector("#add");

    // Removing a camera asks first.
    await hot.locator(".menu-btn").click();
    await hot.locator(".menu button[data-act=delete]").click();
    await page.click("#c-yes");
    await waitFor(async () => (await page.locator("main .card[data-id=hotspot_cam]").count()) === 0, "the camera to be removed");

    // ---- Logs.
    await page.click("nav a[href='#logs']");
    await page.waitForSelector("h2:text('Logs')");
    assert.match(await page.locator("pre").innerText(), /Dashcam Sync .* started/);
    await shot("logs");

    // ---- Phone width: nothing is wider than the screen, the tabs stay on one line.
    const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 });
    const pp = await phone.newPage();
    pp.on("pageerror", (e) => failures.push("phone page error: " + e.message));
    for (const v of ["overview", "cameras", "clips", "storage", "settings", "logs"]) {
      await pp.goto(PANEL + "#" + v);
      await pp.waitForSelector("main > :not(.empty)");
      await pp.waitForTimeout(400);
      const over = await pp.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      assert.ok(over <= 0, `${v} is ${over}px wider than a phone screen`);
      const navHeight = await pp.evaluate(() => document.querySelector("nav").getBoundingClientRect().height);
      assert.ok(navHeight < 60, `tabs wrap on ${v} (${navHeight}px)`);
    }
    await pp.goto(PANEL + "#cameras");
    await pp.click("#add");
    await pp.waitForSelector("dialog#wizard");
    const sheet = await pp.evaluate(() => { const r = document.querySelector("dialog#wizard").getBoundingClientRect(); return [r.width, r.height]; });
    assert.ok(sheet[0] >= 389 && sheet[1] >= 840, "the wizard is full screen on a phone: " + sheet);
    await phone.close();

    // ---- The panel works under an ingress prefix: relative URLs only.
    const html = await (await ctx.request.get(PANEL)).text();
    assert.ok(!/(src|href)="\//.test(html), "no absolute asset URLs");
    const js = await (await ctx.request.get(PANEL + "static/app.js")).text();
    assert.ok(!/fetch\("\//.test(js), "no absolute fetch URLs");

    // ---- Keyboard reachability: tabbing from the top reaches the nav links.
    await page.goto(PANEL);
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement.textContent), "Skip to content", "the first stop is the skip link");
  } catch (err) {
    failures.push(err.stack || String(err));
  } finally {
    await browser.close();
    daemon.removeAllListeners("exit");
    daemon.kill("SIGINT");
  }
  if (failures.length) {
    console.error("UI tests FAILED:\n- " + failures.join("\n- "));
    process.exit(1);
  }
  console.log("UI tests passed");
})();
