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
  let origin;
  try { origin = new URL(raw).origin; }
  catch (e) { setStatus("서버 주소 형식이 올바르지 않습니다 (http://호스트:포트)", "err"); return; }
  // Request host permission for the dashboard origin so the background
  // worker can POST cross-origin (must be from this user gesture).
  chrome.permissions.request({ origins: [origin + "/*"] }, (granted) => {
    if (chrome.runtime.lastError) { setStatus("권한 요청 실패: " + chrome.runtime.lastError.message, "err"); return; }
    if (!granted) { setStatus("호스트 권한이 거부되어 저장 기능이 동작하지 않습니다", "err"); return; }
    chrome.storage.sync.set({ serverUrl: raw, token }, () => {
      setStatus("✅ 저장됨 — 이제 영상 패널의 🧠/📒 버튼이 동작합니다", "ok");
    });
  });
});
