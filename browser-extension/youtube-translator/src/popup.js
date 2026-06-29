const DEFAULTS = { enabled: true, overlay: true, showOriginal: false, panel: true, target: "ko" };
const TOGGLES = ["enabled", "overlay", "showOriginal", "panel"];

chrome.storage.sync.get(DEFAULTS, (s) => {
  const settings = { ...DEFAULTS, ...s };
  TOGGLES.forEach((k) => {
    const el = document.getElementById(k);
    el.checked = !!settings[k];
    el.addEventListener("change", () => chrome.storage.sync.set({ [k]: el.checked }));
  });
  const target = document.getElementById("target");
  target.value = settings.target;
  target.addEventListener("change", () => chrome.storage.sync.set({ target: target.value }));
});

// ---- 두뇌 저장 설정: 서버 주소 + 토큰 + 호스트 권한 ----
const serverEl = document.getElementById("serverUrl");
const tokenEl = document.getElementById("token");
const statusEl = document.getElementById("status");

chrome.storage.sync.get({ serverUrl: "", token: "" }, (s) => {
  serverEl.value = s.serverUrl || "";
  tokenEl.value = s.token || "";
});

function setStatus(msg, kind) {
  statusEl.textContent = msg;
  statusEl.className = "status" + (kind ? " " + kind : "");
}

document.getElementById("saveServer").addEventListener("click", () => {
  const raw = serverEl.value.trim().replace(/\/+$/, "");
  const token = tokenEl.value.trim();
  if (!raw || !token) { setStatus("서버 주소와 토큰을 모두 입력하세요", "err"); return; }
  try { new URL(raw); }
  catch (e) { setStatus("서버 주소 형식이 올바르지 않습니다 (http://호스트:포트)", "err"); return; }
  // Host permission is declared in the manifest (install-time), so the
  // background worker can already POST cross-origin — just persist the
  // settings. (Earlier版 requested an optional permission with a port in
  // the match pattern, which is invalid → request silently failed → the
  // settings never saved. Fixed by granting host access at install.)
  chrome.storage.sync.set({ serverUrl: raw, token }, () => {
    if (chrome.runtime.lastError) {
      setStatus("저장 실패: " + chrome.runtime.lastError.message, "err");
      return;
    }
    setStatus("✅ 저장됨 — 이제 영상 패널의 🧠/📒 버튼이 동작합니다", "ok");
  });
});
