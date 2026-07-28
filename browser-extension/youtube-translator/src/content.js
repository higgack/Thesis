// YouTube 번역 & 스크립트 - content script (isolated world)
(function () {
  "use strict";
  const DEFAULTS = {
    enabled: true,
    overlay: true,
    showOriginal: false,
    panel: true,
    target: "ko"
  };
  let settings = { ...DEFAULTS };
  let segments = []; // [{ start, dur, text, translation }]
  let currentVideoId = null;
  let currentTitle = "";
  let lastIndex = -1;
  let rafId = null;
  function getVideo() {
    return document.querySelector("video.html5-main-video, video");
  }
  function cleanWatchUrl() {
    // Canonical watch URL (id-only) — strips playlist/time params so the
    // bot's dedup keys on the same URL however the page was reached.
    if (currentVideoId) return "https://www.youtube.com/watch?v=" + currentVideoId;
    return location.href.split("&")[0];
  }
  function fmtTime(sec) {
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
    const ss = String(s).padStart(2, "0");
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  }
  // ---------- page-world bridge ----------
  function injectPageScript() {
    if (document.getElementById("ytt-inject")) return;
    const s = document.createElement("script");
    s.id = "ytt-inject";
    s.src = chrome.runtime.getURL("src/inject.js");
    (document.head || document.documentElement).appendChild(s);
  }
  function requestPlayerData() {
    return new Promise((resolve) => {
      const reqId = Math.random().toString(36).slice(2);
      let done = false;
      function handler(ev) {
        if (ev.source !== window) return;
        const msg = ev.data;
        if (!msg || msg.__ytt !== "response" || msg.reqId !== reqId) return;
        done = true;
        window.removeEventListener("message", handler);
        resolve(msg.data);
      }
      window.addEventListener("message", handler);
      window.postMessage({ __ytt: "request", reqId }, "*");
      setTimeout(() => {
        if (done) return;
        window.removeEventListener("message", handler);
        resolve(null);
      }, 5000);
    });
  }
  // ---------- transcript fetching ----------
  function pickTrack(tracks) {
    if (!tracks || !tracks.length) return null;
    const manual = tracks.filter((t) => t.kind !== "asr");
    const pool = manual.length ? manual : tracks;
    return pool.find((t) => /^en/.test(t.lang)) || pool[0];
  }
  async function fetchTimedText(baseUrl, tlang) {
    let url = baseUrl;
    if (!/[?&]fmt=/.test(url)) url += "&fmt=json3";
    if (tlang) url += "&tlang=" + encodeURIComponent(tlang);
    const res = await fetch(url, { credentials: "include" });
    if (!res.ok) throw new Error("timedtext " + res.status);
    const json = await res.json();
    return (json.events || [])
      .filter((e) => e.segs)
      .map((e) => ({
        ms: e.tStartMs || 0,
        dur: e.dDurationMs || 0,
        text: e.segs.map((s) => s.utf8 || "").join("").replace(/\s+/g, " ").trim()
      }))
      .filter((e) => e.text);
  }
  async function buildSegments(track, target) {
    const original = await fetchTimedText(track.baseUrl);
    let translated = [];
    if (target && !new RegExp("^" + target).test(track.lang)) {
      try {
        translated = await fetchTimedText(track.baseUrl, target);
      } catch (e) {}
    }
    const trByMs = {};
    translated.forEach((e) => { trByMs[e.ms] = e.text; });
    return original.map((e) => ({
      start: e.ms / 1000,
      dur: e.dur / 1000,
      text: e.text,
      translation: trByMs[e.ms] || ""
    }));
  }
  // ---------- subtitle overlay ----------
  function ensureOverlay() {
    let el = document.getElementById("ytt-overlay");
    if (el) return el;
    const player = document.querySelector("#movie_player");
    if (!player) return null;
    el = document.createElement("div");
    el.id = "ytt-overlay";
    el.innerHTML = '<div class="ytt-ov-trans"></div><div class="ytt-ov-orig"></div>';
    player.appendChild(el);
    return el;
  }
  function clearOverlay() {
    const el = document.getElementById("ytt-overlay");
    if (el) el.remove();
  }
  function updateOverlay(seg) {
    if (!settings.overlay || !seg) {
      const el = document.getElementById("ytt-overlay");
      if (el) el.style.display = "none";
      return;
    }
    const el = ensureOverlay();
    if (!el) return;
    el.style.display = "block";
    const trans = el.querySelector(".ytt-ov-trans");
    const orig = el.querySelector(".ytt-ov-orig");
    trans.textContent = seg.translation || seg.text;
    if (settings.showOriginal && seg.translation) {
      orig.textContent = seg.text;
      orig.style.display = "block";
    } else {
      orig.style.display = "none";
    }
  }
  // ---------- 두뇌저장 (RAG / 학습노트) ----------
  function toast(message, ok) {
    let t = document.getElementById("ytt-toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "ytt-toast";
      document.body.appendChild(t);
    }
    t.textContent = message;
    t.className = ok === false ? "ytt-toast-err" : (ok ? "ytt-toast-ok" : "");
    t.classList.add("ytt-toast-show");
    clearTimeout(t.__hide);
    t.__hide = setTimeout(() => t.classList.remove("ytt-toast-show"), 3200);
  }
  function saveToBrain(target, btn) {
    const url = cleanWatchUrl();
    const label = target === "note" ? "학습노트" : "RAG";
    if (btn) { btn.disabled = true; btn.dataset.old = btn.textContent; btn.textContent = "…"; }
    toast(label + "에 보내는 중…");
    chrome.runtime.sendMessage(
      { type: "ingest", target, url, title: currentTitle || "" },
      (resp) => {
        if (btn) { btn.disabled = false; if (btn.dataset.old) btn.textContent = btn.dataset.old; }
        if (chrome.runtime.lastError) {
          toast("저장 실패: " + chrome.runtime.lastError.message, false);
          return;
        }
        if (resp && resp.ok) {
          if (resp.duplicate) toast("ℹ️ " + label + " 이미 학습 큐에 있어요", true);
          else toast("✅ " + label + " 학습 큐에 추가됨", true);
        } else {
          toast("⚠️ " + (resp && resp.error ? resp.error : "저장 실패"), false);
        }
      }
    );
  }
  // ---------- side script panel ----------
  function buildPanel() {
    let panel = document.getElementById("ytt-panel");
    if (panel) return panel;
    panel = document.createElement("div");
    panel.id = "ytt-panel";
    panel.innerHTML = `
      <div class="ytt-panel-head">
        <span class="ytt-panel-title">스크립트</span>
        <div class="ytt-panel-actions">
          <button class="ytt-btn ytt-save" data-act="save-rag" title="현재 영상을 RAG에 저장">🧠 RAG</button>
          <button class="ytt-btn ytt-save" data-act="save-note" title="현재 영상을 학습노트로 저장">📒 노트</button>
          <button class="ytt-btn" data-act="toggle-orig" title="원문 표시/숨김">원문</button>
          <button class="ytt-btn" data-act="copy" title="전체 복사">복사</button>
          <button class="ytt-btn" data-act="collapse" title="접기/펼치기">–</button>
        </div>
      </div>
      <div class="ytt-panel-body"></div>`;
    panel.querySelector('[data-act="save-rag"]').addEventListener("click", (e) => {
      saveToBrain("rag", e.currentTarget);
    });
    panel.querySelector('[data-act="save-note"]').addEventListener("click", (e) => {
      saveToBrain("note", e.currentTarget);
    });
    panel.querySelector('[data-act="toggle-orig"]').addEventListener("click", () => {
      panel.classList.toggle("ytt-show-orig");
    });
    panel.querySelector('[data-act="collapse"]').addEventListener("click", (e) => {
      panel.classList.toggle("ytt-collapsed");
      e.currentTarget.textContent = panel.classList.contains("ytt-collapsed") ? "+" : "–";
    });
    panel.querySelector('[data-act="copy"]').addEventListener("click", () => {
      const lines = segments.map((s) => {
        const t = fmtTime(s.start);
        const tr = s.translation && s.translation !== s.text ? "\n" + s.translation : "";
        return `[${t}] ${s.text}${tr}`;
      });
      navigator.clipboard.writeText(lines.join("\n\n")).then(() => {
        const btn = panel.querySelector('[data-act="copy"]');
        const old = btn.textContent;
        btn.textContent = "완료!";
        setTimeout(() => (btn.textContent = old), 1200);
      });
    });
    return panel;
  }
  function mountPanel() {
    if (!settings.panel) {
      const p = document.getElementById("ytt-panel");
      if (p) p.remove();
      return;
    }
    const panel = buildPanel();
    const secondary = document.querySelector("#secondary-inner") || document.querySelector("#secondary");
    if (secondary) {
      if (panel.parentElement !== secondary) secondary.insertBefore(panel, secondary.firstChild);
      panel.classList.remove("ytt-floating");
    } else {
      if (!panel.parentElement) document.body.appendChild(panel);
      panel.classList.add("ytt-floating");
    }
  }
  function renderPanel() {
    if (!settings.panel) return;
    const panel = buildPanel();
    const body = panel.querySelector(".ytt-panel-body");
    body.innerHTML = "";
    if (!segments.length) {
      body.innerHTML = '<div class="ytt-empty">이 영상에는 사용할 수 있는 자막이 없습니다.</div>';
      return;
    }
    const frag = document.createDocumentFragment();
    segments.forEach((seg, i) => {
      const row = document.createElement("div");
      row.className = "ytt-row";
      row.dataset.i = i;
      row.innerHTML = `
        <button class="ytt-ts">${fmtTime(seg.start)}</button>
        <div class="ytt-texts">
          <div class="ytt-orig">${escapeHtml(seg.text)}</div>
          ${seg.translation && seg.translation !== seg.text ? `<div class="ytt-trans">${escapeHtml(seg.translation)}</div>` : ""}
        </div>`;
      row.querySelector(".ytt-ts").addEventListener("click", () => {
        const v = getVideo();
        if (v) { v.currentTime = seg.start + 0.01; v.play(); }
      });
      frag.appendChild(row);
    });
    body.appendChild(frag);
  }
  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  // ---------- sync loop ----------
  function findIndex(t) {
    if (!segments.length) return -1;
    let i = lastIndex >= 0 ? lastIndex : 0;
    if (segments[i] && t < segments[i].start) i = 0;
    for (; i < segments.length; i++) {
      const seg = segments[i];
      const end = seg.start + (seg.dur || 4);
      if (t < seg.start) return i > 0 ? i - 1 : -1;
      if (t >= seg.start && t <= end) return i;
    }
    return segments.length - 1;
  }
  function tick() {
    rafId = requestAnimationFrame(tick);
    const v = getVideo();
    if (!v || !segments.length) return;
    const idx = findIndex(v.currentTime);
    if (idx === lastIndex) return;
    lastIndex = idx;
    const seg = idx >= 0 ? segments[idx] : null;
    updateOverlay(seg);
    highlightRow(idx);
  }
  let lastHighlighted = null;
  function highlightRow(idx) {
    const panel = document.getElementById("ytt-panel");
    if (!panel) return;
    if (lastHighlighted) lastHighlighted.classList.remove("ytt-active");
    const row = panel.querySelector(`.ytt-row[data-i="${idx}"]`);
    if (row) {
      row.classList.add("ytt-active");
      lastHighlighted = row;
      const body = panel.querySelector(".ytt-panel-body");
      const rTop = row.offsetTop;
      const bTop = body.scrollTop;
      const bH = body.clientHeight;
      if (rTop < bTop || rTop > bTop + bH - 40) {
        body.scrollTo({ top: rTop - bH / 2, behavior: "smooth" });
      }
    }
  }
  function startLoop() { if (rafId == null) tick(); }
  // ---------- orchestration ----------
  async function init() {
    if (!settings.enabled) { teardown(); return; }
    if (!location.pathname.startsWith("/watch")) { teardown(); return; }
    injectPageScript();
    const data = await requestPlayerData();
    if (!data) return;
    if (data.title) currentTitle = data.title;
    if (data.videoId && data.videoId === currentVideoId && segments.length) {
      mountPanel(); renderPanel(); startLoop(); return;
    }
    currentVideoId = data.videoId;
    lastIndex = -1;
    segments = [];
    const track = pickTrack(data.tracks);
    mountPanel();
    if (!track) { renderPanel(); return; }
    try {
      segments = await buildSegments(track, settings.target);
    } catch (e) { segments = []; }
    renderPanel();
    startLoop();
  }
  function teardown() {
    clearOverlay();
    const p = document.getElementById("ytt-panel");
    if (p) p.remove();
    if (rafId != null) { cancelAnimationFrame(rafId); rafId = null; }
  }
  // ---------- settings + navigation ----------
  function loadSettings() {
    return new Promise((resolve) => {
      chrome.storage.sync.get(DEFAULTS, (s) => {
        settings = { ...DEFAULTS, ...s };
        resolve();
      });
    });
  }
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "sync") return;
    let needRebuild = false;
    for (const k in changes) {
      if (!(k in DEFAULTS)) continue; // ignore serverUrl/token changes
      const newVal = changes[k].newValue;
      if (k === "target" && newVal !== settings.target) needRebuild = true;
      settings[k] = newVal;
    }
    if (!settings.enabled) { teardown(); return; }
    if (needRebuild) {
      currentVideoId = null;
      init();
    } else {
      mountPanel();
      renderPanel();
      const seg = lastIndex >= 0 ? segments[lastIndex] : null;
      updateOverlay(seg);
    }
  });
  document.addEventListener("yt-navigate-finish", () => {
    setTimeout(() => init(), 600);
  });
  (async function main() {
    await loadSettings();
    setTimeout(() => init(), 800);
  })();
})();
