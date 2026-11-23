import puppeteer from "puppeteer-core";
import fs from "fs";
import path from "path";
import { spawn } from "child_process";

const E = process.env;
const EXT = E.E2E_EXT, DL = E.E2E_DL, INBOX = E.E2E_INBOX, SITE = E.E2E_SITE;
const PROFILE = E.E2E_PROFILE, SHOTS = E.E2E_SHOTS || ".";
let pydm = spawn(E.E2E_PYTHON, [E.E2E_SERVERS, "pydm", "9614", INBOX], { stdio: "ignore" });
process.on("exit", () => { try { pydm.kill(); } catch {} });
fs.rmSync(DL, { recursive: true, force: true }); fs.mkdirSync(DL, { recursive: true });
const results = [];
const check = (name, ok, info = "") => { results.push([name, !!ok]); console.log((ok ? "PASS " : "FAIL ") + name + " :: " + String(info).replace(/\n/g, " ")); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const inbox = () => fs.existsSync(INBOX)
  ? fs.readFileSync(INBOX, "utf8").trim().split("\n").filter(Boolean).map(JSON.parse) : [];
const waitInbox = async (pred, ms = 8000) => { const t = Date.now();
  while (Date.now() - t < ms) { const hit = inbox().find(pred); if (hit) return hit; await sleep(150); } return null; };

fs.rmSync(INBOX, { force: true });
await sleep(1500);
fs.rmSync(PROFILE, { recursive: true, force: true }); fs.mkdirSync(path.join(PROFILE, "Default"), { recursive: true });
fs.writeFileSync(path.join(PROFILE, "Default", "Preferences"), JSON.stringify({ download: { default_directory: DL, prompt_for_download: false, directory_upgrade: true }, safebrowsing: { enabled: false } }));
const browser = await puppeteer.launch({ userDataDir: PROFILE,
  executablePath: E.E2E_CHROME, headless: "new", pipe: true,
  enableExtensions: [EXT],
  protocolTimeout: 30000,
  args: ["--no-sandbox", "--autoplay-policy=no-user-gesture-required", "--disable-gpu", "--no-first-run",
         "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-default-apps"],
});

const swTarget = await browser.waitForTarget((t) => t.type() === "service_worker" && t.url().endsWith("background.js"), { timeout: 15000 });
const sw = await swTarget.worker(); 

const extId = new URL(swTarget.url()).host;
check("extension loads (MV3 service worker)", !!extId, extId);

const page = await browser.newPage();
await page.goto(SITE + "/page.html", { waitUntil: "load" });

await sleep(1500);
const tabId = await sw.evaluate(async () => (await chrome.tabs.query({ url: "http://127.0.0.1/*" }))[0].id);

// 1) dynamic attachment link (no extension in URL) -> caught via onDeterminingFilename, pairing first
await page.click("#dyn");
const pair = await waitInbox((x) => x.type === "pairing");
check("first request triggers pairing approval in PyDM", pair && pair.origin === `chrome-extension://${extId}`, pair && pair.origin);
const add = await waitInbox((x) => x.type === "add" && x.url.includes("/download?id=1"));
check("catches /download?id=1 using the real file name", add && add.filename === "report-2026.zip", add && add.filename);
check("passes cookies + referrer + size", add && /sess=abc123/.test(add.cookies || "") && add.referrer.endsWith("/page.html") && add.size === 600000,
  add && `${add.cookies} | ${add.referrer} | ${add.size}`);
await sleep(1500);
const items = await sw.evaluate(async () => (await chrome.downloads.search({})).map((d) => ({ u: d.url, s: d.state })));
check("browser's own download cancelled & erased", !items.some((d) => d.u.includes("download?id=1")) && !fs.readdirSync(DL).some((f) => f.startsWith("report")),
  JSON.stringify(items));

// 2) small file under the size threshold -> browser keeps it
await page.click("#small");
await sleep(2500);
check("small file (<256 KB) left to the browser", fs.readdirSync(DL).includes("small.zip") && !inbox().some((x) => x.url && x.url.endsWith("small.zip")), fs.readdirSync(DL).join(","));

// 3) never-catch site rule
await sw.evaluate(() => chrome.storage.local.set({ neverSites: "127.0.0.1" }));
await page.click("#iso"); await sleep(2500);
check("'never catch on this site' rule respected", fs.readdirSync(DL).includes("ubuntu.iso") && !inbox().some((x) => x.url && x.url.endsWith("ubuntu.iso")));
await sw.evaluate(() => chrome.storage.local.set({ neverSites: "" }));

// 4) detected media on the page (video element + HLS playlist via fetch)
const media = await sw.evaluate((id) => globalThis.__pydm.media.get(id) || [], tabId);
check("detects page video + HLS playlist", media.some((m) => m.url.endsWith("movie720.mp4")) && media.some((m) => m.type === "hls"),
  media.map((m) => `${m.type}:${m.url.split("/").pop()}`).join(", "));
const badgeText = await sw.evaluate((id) => chrome.action.getBadgeText({ tabId: id }), tabId);
check("toolbar badge shows video count", badgeText && parseInt(badgeText, 10) >= 1, badgeText);

// 5) on-page "Download video" button
await page.hover("#v"); await sleep(400);
const btn = await page.evaluate(() => { const h = document.querySelector("pydm-overlay");
  const b = h && h.shadowRoot.querySelector("button"); if (!b) return null; b.click(); return b.textContent; });
check("video button injected over the player", btn && btn.includes("Download video"), btn);
const med = await waitInbox((x) => x.type === "media");
check("video button sends page + streams to PyDM", med && med.page_url.endsWith("/page.html") && med.candidates.some((c) => c.url.endsWith("movie720.mp4")) && med.candidates.some((c) => c.url.endsWith(".m3u8")) && /sess=abc123/.test(med.cookies || ""),
  med && med.candidates.map((c) => c.url.split("/").pop()).join(","));
await sleep(500);
const label = await page.evaluate(() => document.querySelector("pydm-overlay").shadowRoot.querySelector("button span").textContent);
check("button confirms 'Sent to PyDM'", label === "Sent to PyDM", label);

// 6) popup: status, grab links, send selected
const pop = await browser.newPage();
await pop.goto(`chrome-extension://${extId}/popup.html?tab=${tabId}`);
await sleep(1200);
const statusText = await pop.$eval("#status span", (e) => e.textContent);
check("popup shows connection status", statusText === "Connected", statusText);
const mediaRows = await pop.$$eval("#mediaList .item", (r) => r.length);
check("popup lists detected videos", mediaRows >= 2, `${mediaRows} rows`);
await pop.click("#grabBtn"); await sleep(1000);
const linkRows = await pop.$$eval("#links .item", (r) => r.map((x) => x.textContent));
check("popup grabs page links (pages filtered out)", linkRows.length >= 6 && !linkRows.some((t) => t.includes("page.html")), linkRows.join(" | "));
await pop.select("#type", "Compressed"); await sleep(200);
const sendLabel = await pop.$eval("#sendLinks", (b) => b.textContent);
await pop.click("#sendLinks");
const batch = await waitInbox((x) => x.type === "batch");
check("filter by type + send batch", batch && batch.links.every((u) => /\.(rar|zip|iso)$|download\?id=1/.test(u)) && batch.links.length >= 2, `${sendLabel} -> ${batch && batch.links.map((u) => u.split("/").pop()).join(",")}`);

// 7) settings saved from popup
await pop.$eval("details", (d) => { d.open = true; });
await pop.$eval("#minSizeKB", (i) => { i.value = "1024"; });
await pop.click("#save"); await sleep(500);
const saved = await sw.evaluate(() => chrome.storage.local.get("minSizeKB"));
check("popup saves settings", saved.minSizeKB === 1024, JSON.stringify(saved));
await sw.evaluate(() => chrome.storage.local.set({ minSizeKB: 256 }));

// 8) context menu: "Download all links"
const tab = await sw.evaluate((id) => chrome.tabs.get(id), tabId);
const before = inbox().filter((x) => x.type === "batch").length;
await sw.evaluate((t) => globalThis.__pydm.onMenu({ menuItemId: "pydm-all", pageUrl: t.url }, t), tab);
await sleep(800);
check("context menu 'Download all links'", inbox().filter((x) => x.type === "batch").length === before + 1);
await sw.evaluate((t, site) => globalThis.__pydm.onMenu({ menuItemId: "pydm-link", linkUrl: site + "/files/part1.rar", pageUrl: t.url }, t), tab, SITE);
check("context menu 'Download with PyDM'", !!(await waitInbox((x) => x.type === "add" && x.url.endsWith("part1.rar"))));

// 9) pause catching for 5 minutes
await pop.click("#pause5"); await sleep(400);
await page.bringToFront(); await page.click("#dyn"); await sleep(2500);
const caughtWhilePaused = inbox().filter((x) => x.type === "add" && x.url.includes("download?id=1")).length;
check("'let the browser handle it for 5 min' works", caughtWhilePaused === 1 && fs.readdirSync(DL).some((f) => f.startsWith("report")), fs.readdirSync(DL).join(","));
await sw.evaluate(() => chrome.storage.local.set({ pausedUntil: 0 }));

// 9b) a malicious web page can't talk to PyDM directly
const n0 = inbox().length;
const pageAttempt = await page.evaluate(async () => {
  try { const r = await fetch("http://127.0.0.1:9614/add", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: "http://evil.example/x.exe" }) }); return "status " + r.status; }
  catch (e) { return "blocked: " + e.message; } });
await sleep(800);
check("web pages can't send downloads to PyDM", inbox().length === n0 && !inbox().some((x) => (x.url || "").includes("evil")), pageAttempt);
// screenshots for the README
await pop.setViewport({ width: 380, height: 640 }); await pop.reload(); await sleep(1200);
await pop.screenshot({ path: path.join(SHOTS, "ext_popup.png") });
await page.setViewport({ width: 900, height: 560 }); await page.bringToFront(); await page.hover("#v"); await sleep(500);
await page.screenshot({ path: path.join(SHOTS, "ext_button.png") });

// 10) PyDM not running -> browser downloads normally
pydm.kill();
await sleep(500);
for (const f of fs.readdirSync(DL)) fs.rmSync(`${DL}/${f}`);
await page.bringToFront(); await page.click("#dyn"); await sleep(3000);
check("PyDM offline -> browser keeps the download", fs.readdirSync(DL).some((f) => f.startsWith("report")), fs.readdirSync(DL).join(","));
const offlineLabel = await (async () => { await page.hover("#v"); await page.evaluate(() => document.querySelector("pydm-overlay").shadowRoot.querySelector("button").click()); await sleep(800);
  return page.evaluate(() => document.querySelector("pydm-overlay").shadowRoot.querySelector("button span").textContent); })();
check("video button reports PyDM offline", offlineLabel === "PyDM isn't running", offlineLabel);

await browser.close();
const passed = results.filter((r) => r[1]).length;
console.log(`\nSUMMARY: ${passed} / ${results.length} passed`);
process.exit(0);
