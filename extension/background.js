// PyDM browser integration — background service worker (Chrome/Edge/Brave) or event page (Firefox).
// Talks only to the PyDM app on 127.0.0.1. PyDM asks you to approve this extension once.
const B = globalThis.browser ?? globalThis.chrome;
const DEFAULTS = {
  catchEnabled: true,
  minSizeKB: 256,
  exts: "zip rar 7z tar gz bz2 xz tgz zst iso cab dmg pkg exe msi deb rpm apk appimage bin jar " +
        "mp4 mkv avi mov wmv flv webm m4v mpg mpeg 3gp mp3 wav flac aac ogg m4a wma opus " +
        "pdf doc docx xls xlsx ppt pptx epub",
  neverSites: "",
  alwaysSites: "",
  videoButton: true,
  port: 9614,
  pausedUntil: 0,
};
const CATCH_MIME = /^(application\/(zip|x-zip|x-rar|x-7z|gzip|x-gzip|x-tar|x-xz|x-msdownload|x-msi|x-iso9660|x-apple-diskimage|vnd\.android\.package-archive|vnd\.debian|x-rpm|java-archive|pdf|epub|msword|vnd\.ms-|vnd\.openxmlformats)|video\/|audio\/)/i;
const STREAM_MIME = /^(application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml)|audio\/mpegurl)/i;
const MEDIA_EXT = /\.(m3u8|mpd|mp4|webm|mkv|mov|m4v|flv|mp3|m4a|aac|ogg|opus|flac|wav)(\?|#|$)/i;
const SEGMENT = /\.(ts|m4s|m4f|aac|cmfv|cmfa)(\?|#|$)|[?&](range|bytes)=\d|\/(seg|segment|chunk|frag)[-_]?\d+/i;
const media = new Map();          // tabId -> [{url, type, size, ts}]

// ------------------------------------------------------------------ settings
async function settings() {
  const s = await B.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...s };
}
const api = async () => `http://127.0.0.1:${(await settings()).port}`;
const hostOf = (u) => { try { return new URL(u).hostname.toLowerCase(); } catch { return ""; } };
const extOf = (name) => { const m = /\.([a-z0-9]{1,8})$/i.exec(name || ""); return m ? m[1].toLowerCase() : ""; };
const pathName = (u) => { try { return decodeURIComponent(new URL(u).pathname.split("/").pop()); } catch { return ""; } };
function siteMatch(host, list) {
  return list.split(/[\s,]+/).filter(Boolean).some((p) => {
    p = p.toLowerCase().replace(/^\*\./, "");
    return host === p || host.endsWith("." + p);
  });
}

// ------------------------------------------------------------------ talking to PyDM
function badge(text, color, tabId) {
  const opts = tabId ? { text, tabId } : { text };
  B.action.setBadgeText(opts);
  B.action.setBadgeBackgroundColor(tabId ? { color, tabId } : { color });
}
function flash(text, color) {
  badge(text, color);
  setTimeout(() => B.action.setBadgeText({ text: "" }), 4000);
}
async function cookieHeader(url) {
  try {
    const cs = await B.cookies.getAll({ url });
    return cs.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch { return ""; }
}
async function post(path, body) {
  try {
    const res = await fetch((await api()) + path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (res.ok) { flash("✓", "#7c6cff"); return { ok: true }; }
    flash(res.status === 403 ? "✕" : "!", "#f43f5e");
    return { ok: false, status: res.status };
  } catch {
    flash("off", "#596178");
    return { ok: false, offline: true };
  }
}
async function ping() {
  try {
    const r = await fetch((await api()) + "/ping", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}
async function sendDownload(url, referrer, filename, size) {
  if (!/^https?:\/\//i.test(url)) return { ok: false };
  return post("/add", { url, referrer, filename, size, cookies: await cookieHeader(url) });
}
async function sendMedia(tabId, pageUrl, title, domCandidates = []) {
  const seen = new Set();
  const cands = [...domCandidates, ...(media.get(tabId) || [])].filter((c) => {
    if (!/^https?:/i.test(c.url) || seen.has(c.url)) return false;
    seen.add(c.url); return true;
  });
  return post("/media", { page_url: pageUrl, title, candidates: cands, referrer: pageUrl,
                          cookies: await cookieHeader(pageUrl) });
}
async function collectLinks(tabId) {
  const [res] = await B.scripting.executeScript({ target: { tabId }, func: () => {
    const out = [], seen = new Set();
    const add = (u, text, tag) => {
      try { u = new URL(u, document.baseURI).href.split("#")[0]; } catch { return; }
      if (!/^(https?|ftp):/i.test(u) || seen.has(u)) return;
      seen.add(u); out.push({ url: u, text: (text || "").trim().slice(0, 150), tag });
    };
    document.querySelectorAll("a[href]").forEach((a) => add(a.getAttribute("href"), a.textContent, "a"));
    document.querySelectorAll("img[src]").forEach((i) => add(i.getAttribute("src"), i.alt, "img"));
    document.querySelectorAll("video[src],audio[src],source[src]").forEach((v) => add(v.getAttribute("src"), "", "media"));
    return out;
  } });
  return (res && res.result) || [];
}

// ------------------------------------------------------------------ catching downloads
async function shouldCatch(item, s) {
  if (!s.catchEnabled || Date.now() < s.pausedUntil || item.incognito) return false;
  const url = item.finalUrl || item.url;
  if (!/^https?:\/\//i.test(url)) return false;               // blob:, data:, file: stay in the browser
  const site = hostOf(item.referrer) || hostOf(url);
  if (siteMatch(site, s.neverSites) || siteMatch(hostOf(url), s.neverSites)) return false;
  const always = siteMatch(site, s.alwaysSites) || siteMatch(hostOf(url), s.alwaysSites);
  const size = item.fileSize > 0 ? item.fileSize : item.totalBytes;
  if (size > 0 && size < s.minSizeKB * 1024 && !always) return false;
  if (always) return true;
  const name = (item.filename || "").split(/[\\/]/).pop() || pathName(url);
  const exts = new Set(s.exts.toLowerCase().split(/[\s,]+/).filter(Boolean));
  if (exts.has(extOf(name)) || exts.has(extOf(pathName(url)))) return true;
  if (CATCH_MIME.test(item.mime || "")) return true;
  return (item.mime || "").startsWith("application/octet-stream") && size >= s.minSizeKB * 1024;
}
async function handOver(item) {
  const name = (item.filename || "").split(/[\\/]/).pop() || undefined;
  const size = item.fileSize > 0 ? item.fileSize : (item.totalBytes > 0 ? item.totalBytes : undefined);
  const r = await sendDownload(item.finalUrl || item.url, item.referrer, name, size);
  if (r.ok) {
    try { await B.downloads.cancel(item.id); await B.downloads.erase({ id: item.id }); } catch { /* finished */ }
  }
  return r.ok;
}
const handled = new Set();
if (B.downloads.onDeterminingFilename) {
  // Chromium: we know the real file name, type and size here — even for /download?id=… links.
  B.downloads.onDeterminingFilename.addListener((item, suggest) => {
    (async () => {
      handled.add(item.id);
      try {
        if (await shouldCatch(item, await settings())) await handOver(item);
      } finally {
        try { suggest(); } catch { /* already resolved */ }
      }
    })();
    return true;                                   // we answer asynchronously
  });
}
B.downloads.onCreated.addListener(async (item) => {
  if (B.downloads.onDeterminingFilename) return;   // Firefox fallback only
  if (await shouldCatch(item, await settings())) await handOver(item);
});

// ------------------------------------------------------------------ detecting videos
function remember(tabId, entry) {
  if (tabId < 0) return;
  const list = media.get(tabId) || [];
  if (list.some((x) => x.url === entry.url)) return;
  list.unshift(entry);
  media.set(tabId, list.slice(0, 30));
  const n = media.get(tabId).filter((x) => x.type !== "audio").length;
  B.action.setBadgeText({ text: n ? String(n) : "", tabId });
  B.action.setBadgeBackgroundColor({ color: "#43c6f0", tabId });
}
B.webRequest.onHeadersReceived.addListener((d) => {
  if (d.tabId < 0 || d.statusCode >= 400 || !/^https?:/i.test(d.url)) return;
  const h = Object.fromEntries((d.responseHeaders || []).map((x) => [x.name.toLowerCase(), x.value || ""]));
  const ctype = (h["content-type"] || "").split(";")[0].trim().toLowerCase();
  let size = parseInt(h["content-length"] || "0", 10) || 0;
  const cr = /\/(\d+)$/.exec(h["content-range"] || "");
  if (cr) size = parseInt(cr[1], 10);
  const isStream = STREAM_MIME.test(ctype) || /\.(m3u8|mpd)(\?|$)/i.test(d.url);
  if (isStream) {
    remember(d.tabId, { url: d.url, type: /mpd/i.test(d.url) || /dash/.test(ctype) ? "dash" : "hls", size: 0, ts: Date.now() });
    return;
  }
  if (SEGMENT.test(d.url)) return;                 // pieces of a stream, not a whole file
  const isMedia = /^(video|audio)\//.test(ctype) || (MEDIA_EXT.test(d.url) && ctype !== "text/html");
  if (isMedia && (size === 0 || size >= 512 * 1024)) {
    remember(d.tabId, { url: d.url, type: ctype.startsWith("audio") ? "audio" : "video", size, ts: Date.now() });
  }
}, { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other", "object"] }, ["responseHeaders"]);
B.tabs.onUpdated.addListener((tabId, info) => {
  if (info.url) { media.delete(tabId); B.action.setBadgeText({ text: "", tabId }); }
});
B.tabs.onRemoved.addListener((tabId) => media.delete(tabId));

// ------------------------------------------------------------------ menus & messages
function createMenus() {
  B.contextMenus.removeAll(() => {
    B.contextMenus.create({ id: "pydm-link", title: "Download with PyDM", contexts: ["link"] });
    B.contextMenus.create({ id: "pydm-media", title: "Download media with PyDM", contexts: ["video", "audio", "image"] });
    B.contextMenus.create({ id: "pydm-video", title: "Download video from this page with PyDM", contexts: ["page", "video"] });
    B.contextMenus.create({ id: "pydm-all", title: "Download all links with PyDM…", contexts: ["page"] });
  });
}
B.runtime.onInstalled.addListener(createMenus);
B.runtime.onStartup?.addListener(createMenus);

async function onMenu(info, tab) {
  if (info.menuItemId === "pydm-link") return sendDownload(info.linkUrl, info.pageUrl);
  if (info.menuItemId === "pydm-media") {
    if (/^https?:/i.test(info.srcUrl || "")) return sendDownload(info.srcUrl, info.pageUrl);
    return sendMedia(tab.id, info.pageUrl, tab.title);
  }
  if (info.menuItemId === "pydm-video") return sendMedia(tab.id, info.pageUrl, tab.title);
  if (info.menuItemId === "pydm-all") {
    const links = await collectLinks(tab.id);
    return post("/batch", { links, referrer: info.pageUrl });
  }
}
B.contextMenus.onClicked.addListener((info, tab) => { onMenu(info, tab); });

B.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    // Messages from web pages (content script) are untrusted: they may only ask to download the
    // video they show. Extension pages (popup, even when opened in a tab) get the full API.
    const fromExtensionPage = sender.id === B.runtime.id &&
      (sender.url || "").startsWith(B.runtime.getURL(""));
    if (!fromExtensionPage) {
      if (!sender.tab) { reply(null); return; }
      if (msg?.type === "video-button") {
        const cands = (msg.candidates || []).filter((c) => typeof c?.url === "string").slice(0, 10);
        reply(await sendMedia(sender.tab.id, sender.tab.url, sender.tab.title, cands));
      } else if (msg?.type === "settings") {
        const s = await settings();
        reply({ videoButton: s.videoButton && !siteMatch(hostOf(sender.tab.url), s.neverSites) });
      } else reply(null);
      return;
    }
    // popup (extension page)
    if (msg.type === "status") reply({ pong: await ping(), settings: await settings() });
    else if (msg.type === "media") reply(media.get(msg.tabId) || []);
    else if (msg.type === "download") reply(await sendDownload(msg.url, msg.referrer));
    else if (msg.type === "video") reply(await sendMedia(msg.tabId, msg.pageUrl, msg.title));
    else if (msg.type === "links") reply(await collectLinks(msg.tabId));
    else if (msg.type === "batch") reply(await post("/batch", { links: msg.links, referrer: msg.referrer }));
    else if (msg.type === "save") { await B.storage.local.set(msg.settings); reply(await settings()); }
    else reply(null);
  })();
  return true;
});
globalThis.__pydm = { shouldCatch, settings, media, onMenu, collectLinks, sendMedia };
