// Runs in the page's MAIN world so it can read YouTube's internal player data.
(function () {
  "use strict";
  function readPlayerResponse() {
    try {
      const player = document.getElementById("movie_player");
      if (player && typeof player.getPlayerResponse === "function") {
        const pr = player.getPlayerResponse();
        if (pr) return pr;
      }
    } catch (e) {}
    return window.ytInitialPlayerResponse || null;
  }
  function extractName(name, fallback) {
    if (!name) return fallback;
    if (name.simpleText) return name.simpleText;
    if (name.runs && name.runs[0]) return name.runs[0].text;
    return fallback;
  }
  function getData() {
    const pr = readPlayerResponse();
    if (!pr) return { tracks: [], videoId: null };
    let tracks = [];
    const cap =
      pr.captions &&
      pr.captions.playerCaptionsTracklistRenderer &&
      pr.captions.playerCaptionsTracklistRenderer.captionTracks;
    if (Array.isArray(cap)) {
      tracks = cap.map((t) => ({
        baseUrl: t.baseUrl,
        lang: t.languageCode,
        name: extractName(t.name, t.languageCode),
        kind: t.kind || ""
      }));
    }
    return {
      tracks: tracks,
      videoId: (pr.videoDetails && pr.videoDetails.videoId) || null,
      title: (pr.videoDetails && pr.videoDetails.title) || ""
    };
  }
  window.addEventListener("message", function (ev) {
    if (ev.source !== window) return;
    const msg = ev.data;
    if (!msg || msg.__ytt !== "request") return;
    window.postMessage({ __ytt: "response", reqId: msg.reqId, data: getData() }, "*");
  });
})();
