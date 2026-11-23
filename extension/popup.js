const B = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);
const TYPES = {
  Compressed: "zip rar 7z tar gz bz2 xz tgz zst iso cab", Video: "mp4 mkv avi mov wmv flv webm m4v mpg mpeg 3gp",
  Music: "mp3 wav flac aac ogg m4a wma opus", Documents: "pdf doc docx xls xlsx ppt pptx txt rtf odt ods epub csv",
  Programs: "exe msi dmg pkg deb rpm apk appimage bin jar", Images: "jpg jpeg png gif webp bmp svg avif",
};
const typeOf = (u) => {
  let name = ""; try { name = new URL(u).pathname.split("/").pop().toLowerCase(); } catch { /* bad url */ }
  const ext = (/\.([a-z0-9]+)$/.exec(name) || [])[1] || "";
  for (const [t, list] of Object.entries(TYPES)) if (list.split(" ").includes(ext)) return t;
  return ["", "html", "htm", "php", "asp", "aspx", "jsp"].includes(ext) ? "Page" : "Other";
};
const send = (msg) => new Promise((res) => B.runtime.sendMessage(msg, res));
const human = (n) => { if (!n) return ""; const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < 3) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + " " + u[i]; };
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text; return e; }

let tab = null, links = [];

async function currentTab() {
  const q = new URLSearchParams(location.search).get("tab");
  if (q) return B.tabs.get(parseInt(q, 10));
  const [t] = await B.tabs.query({ active: true, currentWindow: true });
  return t;
}

async function status() {
  const st = $("status"), r = await send({ type: "status" });
  st.className = "status " + (r.pong ? (r.pong.paired ? "ok" : "warn") : "bad");
  st.querySelector("span").textContent = r.pong ? (r.pong.paired ? "Connected" : "Approve in PyDM")
    : "PyDM isn't running";
  for (const k of ["catchEnabled", "videoButton"]) $(k).checked = !!r.settings[k];
  for (const k of ["minSizeKB", "exts", "neverSites", "alwaysSites", "port"]) $(k).value = r.settings[k];
  $("pause5").textContent = Date.now() < r.settings.pausedUntil ? "Catching paused — click to resume"
    : "Let the browser handle downloads for 5 minutes";
  $("pause5").dataset.paused = Date.now() < r.settings.pausedUntil ? "1" : "";
}

async function renderMedia() {
  const list = $("mediaList"); list.replaceChildren();
  const items = tab ? await send({ type: "media", tabId: tab.id }) : [];
  if (!items.length) { list.append(el("p", "empty", "No video detected yet — play it, or use the button below.")); return; }
  for (const m of items) {
    const row = el("div", "item");
    row.append(el("span", "tag", m.type));
    let name = m.url; try { name = decodeURIComponent(new URL(m.url).pathname.split("/").pop()) || m.url; } catch { /* keep */ }
    const n = el("span", "name", name); n.title = m.url; row.append(n);
    if (m.size) row.append(el("span", "meta", human(m.size)));
    const b = el("button", "", "Get");
    b.onclick = async () => {
      const r = m.type === "hls" || m.type === "dash"
        ? await send({ type: "video", tabId: tab.id, pageUrl: tab.url, title: tab.title })
        : await send({ type: "download", url: m.url, referrer: tab.url });
      b.textContent = r && r.ok ? "Sent" : "Failed";
    };
    row.append(b); list.append(row);
  }
}

function renderLinks() {
  const box = $("links"); box.replaceChildren();
  const f = $("filter").value.trim().toLowerCase(), t = $("type").value;
  const shown = links.filter((l) => (!t || l.type === t) && (!f || (l.url + " " + l.text).toLowerCase().includes(f)));
  for (const l of shown) {
    const row = el("label", "item");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = l.checked; cb.onchange = () => { l.checked = cb.checked; count(); };
    row.append(cb, el("span", "tag", l.type));
    const n = el("span", "name", l.text || l.url.split("/").pop() || l.url); n.title = l.url; row.append(n);
    box.append(row);
  }
  if (!shown.length) box.append(el("p", "empty", "No matching links"));
  count();
}
function visibleLinks() {
  const f = $("filter").value.trim().toLowerCase(), t = $("type").value;
  return links.filter((l) => (!t || l.type === t) && (!f || (l.url + " " + l.text).toLowerCase().includes(f)));
}
function count() {
  const n = visibleLinks().filter((l) => l.checked).length;
  $("count").textContent = `(${n} selected)`;
  $("sendLinks").disabled = !n;
  $("sendLinks").textContent = n ? `Download ${n} file${n > 1 ? "s" : ""}` : "Download selected";
}

$("videoBtn").onclick = async () => {
  const r = await send({ type: "video", tabId: tab.id, pageUrl: tab.url, title: tab.title });
  $("videoBtn").textContent = r && r.ok ? "Sent — choose the quality in PyDM" : "PyDM isn't reachable";
};
$("grabBtn").onclick = async () => {
  const raw = await send({ type: "links", tabId: tab.id });
  links = (raw || []).map((l) => ({ ...l, type: typeOf(l.url) })).filter((l) => l.type !== "Page");
  links.forEach((l) => { l.checked = l.type !== "Images" && l.type !== "Other"; });
  $("grab").hidden = false; renderLinks();
};
$("filter").oninput = renderLinks; $("type").onchange = renderLinks;
$("selAll").onchange = () => { visibleLinks().forEach((l) => { l.checked = $("selAll").checked; }); renderLinks(); };
$("sendLinks").onclick = async () => {
  const sel = visibleLinks().filter((l) => l.checked).map((l) => l.url);
  const r = await send({ type: "batch", links: sel, referrer: tab.url });
  $("sendLinks").textContent = r && r.ok ? `Sent ${sel.length} links to PyDM` : "PyDM isn't reachable";
};
$("catchEnabled").onchange = () => send({ type: "save", settings: { catchEnabled: $("catchEnabled").checked } });
$("videoButton").onchange = () => send({ type: "save", settings: { videoButton: $("videoButton").checked } });
$("pause5").onclick = async () => {
  await send({ type: "save", settings: { pausedUntil: $("pause5").dataset.paused ? 0 : Date.now() + 5 * 60 * 1000 } });
  status();
};
$("save").onclick = async () => {
  const port = parseInt($("port").value, 10);
  await send({ type: "save", settings: {
    minSizeKB: Math.max(0, parseInt($("minSizeKB").value, 10) || 0), exts: $("exts").value,
    neverSites: $("neverSites").value, alwaysSites: $("alwaysSites").value,
    port: port >= 1024 && port <= 65535 ? port : 9614,
  } });
  $("save").textContent = "Saved"; setTimeout(() => { $("save").textContent = "Save settings"; }, 1500);
  status();
};

(async () => { tab = await currentTab(); status(); renderMedia(); })();
