// Service worker — performs the cross-origin POST to the Second Brain
// dashboard. The content script runs in the HTTPS youtube.com context, so
// a direct fetch to an http:// dashboard is blocked as mixed content. A
// service-worker fetch (extension origin, with host permission granted in
// the popup) is not subject to that block.
"use strict";

async function handleIngest(msg) {
  const cfg = await chrome.storage.sync.get({ serverUrl: "", token: "" });
  const base = (cfg.serverUrl || "").trim().replace(/\/+$/, "");
  const token = (cfg.token || "").trim();
  if (!base || !token) {
    return { ok: false, error: "설정(팝업)에서 서버 주소와 토큰을 먼저 입력하세요" };
  }
  if (!msg.url || !/^https?:\/\//.test(msg.url)) {
    return { ok: false, error: "유효한 URL이 아닙니다" };
  }
  const endpoint = base + "/" + token + "/ingest";
  let resp;
  try {
    resp = await fetch(endpoint, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ url: msg.url, target: msg.target || "rag" })
    });
  } catch (e) {
    return { ok: false, error: "연결 실패 (서버 주소/네트워크 확인): " + String(e.message || e) };
  }
  if (!resp.ok) {
    let detail = "";
    try { const j = await resp.json(); detail = j.error || ""; } catch (e) {}
    return { ok: false, error: "HTTP " + resp.status + (detail ? " — " + detail : "") };
  }
  let data = {};
  try { data = await resp.json(); } catch (e) {}
  return { ok: true, id: data.id, target: data.target, duplicate: !!data.duplicate };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "ingest") {
    handleIngest(msg)
      .then(sendResponse)
      .catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
    return true; // async response
  }
});
