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
    const shot = async (name) => { if (SCREENS) { fs.mkdirSync(SCREENS, { recursive: true }); await page.screenshot({ path: path.join(SCREENS, name + ".png") }); } };

    // ---- Overview: the fake camera syncs on its own.
    await page.goto(PANEL);
    assert.equal(await page.title(), "Dashcam Sync");
    await page.waitForSelector(".card b:text('Fake dashcam')");
    await waitFor(async () => /Last sync: (?!never)/.test(await page.locator("main").innerText()), "the first sync to finish");
    const overview = await page.locator("main").innerText();
    assert.match(overview, /connected/);
    assert.match(overview, /Status: idle/);
    assert.match(overview, /local/);
    await shot("overview");

    // ---- Clips: three real clips, thumbnails decode, a clip plays.
    await page.click("nav a[href='#clips']");
    await page.waitForSelector("img.thumb");
    assert.equal(await page.locator("main .card[data-id]").count(), 3);
    await waitFor(async () => (await page.$$eval("img.thumb", (imgs) => imgs.filter((i) => i.naturalWidth > 0).length)) === 3, "three decoded thumbnails");
    const kinds = await page.locator("main .card[data-id] b").allInnerTexts();
    assert.deepEqual(kinds.sort(), ["event", "normal", "normal"]);
    await page.click("main .card[data-id] button:text('Play') >> nth=0");
    const video = page.locator("video");
    await video.waitFor();
    await waitFor(() => video.evaluate((v) => v.readyState >= 2 && v.videoWidth === 640), "the video to load");
    await shot("clips");

    // Keep toggles and persists.
    const first = page.locator("main .card[data-id]").first();
    await first.locator("button:text('Keep')").click();
    await waitFor(async () => (await page.locator("main .card[data-id]").first().locator("button").allInnerTexts()).includes("Unkeep"), "Keep to stick");

    // ---- Storage: bars and a retention dry run.
    await page.click("nav a[href='#storage']");
    await page.waitForSelector(".bar");
    await page.click("#dry");
    await page.waitForSelector("#purge-out .card");
    assert.match(await page.locator("#purge-out").innerText(), /Would remove\s+0/);  // everything is fresh or kept
    await shot("storage");

    // ---- Changing the location, then moving the existing clips there.
    assert.equal(await page.locator("#l-net-row").isHidden(), true, "no share picker while local");
    await page.fill("#l-local", "dashcam_moved");
    await page.click("#l-save");
    await page.waitForSelector("#m-start");
    assert.match(await page.locator("#move").innerText(), /clips \(.+\) are not where the settings put them/);
    await shot("storage-move");
    await page.click("#m-start");
    await page.waitForSelector("#m-done", { timeout: 60000 });
    assert.match(await page.locator("#m-done").innerText(), /Last move: \d+ clips/);
    const paths = await page.evaluate(async () => (await (await fetch("api/v1/clips?limit=500")).json()).map((c) => c.path || ""));
    assert.ok(paths.length > 0 && paths.every((p) => p.includes("/dashcam_moved/")), "every clip moved: " + paths.join(", "));
    // Clips still play from the new place.
    const firstId = await page.evaluate(async () => (await (await fetch("api/v1/clips?limit=1")).json())[0].id);
    const res = await ctx.request.get(PANEL + `api/v1/clips/${firstId}/file`);
    assert.equal(res.status(), 200);

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
    await page.fill("#w-name", "Garage cam");
    assert.equal(await page.locator("#w-adv input[disabled]").inputValue(), "garage_cam", "the ID follows the name");
    await page.click("#w-next");
    await page.waitForSelector("#w-done");
    assert.match(await page.locator("#w-connect").innerText(), /Detected a novatek camera/);
    await shot("cameras-wizard");
    await page.click("#w-done");
    await waitFor(async () => /Garage cam/.test(await page.locator("main").innerText()), "the new camera to be listed");
    assert.match(await page.locator("main").innerText(), /Local network · 127\.0\.0\.1:\d+/);

    // A hotspot camera: no Home Assistant to ask here, so the exact profile is shown.
    await page.click("#add");
    await page.click("#w-next");  // Camera hotspot is the default
    await page.fill("#w-if", "wlan9");
    await page.fill("#w-ssid", "CARDV-1234");
    await page.fill("#w-pass", "secret-pass");
    await page.click("#w-next");
    await page.fill("#w-name", "Hotspot cam");
    await page.click("#w-adv summary");
    await page.fill("#w-adv #w-host", "127.0.0.1:9");  // a closed port: nothing answers there
    await page.click("#w-next");
    await page.waitForSelector("#w-connect table.kv", { timeout: 8000 }).catch(async (e) => {
      throw new Error(e.message + "\nwizard shows: " + (await page.locator("#wizard").innerText()));
    });
    const manualText = await page.locator("#w-connect").innerText();
    assert.match(manualText, /integration is not connected/);
    assert.match(manualText, /CARDV-1234/);
    assert.match(manualText, /Static · \d+\.\d+\.\d+\.\d+\/30/);
    assert.ok(!/secret-pass/.test(manualText), "the password is never shown");
    await shot("cameras-hotspot-manual");
    await page.click("#w-close");
    await waitFor(async () => /Camera hotspot · wlan9 · CARDV-1234/.test(await page.locator("main").innerText()), "the hotspot camera to be listed");

    // Editing keeps the stored password when it is left blank.
    const hot = page.locator("main .card[data-id=hotspot_cam]");
    await hot.locator("button:text('Edit')").click();
    await page.fill("#w-ssid", "CARDV-5678");
    await page.click("#w-next");
    await page.click("#w-next");
    await page.waitForSelector("#w-connect table.kv");
    await page.click("#w-close");
    const stored = await page.evaluate(async () => (await (await fetch("api/v1/cameras/hotspot_cam")).json()).options);
    assert.equal(stored.ssid, "CARDV-5678");
    assert.equal(stored.wifi_password, "********", "the password was kept, not cleared");
    await shot("cameras");

    // ---- Logs.
    await page.click("nav a[href='#logs']");
    await page.waitForSelector("h2:text('Logs')");
    assert.match(await page.locator("pre").innerText(), /Dashcam Sync .* started/);
    await shot("logs");

    // ---- The panel works under an ingress prefix: relative URLs only.
    const html = await (await ctx.request.get(PANEL)).text();
    assert.ok(!/(src|href)="\//.test(html), "no absolute asset URLs");
    const js = await (await ctx.request.get(PANEL + "static/app.js")).text();
    assert.ok(!/fetch\("\//.test(js), "no absolute fetch URLs");

    // ---- Keyboard reachability: tabbing from the top reaches the nav links.
    await page.goto(PANEL);
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement.tagName), "A");
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
