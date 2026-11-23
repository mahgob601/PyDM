// PyDM: shows a "Download video" button over videos on the page. Runs isolated from the page;
// it only reads video sources and asks the background worker to hand them to PyDM.
(() => {
  const B = globalThis.browser ?? globalThis.chrome;
  if (window.top !== window || window.__pydmButtons) return;
  window.__pydmButtons = true;
  let enabled = true;
  try {
    B.runtime.sendMessage({ type: "settings" }, (r) => { enabled = !!(r && r.videoButton); if (enabled) scan(); });
  } catch { return; }

  const host = document.createElement("pydm-overlay");
  host.style.cssText = "position:absolute;top:0;left:0;width:0;height:0;z-index:2147483647;";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `<style>
    button{position:absolute;display:flex;align-items:center;gap:6px;padding:6px 11px;border:0;border-radius:10px;
    font:600 12.5px/1 -apple-system,Segoe UI,Inter,Roboto,sans-serif;color:#fff;cursor:pointer;opacity:.0;white-space:nowrap;
    background:linear-gradient(90deg,#7c6cff,#4f8cff);box-shadow:0 4px 14px rgba(0,0,0,.35);transition:opacity .15s}
    button.show{opacity:.96} button:hover{opacity:1} button.sent{background:#22c55e} button.err{background:#f43f5e}
    svg{width:14px;height:14px}</style>`;
  const buttons = new Map();   // video -> button
  const icon = '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14"/></svg>';

  function candidates(v) {
    const out = [];
    const add = (u, type) => { try { u = new URL(u, location.href).href; } catch { return; }
      if (/^https?:/i.test(u)) out.push({ url: u, type }); };
    if (v.currentSrc) add(v.currentSrc, "video");
    if (v.src) add(v.src, "video");
    v.querySelectorAll("source[src]").forEach((s) => add(s.getAttribute("src"), s.type || "video"));
    return out;
  }
  function place(v, b) {
    const r = v.getBoundingClientRect();
    const visible = r.width >= 200 && r.height >= 110 && r.bottom > 0 && r.top < innerHeight;
    b.style.display = visible ? "flex" : "none";
    if (!visible) return;
    b.style.top = `${r.top + scrollY + 10}px`;
    b.style.left = `${r.right + scrollX - b.offsetWidth - 10}px`;
  }
  function attach(v) {
    if (buttons.has(v)) return;
    const b = document.createElement("button");
    b.innerHTML = icon + "<span>Download video</span>";
    b.title = "Download with PyDM";
    b.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      B.runtime.sendMessage({ type: "video-button", candidates: candidates(v) }, (r) => {
        b.className = "show " + (r && r.ok ? "sent" : "err");
        b.querySelector("span").textContent = r && r.ok ? "Sent to PyDM" : (r && r.offline ? "PyDM isn't running" : "Not sent");
        setTimeout(() => { b.className = ""; b.querySelector("span").textContent = "Download video"; }, 2500);
      });
    });
    const show = () => { place(v, b); b.classList.add("show"); };
    const hide = () => setTimeout(() => { if (!b.matches(":hover")) b.classList.remove("show"); }, 1500);
    v.addEventListener("mouseenter", show); v.addEventListener("mouseleave", hide);
    v.addEventListener("play", show);
    b.addEventListener("mouseleave", hide);
    root.appendChild(b);
    buttons.set(v, b);
    place(v, b);
  }
  function scan() {
    if (!enabled) return;
    if (!host.isConnected) document.documentElement.appendChild(host);
    document.querySelectorAll("video").forEach(attach);
    for (const [v, b] of buttons) {
      if (!v.isConnected) { b.remove(); buttons.delete(v); } else place(v, b);
    }
  }
  let pending = false;
  const later = () => { if (!pending) { pending = true; requestAnimationFrame(() => { pending = false; scan(); }); } };
  new MutationObserver(later).observe(document.documentElement, { childList: true, subtree: true });
  addEventListener("scroll", later, { passive: true });
  addEventListener("resize", later);
})();
