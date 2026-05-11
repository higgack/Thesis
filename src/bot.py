import asyncio
import base64
import logging
import re
from datetime import datetime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from telegram.request import HTTPXRequest

from . import config
from .store import meta, vector, obsidian, cost, qna
from .ingest import pipeline
from .agent import agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

_INGEST_SEM = asyncio.Semaphore(2)
_INGEST_RETRY_QUEUE: list[dict] = []
_INGEST_FAILED: list[dict] = []
_FAILED_MAX = 200

# Persist these two so a hang/restart doesn't lose state.
_RETRY_QUEUE_PATH = config.DATA_DIR / "retry_queue.json"
_FAILED_LOG_PATH = config.DATA_DIR / "failed_log.json"
_HISTORY_PATH = config.DATA_DIR / "chat_history.json"

# Per-chat conversation memory: keyed by chat_id, holds the most
# recent user/model text turns so follow-up questions like
# "그 회사의 경쟁사는?" carry topic context. Tool-call payloads are
# NOT stored — replaying stale retrievals confuses the model and
# wastes tokens.
_HISTORY: dict[int, list[dict]] = {}
_HISTORY_MAX_TURNS = 7   # 7 user + 7 model = 14 messages
_HISTORY_USER_CAP = 400
_HISTORY_MODEL_CAP = 1200


def _load_persisted_state() -> None:
    """Restore retry queue + failed log from disk on startup."""
    import json
    try:
        if _RETRY_QUEUE_PATH.exists():
            data = json.loads(_RETRY_QUEUE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _INGEST_RETRY_QUEUE.extend(data)
                log.info("restored %d items to retry queue", len(data))
    except Exception:
        log.exception("retry queue load failed")
    try:
        if _FAILED_LOG_PATH.exists():
            data = json.loads(_FAILED_LOG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _INGEST_FAILED.extend(data[-_FAILED_MAX:])
                log.info("restored %d failed entries", len(_INGEST_FAILED))
    except Exception:
        log.exception("failed log load failed")
    try:
        if _HISTORY_PATH.exists():
            data = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, list):
                        _HISTORY[int(k)] = v
                log.info("restored chat history for %d chats", len(_HISTORY))
    except Exception:
        log.exception("chat history load failed")


def _persist_retry_queue() -> None:
    import json
    try:
        _RETRY_QUEUE_PATH.write_text(
            json.dumps(_INGEST_RETRY_QUEUE, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.exception("retry queue persist failed")


def _persist_chat_history() -> None:
    import json
    try:
        _HISTORY_PATH.write_text(
            json.dumps({str(k): v for k, v in _HISTORY.items()},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.exception("chat history persist failed")


def _record_turn(chat_id: int, role: str, text: str,
                 sources: list[str] | None = None,
                 tools: list[str] | None = None) -> None:
    """Append one turn to this chat's rolling history. Trims old turns
    so memory stays bounded; persists to disk so restarts don't drop
    context. Model turns can also carry the sources/tools that produced
    them so a follow-up that doesn't trigger a new search still has the
    previous turn's citations to show."""
    if not text:
        return
    cap = _HISTORY_USER_CAP if role == "user" else _HISTORY_MODEL_CAP
    entry: dict = {"role": role, "text": text[:cap]}
    if role == "model":
        if sources:
            entry["sources"] = list(sources)
        if tools:
            entry["tools"] = list(tools)
    h = _HISTORY.setdefault(chat_id, [])
    h.append(entry)
    max_msgs = _HISTORY_MAX_TURNS * 2
    if len(h) > max_msgs:
        del h[: len(h) - max_msgs]
    _persist_chat_history()


def _last_model_citations(chat_id: int) -> tuple[list[str], list[str]]:
    """Return (sources, tools) from the most recent model turn that
    actually carried citations. Used as a fallback when the current
    turn answered from memory without calling any tool."""
    for entry in reversed(_HISTORY.get(chat_id, [])):
        if entry.get("role") == "model" and entry.get("sources"):
            return list(entry.get("sources") or []), list(entry.get("tools") or [])
    return [], []


def _persist_failed_log() -> None:
    import json
    try:
        _FAILED_LOG_PATH.write_text(
            json.dumps(_INGEST_FAILED, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.exception("failed log persist failed")

URL_RE = re.compile(r"https?://[^\s\)\]<>\"']+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)")
_URL_TRAILING_RE = re.compile(r"[).,;:!?\]]+$")
_MD_BOLD_RE = re.compile(r"\*\*([^\*\n]{1,200}?)\*\*")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)


def _extract_urls(text: str) -> tuple[list[str], str]:
    """Pull markdown-link and plain URLs out of `text`, return cleaned URLs
    plus the leftover text with URL syntax stripped. Trailing closing-paren
    and other punctuation that gets glued onto a URL are removed."""
    urls: list[str] = []

    def _take_md_link(m: "re.Match[str]") -> str:
        urls.append(_URL_TRAILING_RE.sub("", m.group(2)))
        return m.group(1)

    cleaned = _MD_LINK_RE.sub(_take_md_link, text)
    for raw in URL_RE.findall(cleaned):
        url = _URL_TRAILING_RE.sub("", raw)
        if url and url not in urls:
            urls.append(url)
    text_no_urls = URL_RE.sub("", cleaned).strip()
    return urls, text_no_urls


_INTERNAL_TG_RE = re.compile(r"^https?://t\.me/", re.IGNORECASE)
_MAX_URLS_PER_MSG = 5


def _collect_message_urls(msg) -> tuple[list[str], str]:
    """Plain text URLs + markdown links + Telegram text_link entities
    + inline-keyboard URL buttons. Drops t.me internal links, dedups,
    caps at _MAX_URLS_PER_MSG so spammy channels can't trigger runaway
    ingestion."""
    text = msg.text or msg.caption or ""
    urls, plain = _extract_urls(text)

    for ent_list in (msg.entities or [], msg.caption_entities or []):
        for ent in ent_list:
            u = getattr(ent, "url", None)
            if getattr(ent, "type", None) == "text_link" and u and u not in urls:
                urls.append(u)

    rm = getattr(msg, "reply_markup", None)
    if rm and getattr(rm, "inline_keyboard", None):
        for row in rm.inline_keyboard:
            for btn in row:
                u = getattr(btn, "url", None)
                if u and u not in urls:
                    urls.append(u)

    urls = [u for u in urls if not _INTERNAL_TG_RE.match(u)]

    is_forward = bool(
        getattr(msg, "forward_origin", None)
        or getattr(msg, "forward_from_chat", None)
        or getattr(msg, "forward_from", None)
    )
    # Forwarded automation digest (e.g. Noahsummary) tends to bundle 50+
    # URLs per message — let those ride as plain text, the URLs survive
    # inside the body string. Hand-curated forwards (broker reports
    # with 1~4 links) keep their URL ingest path.
    if is_forward and len(urls) >= _MAX_URLS_PER_MSG:
        urls = []
    else:
        urls = urls[:_MAX_URLS_PER_MSG]
    return urls, plain
_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)\n```", re.DOTALL)
_NUMBERED_SECTION_RE = re.compile(
    r"^(\s*)(\d+)\.\s+(.{3,100}?)[:：]?\s*$", re.MULTILINE
)
_SECTION_EMOJIS = ["📌", "🔹", "🔸", "⚙️", "🧪", "💡", "📊", "🎯", "⚡", "🔧"]

# Per-tool emoji so the user can tell at a glance whether the answer
# came from their saved RAG store, an external academic DB, the web,
# etc. Anything not listed falls through to 🔧.
_TOOL_EMOJI = {
    "search_my_brain": "🧠",
    "compare_papers": "🧠",
    "recent_docs": "🧠",
    "search_papers": "📄",
    "web_search": "🌐",
    "ingest_url": "📥",
}


def _format_tool_calls(calls: list[str]) -> str:
    parts = [f"{_TOOL_EMOJI.get(c, '🔧')} {c}" for c in calls]
    return " → ".join(parts)
_SEP = "━" * 22


def _extract_mermaid(text: str) -> tuple[str, list[str]]:
    """Pull every ```mermaid``` block out so we can render it as a photo
    instead of leaking the raw code to the user."""
    blocks = [m.group(1).strip() for m in _MERMAID_BLOCK_RE.finditer(text)]
    cleaned = _MERMAID_BLOCK_RE.sub("", text).strip()
    return cleaned, blocks


async def _render_mermaid_png(code: str) -> bytes:
    """Render a Mermaid diagram to PNG bytes, trying kroki POST first
    (most lenient parser, no URL length limit) and falling back to
    mermaid.ink GET."""
    import httpx
    code = code.strip()

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.post(
                "https://kroki.io/mermaid/png",
                content=code.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
            )
            if r.status_code == 200 and r.headers.get(
                "content-type", ""
            ).startswith("image/"):
                return r.content
            log.warning("kroki failed: %d %s",
                        r.status_code, r.text[:200])
    except Exception as e:
        log.warning("kroki render failed: %s", e)

    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    url = f"https://mermaid.ink/img/{encoded}?type=png&bgColor=white"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content


def _enforce_format(text: str) -> str:
    """Strip stray markdown and beautify numbered sections with separators.
    Telegram is plain-text (no parse_mode), so we use unicode glyphs for the
    visual hierarchy the model is supposed to produce but sometimes skips."""
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    counter = [0]

    def replace(m: "re.Match[str]") -> str:
        indent, num, title = m.group(1), m.group(2), m.group(3).rstrip(":：").strip()
        if any(ch in title for ch in "•|") or "  " in title:
            return m.group(0)
        emoji = _SECTION_EMOJIS[counter[0] % len(_SECTION_EMOJIS)]
        counter[0] += 1
        return f"\n{indent}{_SEP}\n{indent}{emoji} {num}. {title}\n{indent}{_SEP}"

    return _NUMBERED_SECTION_RE.sub(replace, text)


def _strip_markdown(text: str) -> str:
    return _enforce_format(text)


# Lightweight stripper for command output (summary/title/detail fields).
# Telegram renders messages as plain text, so any markdown the source
# happens to use shows up as raw `**`, `##`, `*` characters and hurts
# readability. _enforce_format above is too heavy for these fields
# (it adds section separators), so we use a minimal pass instead.
_MD_BOLD2_RE_ = re.compile(r"\*\*([^\*\n]{1,300}?)\*\*")
_MD_ITALIC_RE_ = re.compile(r"(?<!\*)\*([^\*\n]+?)\*(?!\*)")
_MD_CODE_INLINE_RE_ = re.compile(r"`([^`\n]+?)`")
_MD_HEADER_RE_ = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_LIST_STAR_RE_ = re.compile(r"^(\s*)\*\s+", re.MULTILINE)
_BLANK_LINES_RE_ = re.compile(r"\n{3,}")


def _clean_text(text: str) -> str:
    if not text:
        return text or ""
    text = _MD_BOLD2_RE_.sub(r"\1", text)
    text = _MD_ITALIC_RE_.sub(r"\1", text)
    text = _MD_CODE_INLINE_RE_.sub(r"\1", text)
    text = _MD_HEADER_RE_.sub("", text)
    text = _MD_LIST_STAR_RE_.sub(r"\1• ", text)
    text = _BLANK_LINES_RE_.sub("\n\n", text)
    return text.strip()


def _is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == config.TELEGRAM_OWNER_ID)


async def _typing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)


async def _sustained_typing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Re-send the 'typing...' chat action every few seconds so the
    user sees continuous activity through long agent runs (Pro
    synthesis on a 50-doc compare can be ~30-60s)."""
    while True:
        try:
            await _typing(update, ctx)
        except Exception:
            pass
        try:
            await asyncio.sleep(4)
        except asyncio.CancelledError:
            break


_HELP_TEXT = """<b>🧠 SECOND BRAIN 봇 사용법</b>

<b>【1. 명령어】</b>
▸ 조회·검색
 • /find &lt;키워드&gt;  제목·요약·메타 검색
 • /recent [N]  최근 N개 (기본 10)
 • /stats  문서·청크 수
 • /usage  인입속도·큐·비용 (오늘/7일/30일·일별 그래프·모델별·ingest/query)
▸ 대화·메모리
 • /reset  대화 메모리 초기화 (토픽 바뀔 때)
▸ 장애·재시도
 • /failed  실패 목록 + [🔁재시도][🗑비우기]
 • /failed_retry  탭 → 일괄 재시도
 • /failed_clear  탭 → 비우기
 • /queue  자동 재시도 대기 (2분 간격)
▸ 정리·삭제
 • /forget &lt;id&gt;  특정 문서 삭제
 • /forget_search &lt;키워드&gt;  최대 5건 안전 삭제
 • /forget_search_all &lt;키워드&gt;  안전장치 없이 일괄 삭제
 • /forget_qna &lt;id&gt;  Q&amp;A 1건 삭제 (대시보드 q-{id}.html의 id)
 • /forget_qna_search &lt;키워드&gt;  Q&amp;A 일괄 삭제 (질문/답변 매칭)
 • /dedupe·/dedupe_confirm  중복 후보 → 일괄 제거 (긴 본문만 유지)
 • /cleanup·/cleanup_confirm  노이즈 후보 → 일괄 제거
▸ 고급
 • /deep &lt;질문&gt;  Pro 모델 (어려운 추론)
▸ 시작
 • /start /help

<b>【2. 핵심 원리】</b>
 • 채널: 무엇이든 → 자동 수집·요약·임베딩·Obsidian
 • DM: 자연어 → 에이전트가 도구 자동 선택
 • 답변마다 (사용 자료 시점: YYYY.MM~YYYY.MM)
 • 끝줄에 도구 이모지
 • 메모리: 최근 7턴 자동 ("그 회사 경쟁사는?" 가능, /reset으로 초기화)
 • 쿼리 확장: 짧은 질문은 facet 2개로 분해 검색
 • 비용 추적: 모든 Gemini 콜 SQLite 누적
 • Q&amp;A 영구 보관: SQLite + 정적 웹 대시보드 자동
 • 로컬 reranker (BGE): 검색 rerank 비용 0 + 속도 ↑
 • 답변 끝: (사용 자료 시점: 발행일 · 학습: YYYY.MM)

<b>【3. 답변 출처 도구】</b>
 🧠 search_my_brain  저장 자료 단일 검색
 🧠 compare_papers  저장 자료 다수(50) 통합·비교
   (25개+ 반환 시 Pro 모델 자동 합성)
 🧠 recent_docs  최근 학습 목록
 📄 search_papers  외부 학술 (S2→arXiv)
 🌐 web_search  실시간 구글 (명시 시만)
 📥 ingest_url  URL 학습
 예) 🧠→📄 = 저장+외부 / 🧠→🧠 = A vs B

<b>【4. 자연어 트리거】</b>
 🧠 brain — "삼성전기 MLCC 동향"
 🧠 compare — "정리/리뷰/통합/비교/전체"
 📄 papers — "찾아줘/추천/새로운/어떤 논문"
 🌐 web — "웹/구글/오늘/실시간/지금" 필수
   ⚠️ "최근/요즘"만으로는 안 감
 📥 ingest — "이거 학습해줘 URL" 또는 URL만
 후속 질문 — 대명사 OK

<b>【5. 자료 인입】</b>
 URL/PDF/PPTX/DOCX/XLSX/이미지/음성/YouTube/텍스트 그냥 보내기
 • PDF: 텍스트+OCR fallback (arXiv 자동인식)
        차트/표 많은 PDF는 Vision OCR 자동 보강
 • 이미지: 캡션 ≥80자 캡션만 / 짧으면 OCR / [OCR] 태그 강제 병행
 • 음성: Gemini STT (캡션 prepend)
 • YouTube: 자막→Jina fallback
 차단: LinkedIn/FB/IG/카스 + Reuters/Bloomberg/WSJ/FT/Economist/NYT/WaPo/Barrons

<b>【6. 자동 포워딩 (24/7)】</b>
 forward-listener: LISTEN_CHANNEL→FORWARD_TARGET 미러링
 Telethon auto-reconnect + Docker restart
 학습 차단 자동:
  • 슬래시 명령 forward 안 함
  • 그 명령 reply도 차단 (reply_to 추적)
  • INGEST_SKIP_PATTERNS env (세미콜론 구분)
 큰 채널 백필: tmux + python -m src.scripts.import_channel &lt;ch&gt; --resume

<b>【7. 메타데이터 자동 추출】</b>
 Flash-Lite ~₩0.5/문서:
  🏢 회사명 (계열사 구분) · 🏷 태그 1~5 · 📅 발행일 YYYY.MM
 → /find·중복알림·답변 출처에 표시

<b>【8. 웹 대시보드】</b>
 바로가기: http://34.64.89.160:8082/1e68e9fae4e6fb1f8298bdee768eb73b/
 Basic Auth: 사용자명/비밀번호 (.env 참조)
 구성:
  • 통계 카드 4장 (Q&amp;A·학습자료·오늘·이번 달)
  • 검색창 (질문/답변/출처 즉시 필터)
  • 도구 칩 4개 (🧠📄🌐📥) 다중 선택
  • 날짜별 접이식 섹션
  • 카드 클릭 → 답변 펼침 / 제목 → 상세
  • 🗑 버튼 → 1-탭 삭제 (서버 즉시 반영)
  • 푸터: 생성 시각 · 누적 건수 · 무제한 보관
 봇 답변 시 + 60초 주기 자동 갱신
 테마: 19:00~07:00 KST 다크 / 그 외 라이트 자동

<b>【9. 답변 품질】</b>
 • 자료 시점 표기 필수
 • brain 안 부르고 web만 부르는 routing 금지
 • "최근/요즘"만으로 web X → "웹에서/오늘/실시간" 필요
 • 자료 부족 시 솔직히 "부족"
 • web 결과는 [도메인]으로 인용
 • 후속 질문: 모델이 추가 검색 필요성 자율 판단
   메모리만으로 충분하면 스킵, 새 각도면 검색
 • 출처는 매 답변에 항상 표시 (새 검색 X면 이전 자료 재사용)

<b>【10. 운영 / 비용 / 안정성】</b>
 • 동시성 Semaphore(2) · 메모리 bot 1500m / listener 400m
 • 재시도 5회×90s → 영구실패 /failed
 • 영속: retry_queue·failed_log·chat_history·qna.db·cost.db·dashboard
 • BM25 캐시: 부팅 시 백그라운드 빌드 (41k 1~2분, 빌드 중엔 dense-only)
 • BGE-reranker-base: 로컬 cross-encoder (~400MB, 첫 호출만 다운로드)
 • Pro 합성 자동 트리거: compare_papers가 25개+ 반환 시
 • 비용 단가 (1M 토큰): Pro ₩1,750·Flash ₩420·Flash-Lite ₩140·Embed ₩210
   환율 ₩1,400/USD 추정 ±20%
 • 대시보드: systemd second-brain-dashboard.service (8082)
 • SQLite 잠금: import_channel↔listener 동시 X
 • Obsidian: ./data/obsidian/

<b>【11. 트러블슈팅】</b>
 • "본문 비어있음" → 차단 도메인/paywall
 • 봇 응답 없음 → docker logs thesis-bot-1
 • 채널 이중 인입 → import_channel↔listener 동시 X
 • brain 검색 에러 → BM25 빌드 중, 30초 후
 • 답변 토픽 어긋남 → /reset
 • /usage 비용 급등 → audio/Pro/web 다발 의심
 • 대시보드 빈 폴더 → docker compose exec bot python -c "from src.dashboard import regenerate; regenerate.regenerate()"
 • 봇 메타글 학습 → /forget_search_all + INGEST_SKIP_PATTERNS
 • 답변 길어 메시지 잘림 → 자동 분할 (본문/출처 별도 말풍선)"""


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await update.message.reply_text(
        _HELP_TEXT,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await update.message.reply_text(
        f"문서 {meta.count()}개 / 청크 {vector.chunk_count()}개"
    )


async def cmd_usage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ingest velocity, type breakdown, rough cost band so the user
    can spot anomalies (sudden surge, drop) at a glance."""
    if not _is_owner(update):
        return
    s = meta.usage_stats()
    chunks = vector.chunk_count()
    queue_len = len(_INGEST_RETRY_QUEUE)
    failed_len = len(_INGEST_FAILED)

    types_line = ", ".join(f"{t}:{c}" for t, c in s["types"][:8]) or "-"

    today = cost.today_krw()
    week = cost.period_krw(7)
    mtd = cost.month_to_date_krw()
    by_today = today["by_model"]
    cost_lines = []
    for m in ("gemini-2.5-pro", "gemini-2.5-flash",
              "gemini-2.5-flash-lite", "gemini-embedding-001"):
        if m in by_today:
            d = by_today[m]
            cost_lines.append(
                f"    {m.replace('gemini-2.5-', '').replace('gemini-', '')}"
                f"  ₩{d['cost']:,.1f}  ({d['calls']}콜)"
            )
    cost_breakdown = ("\n" + "\n".join(cost_lines)) if cost_lines else ""

    by_purpose = today.get("by_purpose", {})
    purpose_lines = []
    for tag, label in (("ingest", "📥 ingest"), ("query", "💬 query"),
                       ("unknown", "❓ unknown")):
        if tag in by_purpose:
            d = by_purpose[tag]
            purpose_lines.append(
                f"    {label}  ₩{d['cost']:,.1f}  ({d['calls']}콜)"
            )
    purpose_breakdown = ("\n" + "\n".join(purpose_lines)) if purpose_lines else ""

    # Tiny inline bar chart for the last 7 days so trends are visible
    # without leaving the /usage screen.
    daily = cost.daily_breakdown(7)
    max_cost = max((d["cost"] for d in daily), default=0.0)
    daily_lines = []
    for d in daily:
        bar_len = int(round((d["cost"] / max_cost) * 20)) if max_cost else 0
        bar = "█" * bar_len if bar_len else "·"
        daily_lines.append(
            f"  {d['date'][5:]}  {bar:<20}  ₩{d['cost']:,.0f}"
            + (f"  ({d['calls']}콜)" if d["calls"] else "")
        )
    daily_block = "\n".join(daily_lines)

    out = (
        "📊 봇 사용 현황\n"
        f"\n총 문서: {s['total']}개  /  청크: {chunks}개"
        f"\n\n📥 ingest 속도"
        f"\n  • 24h: {s['last_24h']}건"
        f"\n  • 7d:  {s['last_7d']}건"
        f"\n  • 30d: {s['last_30d']}건"
        f"\n\n📂 type별 분포"
        f"\n  {types_line}"
        f"\n\n💰 추정 비용 (Gemini API · KST)"
        f"\n  • 오늘: ₩{today['total_krw']:,.0f}  ({today['calls']}콜)"
        f"\n  • 7일:  ₩{week['total_krw']:,.0f}"
        f"\n  • 이번 달 ({mtd['year']}년 {mtd['month']}월): "
        f"₩{mtd['total_krw']:,.0f}  ({mtd['day']}일차)"
        f"{cost_breakdown}"
        f"{purpose_breakdown}"
        f"\n\n📅 최근 7일 (KST)\n{daily_block}"
        f"\n\n📖 모델 용도"
        f"\n  embedding   인입 chunk+summary / 질문 쿼리 임베딩"
        f"\n  flash-lite  인입 요약·메타·OCR·STT / 질문 확장·rerank·verify"
        f"\n  flash       질문 agent 추론·web_search"
        f"\n  pro         /deep 질문 전용"
        f"\n\n📚 가장 최근 학습"
        f"\n  {s['latest_title'][:80]}"
        f"\n  {s['latest_at'][:16].replace('T', ' ')}"
        f"\n\n🔁 retry 큐: {queue_len}건"
        f"\n❌ failed 누적: {failed_len}건"
    )
    await update.message.reply_text(out)


async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    n = 10
    if ctx.args and ctx.args[0].isdigit():
        n = max(1, min(int(ctx.args[0]), 50))
    items = meta.recent(n)
    if not items:
        await update.message.reply_text("아직 비어있어요.")
        return
    lines = [f"📚 최근 {len(items)}개 학습"]
    for r in items:
        title = _clean_text(r.get("title") or "(제목 없음)")[:90]
        ingested = (r.get("ingested_at") or "")[:10]
        lines.append(f"\n[{r['type']}]  {title}\n  {ingested}  ·  id {r['id']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not ctx.args:
        await update.message.reply_text("사용법: /forget <doc_id>")
        return
    doc_id = ctx.args[0]
    n = vector.delete_doc(doc_id)
    ok = meta.delete(doc_id)
    await update.message.reply_text(
        f"{'삭제됨' if ok else '메타 없음'} · 청크 {n}개 제거"
    )


async def cmd_cleanup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    noisy = meta.find_noise()
    if not noisy:
        await update.message.reply_text("정리할 노이즈 없음 ✨")
        return
    args = [a.lower() for a in (ctx.args or [])]
    if "confirm" in args:
        n_chunks = 0
        for r in noisy:
            n_chunks += vector.delete_doc(r["id"])
            meta.delete(r["id"])
        await update.message.reply_text(
            f"✅ 노이즈 {len(noisy)}건 / 청크 {n_chunks}개 제거 완료"
        )
        return
    preview = "\n".join(
        f"  • {r['id']}  {_clean_text(r.get('title') or r.get('source') or '')[:55]}"
        for r in noisy[:15]
    )
    more = f"\n... 외 {len(noisy)-15}건" if len(noisy) > 15 else ""
    await update.message.reply_text(
        f"노이즈 후보 {len(noisy)}건 (text 타입, 본문 짧음):\n{preview}{more}\n\n"
        f"전부 삭제하려면: /cleanup confirm"
    )


async def cmd_dedupe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    groups = meta.find_duplicates()
    if not groups:
        await update.message.reply_text("중복 없음 ✨")
        return
    args = [a.lower() for a in (ctx.args or [])]
    if "confirm" in args:
        deleted = 0
        chunks_removed = 0
        for g in groups:
            keeper = max(g, key=lambda d: len(d.get("summary") or ""))
            for d in g:
                if d["id"] == keeper["id"]:
                    continue
                chunks_removed += vector.delete_doc(d["id"])
                meta.delete(d["id"])
                deleted += 1
        await update.message.reply_text(
            f"✅ 중복 {deleted}건 / 청크 {chunks_removed}개 제거 완료\n"
            f"(각 그룹에서 본문이 가장 긴 것 1개만 유지)"
        )
        return
    lines: list[str] = []
    total_dups = 0
    for g in groups[:8]:
        total_dups += len(g) - 1
        title = _clean_text(g[0].get("title") or g[0].get("source") or "")[:55]
        lines.append(f"\n중복 {len(g)}건 — {title}")
        for d in g:
            chars = len(d.get("summary") or "")
            lines.append(f"  • {d['id']}  [{d['type']}]  {chars}자")
    more = f"\n\n... 외 {len(groups)-8}그룹" if len(groups) > 8 else ""
    total = sum(len(g) - 1 for g in groups)
    await update.message.reply_text(
        f"중복 {len(groups)}그룹 / 삭제 후보 {total}건\n"
        + "".join(lines) + more +
        f"\n\n각 그룹에서 본문 가장 긴 것 1개만 남기고 삭제: /dedupe confirm"
    )


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Locate a saved doc by title fragment, return source URL / Obsidian
    path / first-line summary so the user can jump back to the original."""
    if not _is_owner(update):
        return
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("사용법: /find <제목 일부>")
        return
    matches = meta.search_broad(query, limit=30)
    if not matches:
        await update.message.reply_text(f"매칭 없음: '{query}'")
        return
    cap = 20
    header = f"🔍 '{query}' — {len(matches)}개 매칭"
    if len(matches) > cap:
        header += f" (상위 {cap}개 표시)"
    out = header
    LIMIT = 3800  # safety margin under Telegram 4096
    truncated = 0
    import json as _json
    for i, m in enumerate(matches[:cap]):
        title = _clean_text((m.get("title") or "(제목 없음)"))[:70]
        ingested = (m.get("ingested_at") or "")[:10]
        source = m.get("source") or ""
        obs = m.get("obsidian_path") or ""
        summary_full = _clean_text(m.get("summary") or "")
        summary = summary_full[:500]
        loc_bits = []
        if source.startswith(("http://", "https://")):
            loc_bits.append(f"📎 {source[:80]}")
        elif source:
            loc_bits.append(f"🆔 {source[:50]}")
        if obs:
            loc_bits.append(f"📁 {obs[:60]}")
        meta_bits = []
        meta_raw = m.get("metadata")
        if meta_raw:
            try:
                md = _json.loads(meta_raw)
            except Exception:
                md = {}
            if md.get("company"):
                meta_bits.append(f"🏢 {md['company']}")
            if md.get("tags"):
                meta_bits.append("🏷️ " + ", ".join(md["tags"][:5]))
            if md.get("report_date"):
                meta_bits.append(f"📅 {md['report_date']}")
        item = f"\n\n📄 {title} ({ingested})"
        if loc_bits:
            item += f"\n   {' · '.join(loc_bits)}"
        if meta_bits:
            item += f"\n   {' · '.join(meta_bits)}"
        if summary:
            item += f"\n   {summary}"
        if len(out) + len(item) > LIMIT:
            truncated = cap - i
            break
        out += item
    if truncated:
        out += f"\n\n…(나머지 {truncated}개는 길이 제한으로 생략. 키워드를 좁혀 다시 시도)"
    await update.message.reply_text(out, disable_web_page_preview=True)


def _failed_retry_all(chat_id: int) -> str:
    """Move every failed entry that has a saved retry_payload back into
    the auto-retry queue. Returns the user-facing summary message."""
    retried = 0
    kept: list[dict] = []
    for entry in _INGEST_FAILED:
        payload = entry.get("retry")
        if payload:
            payload = dict(payload)
            payload["attempts"] = 0
            payload["chat_id"] = chat_id
            _INGEST_RETRY_QUEUE.append(payload)
            retried += 1
        else:
            kept.append(entry)
    _INGEST_FAILED.clear()
    _INGEST_FAILED.extend(kept)
    _persist_retry_queue()
    _persist_failed_log()
    msg = f"🔁 retry queue로 {retried}건 재등록\n2분 간격으로 자동 처리됩니다."
    if kept:
        msg += f"\n\n♻️ retry 정보 없는 {len(kept)}건은 그대로 — 채널 스크롤로 직접 다시 보내주세요."
    return msg


def _failed_clear_all() -> str:
    n = len(_INGEST_FAILED)
    _INGEST_FAILED.clear()
    _persist_failed_log()
    return f"실패 목록 비웠음 ({n}건)"


async def cmd_failed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show recent ingest failures (errors + empty bodies). Persisted to
    disk so /failed survives bot restart. Inline buttons let the user
    retry / clear with one tap. /failed retry and /failed clear still
    work as text commands too."""
    if not _is_owner(update):
        return
    if ctx.args and ctx.args[0] == "clear":
        await update.message.reply_text(_failed_clear_all())
        return
    if ctx.args and ctx.args[0] == "retry":
        await update.message.reply_text(
            _failed_retry_all(update.effective_chat.id)
        )
        return
    if not _INGEST_FAILED:
        await update.message.reply_text("실패 / 빈본문 없음 ✨")
        return
    out = f"❌ 실패/빈본문 누적 {len(_INGEST_FAILED)}건 (최근순)"
    LIMIT = 3800
    truncated = 0
    recent = list(reversed(_INGEST_FAILED[-50:]))
    for i, r in enumerate(recent):
        ts = (r.get("ts", "")[:16]).replace("T", " ")
        title = _clean_text(r.get("title", "(unknown)"))[:90]
        status = r.get("status", "error")
        detail = _clean_text(r.get("detail", ""))[:120]
        icon = "❌" if status == "error" else "⚠️"
        item = f"\n\n{icon} {ts}\n   {title}"
        if detail and detail != title:
            item += f"\n   {detail}"
        if len(out) + len(item) > LIMIT:
            truncated = len(recent) - i
            break
        out += item
    if truncated:
        out += f"\n\n…(나머지 {truncated}개 생략)"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 일괄 재시도", callback_data="failed_retry"),
        InlineKeyboardButton("🗑 비우기", callback_data="failed_clear"),
    ]])
    await update.message.reply_text(
        out, disable_web_page_preview=True, reply_markup=keyboard,
    )


async def cmd_failed_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Single-token alias so the usage guide can render as a one-tap
    command (Telegram only treats `/word` as tappable; `/failed retry`
    needs the user to type 'retry' manually)."""
    if not _is_owner(update):
        return
    await update.message.reply_text(
        _failed_retry_all(update.effective_chat.id)
    )


async def cmd_failed_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """One-tap alias for /failed clear (see cmd_failed_retry)."""
    if not _is_owner(update):
        return
    await update.message.reply_text(_failed_clear_all())


async def on_callback_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the /failed inline-button taps."""
    q = update.callback_query
    if not q:
        return
    user = q.from_user
    if not user or user.id != config.TELEGRAM_OWNER_ID:
        await q.answer("권한 없음", show_alert=False)
        return
    await q.answer()  # dismiss the loading spinner
    chat_id = q.message.chat.id if q.message else config.TELEGRAM_OWNER_ID
    if q.data == "failed_retry":
        await q.edit_message_text(_failed_retry_all(chat_id))
    elif q.data == "failed_clear":
        await q.edit_message_text(_failed_clear_all())


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Items currently waiting in the auto-retry queue (503/timeout
    failures). They re-attempt every 2 minutes."""
    if not _is_owner(update):
        return
    if not _INGEST_RETRY_QUEUE:
        await update.message.reply_text("재시도 큐 비어있음 ✨")
        return
    out = f"🔁 재시도 큐 {len(_INGEST_RETRY_QUEUE)}건 (2분 간격 자동)"
    for item in _INGEST_RETRY_QUEUE[:25]:
        kind = item.get("kind", "?")
        title = item.get("file_name") or item.get("url") or "(unknown)"
        attempts = item.get("attempts", 0)
        out += f"\n• [{kind}] {title[:80]} (시도 {attempts}회)"
    if len(_INGEST_RETRY_QUEUE) > 25:
        out += f"\n... 외 {len(_INGEST_RETRY_QUEUE) - 25}건"
    await update.message.reply_text(out, disable_web_page_preview=True)


async def cmd_forget_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("사용법: /forget_search <제목 일부>")
        return
    matches = meta.search_title(query, limit=20)
    if not matches:
        await update.message.reply_text(f"매칭 없음: '{query}'")
        return
    if len(matches) > 5:
        preview = "\n".join(
            f"  • {m['id']}  [{m['type']}]  {_clean_text(m['title'])[:60]}"
            for m in matches[:8]
        )
        await update.message.reply_text(
            f"⚠️ {len(matches)}개 매칭 — 너무 많아서 자동 삭제 안 함.\n"
            f"더 구체적인 검색어로 다시 시도하거나 /forget <id>로 직접:\n{preview}"
        )
        return
    forgotten = []
    for m in matches:
        n = vector.delete_doc(m["id"])
        meta.delete(m["id"])
        forgotten.append(f"  ✅ {_clean_text(m['title'])[:60]} ({n} chunks)")
    await update.message.reply_text(
        f"삭제 완료 · {len(forgotten)}건\n" + "\n".join(forgotten)
    )


async def cmd_deep(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text("사용법: /deep <질문>")
        return
    await _run_agent(update, ctx, text, deep=True)


async def cmd_forget_qna(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Drop one Q&A row from the archive by id (visible on the
    dashboard's q-{id}.html detail page). Triggers an immediate
    dashboard regenerate so the card disappears in the next refresh."""
    if not _is_owner(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("사용법: /forget_qna <id>")
        return
    qid = int(ctx.args[0])
    n = qna.delete(qid)
    try:
        from .dashboard import regenerate as dashboard_regen
        dashboard_regen.regenerate()
    except Exception:
        log.exception("dashboard regen after qna delete failed")
    await update.message.reply_text(
        f"{'✅ 삭제됨' if n else '⚠️ 매칭 없음'} · qna #{qid}"
    )


async def cmd_forget_qna_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bulk-drop Q&A rows whose question or answer contains the
    keyword. Same regen hook so the dashboard updates immediately."""
    if not _is_owner(update):
        return
    keyword = " ".join(ctx.args).strip()
    if not keyword:
        await update.message.reply_text("사용법: /forget_qna_search <키워드>")
        return
    n = qna.delete_search(keyword)
    try:
        from .dashboard import regenerate as dashboard_regen
        dashboard_regen.regenerate()
    except Exception:
        log.exception("dashboard regen after qna delete_search failed")
    await update.message.reply_text(f"✅ Q&A {n}건 삭제 · 키워드 '{keyword}'")


async def cmd_forget_search_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bulk forget — bypass /forget_search's 5-match safety so junk
    cleanup (bot meta-output, accidental ingests) is one tap. Typing
    the long command name acts as the confirmation."""
    if not _is_owner(update):
        return
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("사용법: /forget_search_all <키워드>")
        return
    matches = meta.search_title(query, limit=500)
    if not matches:
        await update.message.reply_text(f"매칭 없음: '{query}'")
        return
    forgotten = 0
    chunks_total = 0
    for m in matches:
        chunks_total += vector.delete_doc(m["id"])
        meta.delete(m["id"])
        forgotten += 1
    await update.message.reply_text(
        f"✅ 일괄 삭제 · {forgotten}건 / 청크 {chunks_total}개 제거"
    )


# Single-token aliases so the usage guide can render destructive
# operations as one-tap; the original /dedupe and /cleanup still need
# 'confirm' typed manually as a guard, so these wrappers replicate
# that behavior with the arg pre-supplied.
async def cmd_dedupe_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    ctx.args = ["confirm"]
    await cmd_dedupe(update, ctx)


async def cmd_cleanup_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    ctx.args = ["confirm"]
    await cmd_cleanup(update, ctx)


# Single-token slash aliases for the agent's tools so the usage guide
# can render them as one-tap commands. Each one rephrases the user's
# arg into a sentence the routing layer reliably maps to the intended
# tool — keeps the agent's rich formatting (sources, recency, emoji
# suffix) instead of bypassing it with a raw tool call.
async def cmd_search_my_brain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /search_my_brain <검색어>")
        return
    await _run_agent(update, ctx,
                     f"내 저장 자료에서 '{q}' 찾아서 정리해줘", deep=False)


async def cmd_compare_papers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /compare_papers <주제>")
        return
    await _run_agent(update, ctx,
                     f"내 저장 자료에서 '{q}' 관련 다수 문서를 통합·비교해서 정리해줘",
                     deep=False)


async def cmd_search_papers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /search_papers <검색어>")
        return
    await _run_agent(update, ctx,
                     f"외부 학술DB에서 '{q}' 관련 최신 논문 찾아줘", deep=False)


async def cmd_web_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /web_search <검색어>")
        return
    await _run_agent(update, ctx,
                     f"'{q}' 웹에서 검색해줘", deep=False)


async def cmd_ingest_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    url = " ".join(ctx.args).strip()
    if not url:
        await update.message.reply_text("사용법: /ingest_url <URL>")
        return
    await _run_agent(update, ctx, f"이 URL 학습해줘: {url}", deep=False)


async def cmd_recent_docs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    n = " ".join(ctx.args).strip() or "10"
    await _run_agent(update, ctx,
                     f"최근 학습한 문서 {n}개 알려줘", deep=False)


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Wipe rolling chat memory for this chat. Useful when topic shifts
    and stale context is hurting answer quality."""
    if not _is_owner(update):
        return
    chat_id = update.effective_chat.id
    n = len(_HISTORY.get(chat_id, []))
    _HISTORY.pop(chat_id, None)
    _persist_chat_history()
    await update.message.reply_text(f"대화 메모리 초기화 ({n} 메시지 비움)")


_OVERLOAD_MARKERS = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "high demand", "overloaded")
_MAX_RETRY_ATTEMPTS = 5
_RETRY_INTERVAL_SECONDS = 90
_RETRY_QUEUE: list[dict] = []


async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return
    if config.TELEGRAM_CHANNEL_ID and str(msg.chat.id) != config.TELEGRAM_CHANNEL_ID:
        return
    await _ingest_message(msg, ctx, notify_chat_id=config.TELEGRAM_OWNER_ID)


def _is_overload(exc: BaseException) -> bool:
    s = f"{type(exc).__name__} {exc}"
    return any(m in s for m in _OVERLOAD_MARKERS)


def _format_sources_with_url(titles: list[str], cap: int | None = None) -> str:
    """Look up each cited doc title in meta and append the source URL
    if it is an http(s) link, so the user can click straight from the
    bot reply to the original article. By default lists every cited
    source — the chunked Telegram send handles oversized blocks."""
    formatted: list[str] = []
    items = titles if cap is None else titles[:cap]
    for title in items:
        try:
            matches = meta.search_title(title, limit=1)
        except Exception:
            matches = []
        url = ""
        if matches:
            src = matches[0].get("source") or ""
            if src.startswith(("http://", "https://")):
                url = src
        if url:
            formatted.append(f"{title} → {url}")
        else:
            formatted.append(title)
    return "\n  • " + "\n  • ".join(formatted) if formatted else ""


# Telegram caps a single message at 4096 chars. Long brain answers
# (compare_papers can produce 3000+ chars body + 2000 chars of sources)
# get silently dropped by the API if we pack everything into one send.
# Chunk on paragraph boundaries so each piece reads naturally.
_TG_CHUNK_LIMIT = 3900


def _chunk_for_telegram(text: str, limit: int = _TG_CHUNK_LIMIT) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Prefer paragraph boundary, then sentence, then last newline,
        # then a hard cut as a last resort.
        slice_at = remaining.rfind("\n\n", 0, limit)
        if slice_at < int(limit * 0.5):
            slice_at = remaining.rfind("\n", 0, limit)
        if slice_at < int(limit * 0.5):
            slice_at = remaining.rfind(". ", 0, limit)
        if slice_at < int(limit * 0.5):
            slice_at = limit
        parts.append(remaining[:slice_at].rstrip())
        remaining = remaining[slice_at:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


async def _send_chunked(send, text: str) -> None:
    for piece in _chunk_for_telegram(text):
        await send(piece)


_CITATION_LINE_RE = re.compile(r"\(사용 자료 시점:\s*([^)]+)\)")


def _annotate_learn_date(body: str, sources: list[str]) -> str:
    """Add '· 학습: YYYY.MM' to the agent's '(사용 자료 시점: …)' line.

    The agent's date range reflects publication dates pulled from
    source bodies — useful for brokerage reports where the writing
    time matters, but confusing when the user wonders why a recently
    ingested archive shows old dates. Append the latest ingest month
    among cited sources so both axes are visible at a glance."""
    if not sources or not _CITATION_LINE_RE.search(body):
        return body
    latest = ""
    for title in sources[:20]:
        try:
            matches = meta.search_title(title, limit=1)
        except Exception:
            matches = []
        if not matches:
            continue
        d = (matches[0].get("ingested_at") or "")[:7]  # YYYY-MM
        if d and d > latest:
            latest = d
    if not latest:
        return body
    learn_mark = latest.replace("-", ".")  # YYYY.MM
    return _CITATION_LINE_RE.sub(
        lambda m: f"(사용 자료 시점: {m.group(1)} · 학습: {learn_mark})",
        body, count=1,
    )


async def _send_agent_reply(send, result, inherited: bool = False):
    raw, mermaid_blocks = _extract_mermaid(result["text"])
    body = _strip_markdown(raw)
    body = _annotate_learn_date(body, result.get("sources") or [])
    suffix_lines = []
    if result.get("warning"):
        suffix_lines.append(result["warning"])
    if result.get("sources"):
        header = "📚 출처 (이전 자료 재사용):" if inherited else "📚 출처:"
        suffix_lines.append(header + _format_sources_with_url(result["sources"]))
    if result.get("tool_calls"):
        suffix_lines.append(_format_tool_calls(result["tool_calls"]))
    await _send_chunked(send, body)
    if suffix_lines:
        await _send_chunked(send, "\n".join(suffix_lines))
    return body, mermaid_blocks


async def _retry_pending(ctx: ContextTypes.DEFAULT_TYPE):
    if not _RETRY_QUEUE:
        return
    item = _RETRY_QUEUE.pop(0)
    try:
        result = await agent.run(item["text"], deep=item["deep"])
    except Exception as e:
        item["attempts"] += 1
        if item["attempts"] >= _MAX_RETRY_ATTEMPTS or not _is_overload(e):
            await ctx.bot.send_message(
                item["chat_id"],
                f"⚠️ 재시도 포기 — {_explain_error(e)}\n원래 질문을 다시 보내주세요.",
            )
            return
        log.info("queued retry %d/%d for chat %s",
                 item["attempts"], _MAX_RETRY_ATTEMPTS, item["chat_id"])
        _RETRY_QUEUE.append(item)
        return
    chat_id = item["chat_id"]

    async def _send(text):
        await ctx.bot.send_message(chat_id, f"⏰ 재시도 성공\n\n{text}")

    _, mermaid_blocks = await _send_agent_reply(_send, result)
    for code in mermaid_blocks:
        try:
            png = await _render_mermaid_png(code)
            await ctx.bot.send_photo(chat_id, photo=png, caption="🧩 다이어그램")
        except Exception:
            pass




async def on_private(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    msg = update.message
    if not msg:
        return

    # Attachments — always ingest, never reach agent.
    if msg.document or msg.photo or msg.voice or msg.audio:
        await _ingest_message(msg, ctx, notify_chat_id=msg.chat.id)
        return

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    # Treat anything with a URL or 200+ chars of body as a "save this"
    # signal — ingest first, no agent prompt asking '저장하시겠습니까?'.
    # Short queries fall through to the agent for a normal answer.
    urls, plain = _collect_message_urls(msg)
    if urls or len(plain) >= 200:
        await _ingest_message(msg, ctx, notify_chat_id=msg.chat.id)
        return

    await _run_agent(update, ctx, text, deep=False)


async def _run_agent(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                     text: str, deep: bool) -> None:
    await _typing(update, ctx)
    chat_id = update.effective_chat.id
    history = list(_HISTORY.get(chat_id, []))
    # Sustained "typing..." indicator until agent returns. Background
    # task fires the chat_action every 4s so the user sees the bot is
    # still working through compare_papers/Pro synthesis.
    typing_task = asyncio.create_task(_sustained_typing(update, ctx))
    try:
        try:
            result = await agent.run(text, deep=deep, history=history)
        except Exception as e:
            if _is_overload(e):
                _RETRY_QUEUE.append({
                    "chat_id": update.effective_chat.id,
                    "text": text,
                    "deep": deep,
                    "attempts": 0,
                })
                await update.message.reply_text(
                    "⏳ Gemini 일시 과부하 — 자동으로 재시도 중입니다 (최대 약 7~8분).\n"
                    "별도로 다시 보내실 필요 없어요."
                )
                return
            log.exception("agent failed")
            await update.message.reply_text(f"⚠️ {_explain_error(e)}")
            return
    finally:
        typing_task.cancel()
    # If the agent answered from memory without calling any tool this
    # turn, fall back to the previous turn's citations so the user
    # always sees an 출처 block. New tool results take precedence when
    # present.
    inherited = False
    if not result.get("sources"):
        prev_sources, prev_tools = _last_model_citations(chat_id)
        if prev_sources:
            result["sources"] = prev_sources
            result["tool_calls"] = (result.get("tool_calls") or []) + prev_tools
            inherited = True
    # Use the shared sender: applies _annotate_learn_date, lists all
    # sources (no 15-cap), and chunks long outputs across multiple
    # Telegram messages so the suffix isn't silently dropped.
    body, mermaid_blocks = await _send_agent_reply(
        update.message.reply_text, result, inherited=inherited,
    )
    _record_turn(chat_id, "user", text)
    _record_turn(
        chat_id, "model", body,
        sources=result.get("sources") or [],
        tools=result.get("tool_calls") or [],
    )
    qna.record(
        chat_id=chat_id,
        question=text,
        answer=body,
        sources=result.get("sources") or [],
        tools=result.get("tool_calls") or [],
        model=result.get("model"),
        warning=result.get("warning"),
    )
    try:
        from .dashboard import regenerate as dashboard_regen
        dashboard_regen.regenerate()
    except Exception:
        log.exception("dashboard regen failed")
    for code in mermaid_blocks:
        try:
            png = await _render_mermaid_png(code)
            await update.message.reply_photo(
                photo=png,
                caption="🧩 다이어그램",
            )
        except Exception as e:
            log.warning("mermaid render failed: %s", e)
            await update.message.reply_text(
                f"(다이어그램 렌더 실패: {_explain_error(e)})\n\n{code[:500]}"
            )


def _explain_error(e: BaseException, max_len: int = 280) -> str:
    """Pretty-print an exception with type + first line of message."""
    cause = e
    while getattr(cause, "__cause__", None):
        cause = cause.__cause__
    msg = str(cause).strip().splitlines()[0] if str(cause).strip() else "(no message)"
    return f"{type(cause).__name__}: {msg}"[:max_len]


_INGEST_TIMEOUT_SEC = 900  # 15 minutes per message — large PDFs with OCR can take this long


async def _ingest_message(msg, ctx: ContextTypes.DEFAULT_TYPE, notify_chat_id: int):
    """Cap concurrent ingests via semaphore + per-message timeout.
    Up to 3 messages run in parallel; the 4th waits. 15 min timeout
    prevents one stuck PDF from hanging the whole bot."""
    async with _INGEST_SEM:
        try:
            return await asyncio.wait_for(
                _ingest_message_locked(msg, ctx, notify_chat_id),
                timeout=_INGEST_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            label = "(unknown)"
            if msg.document and msg.document.file_name:
                label = msg.document.file_name
            elif msg.text:
                label = msg.text.strip().splitlines()[0][:80]
            log.warning("ingest timeout (%ds) for %s", _INGEST_TIMEOUT_SEC, label)
            _record_failure("timeout", label, f"ingest exceeded {_INGEST_TIMEOUT_SEC}s")
            try:
                await ctx.bot.send_message(
                    notify_chat_id,
                    f"⚠️ ingest timeout (15분 초과): {label[:60]}\n"
                    "자료가 너무 크거나 OCR 처리 지연. 같은 자료 다시 보내면 재시도됩니다.",
                )
            except Exception:
                log.exception("timeout notify failed")
            return None


async def _ingest_doc_attachment(msg, ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    file = await ctx.bot.get_file(msg.document.file_id)
    dest = Path(config.DATA_DIR) / "files" / msg.document.file_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    await file.download_to_drive(custom_path=dest)
    label = f"tg-doc:{msg.document.file_unique_id}:{msg.document.file_name}"
    suffix = dest.suffix.lower()
    if suffix == ".pdf":
        return await pipeline.ingest_pdf(dest, label)
    if suffix == ".pptx":
        return await pipeline.ingest_pptx(dest, label)
    if suffix == ".docx":
        return await pipeline.ingest_docx(dest, label)
    if suffix == ".xlsx":
        return await pipeline.ingest_xlsx(dest, label)
    if suffix in _AUDIO_SUFFIX_MIME:
        return await pipeline.ingest_audio(
            dest.read_bytes(), label,
            caption=msg.caption or "",
            mime_type=_AUDIO_SUFFIX_MIME[suffix],
        )
    if suffix in {".ppt", ".doc", ".xls"}:
        fname = (msg.document.file_name or dest.name)
        return {
            "status": "error",
            "title": fname,
            "error": (
                f"{fname} — {suffix} 구버전 포맷 지원 안 됨. "
                f"{suffix}x로 변환해서 다시 보내주세요."
            ),
        }
    content = dest.read_text(encoding="utf-8", errors="ignore")
    return await pipeline.ingest_text(content, label)


_AUDIO_SUFFIX_MIME = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}


async def _ingest_voice_attachment(msg, ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """Telegram voice note (msg.voice) — OGG/Opus."""
    import io
    voice = msg.voice
    file = await ctx.bot.get_file(voice.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    label = f"tg-voice:{voice.file_unique_id}"
    return await pipeline.ingest_audio(
        bio.getvalue(), label, caption=msg.caption or "",
        mime_type=voice.mime_type or "audio/ogg",
    )


async def _ingest_audio_attachment(msg, ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """Telegram audio (msg.audio) — uploaded music/audio file."""
    import io
    audio = msg.audio
    file = await ctx.bot.get_file(audio.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    label = f"tg-audio:{audio.file_unique_id}"
    title_hint = (audio.title or audio.file_name or "").strip()
    caption_part = (msg.caption or "").strip()
    full_caption = "\n".join(p for p in [title_hint, caption_part] if p)
    return await pipeline.ingest_audio(
        bio.getvalue(), label, caption=full_caption,
        mime_type=audio.mime_type or "audio/mpeg",
    )


async def _ingest_photo_attachment(msg, ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """Standalone photo (screenshot, table capture). Caption ≥80 chars
    skips OCR; otherwise Gemini Vision extracts text."""
    import io
    photo = msg.photo[-1]  # largest size
    file = await ctx.bot.get_file(photo.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    label = f"tg-photo:{photo.file_unique_id}"
    return await pipeline.ingest_image(
        bio.getvalue(), label, caption=msg.caption or "",
        mime_type="image/jpeg",
    )


def _is_retryable(e: BaseException) -> bool:
    s = f"{type(e).__name__} {e}"
    return any(m in s for m in (
        *_OVERLOAD_MARKERS,
        "Timeout", "ReadError", "ConnectError", "RemoteProtocolError",
    ))


async def _ingest_message_locked(msg, ctx: ContextTypes.DEFAULT_TYPE, notify_chat_id: int):
    text = msg.text or msg.caption or ""
    results = []

    if msg.document:
        try:
            results.append(await _ingest_doc_attachment(msg, ctx))
        except Exception as e:
            log.exception("file ingest failed")
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    "kind": "doc",
                    "file_id": msg.document.file_id,
                    "file_unique_id": msg.document.file_unique_id,
                    "file_name": msg.document.file_name,
                    "chat_id": notify_chat_id,
                    "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued",
                                "title": msg.document.file_name})
            else:
                results.append({"status": "error", "error": _explain_error(e)})

        # Korean analyst commentary often ships as a long caption above
        # the attached IR PDF/XLSX. Keep it as a separate doc so search
        # can match the analysis even when the PDF body is in English.
        cap = (msg.caption or "").strip()
        if len(cap) >= 200:
            cap_retry = {
                "kind": "text",
                "text": cap,
                "label": f"tg-doc-caption:{msg.message_id}",
            }
            try:
                rcap = await pipeline.ingest_text(
                    cap, f"tg-doc-caption:{msg.message_id}",
                )
                if rcap.get("status") in ("empty", "error"):
                    rcap["retry_payload"] = cap_retry
                results.append(rcap)
            except Exception as e:
                log.exception("doc caption ingest failed")
                if _is_retryable(e):
                    _INGEST_RETRY_QUEUE.append({
                        **cap_retry, "chat_id": notify_chat_id, "attempts": 0,
                    })
                    _persist_retry_queue()
                    results.append({"status": "queued", "title": cap[:60]})
                else:
                    results.append({"status": "error",
                                    "error": _explain_error(e),
                                    "retry_payload": cap_retry})

    if msg.photo:
        photo = msg.photo[-1]
        photo_retry = {
            "kind": "photo",
            "file_id": photo.file_id,
            "file_unique_id": photo.file_unique_id,
            "caption": msg.caption or "",
        }
        try:
            r = await _ingest_photo_attachment(msg, ctx)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = photo_retry
            results.append(r)
        except Exception as e:
            log.exception("photo ingest failed")
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    **photo_retry, "chat_id": notify_chat_id, "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued", "title": "photo"})
            else:
                results.append({"status": "error",
                                "error": _explain_error(e),
                                "retry_payload": photo_retry})

    if msg.voice:
        voice = msg.voice
        voice_retry = {
            "kind": "voice",
            "file_id": voice.file_id,
            "file_unique_id": voice.file_unique_id,
            "mime_type": voice.mime_type or "audio/ogg",
            "caption": msg.caption or "",
        }
        try:
            r = await _ingest_voice_attachment(msg, ctx)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = voice_retry
            results.append(r)
        except Exception as e:
            log.exception("voice ingest failed")
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    **voice_retry, "chat_id": notify_chat_id, "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued", "title": "voice"})
            else:
                results.append({"status": "error",
                                "error": _explain_error(e),
                                "retry_payload": voice_retry})

    if msg.audio:
        audio = msg.audio
        audio_retry = {
            "kind": "audio",
            "file_id": audio.file_id,
            "file_unique_id": audio.file_unique_id,
            "file_name": audio.file_name or "",
            "title": audio.title or "",
            "mime_type": audio.mime_type or "audio/mpeg",
            "caption": msg.caption or "",
        }
        try:
            r = await _ingest_audio_attachment(msg, ctx)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = audio_retry
            results.append(r)
        except Exception as e:
            log.exception("audio ingest failed")
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    **audio_retry, "chat_id": notify_chat_id, "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued",
                                "title": audio.file_name or "audio"})
            else:
                results.append({"status": "error",
                                "error": _explain_error(e),
                                "retry_payload": audio_retry})

    urls, plain = _collect_message_urls(msg)
    for url in urls:
        url_retry = {"kind": "url", "url": url}
        try:
            r = await pipeline.ingest_url(url)
            r.setdefault("source", url)
            r.setdefault("title", url)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = url_retry
            results.append(r)
        except Exception as e:
            log.exception("url ingest failed: %s", url)
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    **url_retry, "chat_id": notify_chat_id, "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued", "title": url})
            else:
                results.append({"status": "error",
                                "error": _explain_error(e),
                                "source": url, "retry_payload": url_retry})

    if (plain and not msg.document and not msg.photo
            and not msg.voice and not msg.audio and len(plain) >= 80):
        text_retry = {
            "kind": "text",
            "text": plain,
            "label": f"tg-msg:{msg.message_id}",
        }
        try:
            r = await pipeline.ingest_text(plain, f"tg-msg:{msg.message_id}")
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = text_retry
            results.append(r)
        except Exception as e:
            log.exception("text ingest failed")
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    **text_retry, "chat_id": notify_chat_id, "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued", "title": plain[:60]})
            else:
                results.append({"status": "error",
                                "error": _explain_error(e),
                                "retry_payload": text_retry})

    if not results:
        return
    summary = _format_results(results)
    try:
        await ctx.bot.send_message(notify_chat_id, summary)
    except Exception:
        log.exception("notify failed")


def _record_failure(status: str, title: str, detail: str = "",
                    retry_payload: dict | None = None) -> None:
    """Append to in-memory failure log + persist so /failed survives
    restart. retry_payload (kind/file_id/url/text/...) lets
    /failed retry re-enqueue the item automatically."""
    entry: dict = {
        "status": status,
        "title": (title or "(unknown)")[:140],
        "detail": (detail or "")[:200],
        "ts": datetime.utcnow().isoformat(),
    }
    if retry_payload:
        entry["retry"] = retry_payload
    _INGEST_FAILED.append(entry)
    if len(_INGEST_FAILED) > _FAILED_MAX:
        del _INGEST_FAILED[0]
    _persist_failed_log()


def _empty_url_guidance(source: str) -> str:
    """Suggest a manual recovery path for URLs the bot can't crawl
    (auth wall, IP block, JS-only, shortener that points at a
    restricted PDF). Shown right under the ⚠️ 본문 비어있음 line."""
    s = (source or "").lower()
    if any(d in s for d in (
        "linkedin.com", "facebook.com", "story.kakao.com", "instagram.com",
    )):
        return (
            "🔒 인증 차단 사이트입니다.\n"
            "  • 글 열어 본문 복사 → 봇 DM에 붙여넣기 (200자+ 자동 학습)\n"
            "  • 또는 스크린샷 → 봇에 이미지로 (caption 없이) 보내기"
        )
    if any(d in s for d in (
        "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
        "economist.com", "nytimes.com", "washingtonpost.com", "barrons.com",
    )):
        return (
            "🔒 영문 paywall 사이트입니다.\n"
            "  • 본문 복사 → 봇 DM에 붙여넣기 (구독 중이면 가능)\n"
            "  • 또는 같은 뉴스 다루는 한국 매체 URL을 대신 전송\n"
            "  • 또는 스크린샷 → 이미지로 보내기 (OCR 자동)"
        )
    if "x.com" in s or "twitter.com" in s:
        return (
            "🔒 X(Twitter) 본문 추출 어려움.\n"
            "  • 트윗 본문 복사 → 봇 DM에 붙여넣기\n"
            "  • 또는 스크린샷 → 이미지로 보내기 (OCR 자동)"
        )
    if any(d in s for d in (
        "buly.kr", "vo.la", "zrr.kr", "bit.ly", "shinhansec.com",
        "kiwoom.com", "nh-securities.com",
    )):
        return (
            "🔒 단축 URL 또는 인증 필요한 호스트.\n"
            "  • 브라우저에서 URL 열어 PDF 다운로드 → 봇에 첨부\n"
            "  • 또는 본문 텍스트 복사 → 봇 DM에 붙여넣기"
        )
    return (
        "🔒 본문 추출 실패.\n"
        "  • URL 열어 본문 복사 → 봇 DM에 붙여넣기 (200자+)\n"
        "  • 또는 스크린샷 → 이미지로 보내기 (OCR 자동)"
    )


def _format_results(results: list[dict]) -> str:
    lines = []
    for r in results:
        s = r.get("status")
        if s == "ok":
            lines.append(f"✅ {r['title']}  ({r['type']}, {r['chunks']} chunks)")
        elif s == "duplicate":
            lines.append(f"♻️ 이미 있음: {r['title']}")
        elif s == "empty":
            title = r.get("title", "")
            src = r.get("source", "") or title
            # URL이 source에 있으면 그걸 우선 노출 (사용자가 어떤 자료인지
            # 즉시 알 수 있게). title이 'XX증권' 같은 짧은 페이지 제목이면
            # 정보가 부족하므로 URL을 같이 보여줌.
            if src.startswith(("http://", "https://")):
                shown = src
                if title and title != src and title not in src:
                    shown = f"{title}\n   {src}"
            else:
                shown = title or src
            _record_failure("empty", title or src, src,
                            retry_payload=r.get("retry_payload"))
            line = f"⚠️ 본문 비어있음: {shown}"
            guidance = _empty_url_guidance(src)
            if guidance:
                line += "\n" + guidance
            lines.append(line)
        elif s == "queued":
            lines.append(f"⏳ 재시도 대기 (자동): {r.get('title', '')}")
        else:
            label = r.get("title") or r.get("source", "")
            _record_failure("error", label, r.get("error", ""),
                            retry_payload=r.get("retry_payload"))
            lines.append(f"❌ {r.get('error', 'error')}")
    return "\n".join(lines)


async def _refresh_dashboard(ctx: ContextTypes.DEFAULT_TYPE):
    """Regenerate the static dashboard HTML on a tick so ingest-only
    activity (no Q&As happening) still shows up in the totals."""
    try:
        from .dashboard import regenerate as dashboard_regen
        dashboard_regen.regenerate()
    except Exception:
        log.exception("scheduled dashboard refresh failed")


async def _retry_pending_ingest(ctx: ContextTypes.DEFAULT_TYPE):
    """Drain one queued ingest per tick, sharing the same semaphore as live
    ingests so total concurrent ingests stays bounded."""
    if not _INGEST_RETRY_QUEUE:
        return
    item = _INGEST_RETRY_QUEUE.pop(0)
    _persist_retry_queue()
    chat_id = item["chat_id"]
    title = item.get("file_name") or item.get("url") or item.get("text", "")[:60] or "(unknown)"
    async with _INGEST_SEM:
        try:
            kind = item["kind"]
            if kind == "doc":
                file = await ctx.bot.get_file(item["file_id"])
                dest = Path(config.DATA_DIR) / "files" / item["file_name"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                await file.download_to_drive(custom_path=dest)
                label = f"tg-doc:{item['file_unique_id']}:{item['file_name']}"
                suffix = dest.suffix.lower()
                if suffix == ".pdf":
                    r = await pipeline.ingest_pdf(dest, label)
                elif suffix == ".pptx":
                    r = await pipeline.ingest_pptx(dest, label)
                elif suffix == ".docx":
                    r = await pipeline.ingest_docx(dest, label)
                elif suffix == ".xlsx":
                    r = await pipeline.ingest_xlsx(dest, label)
                elif suffix in _AUDIO_SUFFIX_MIME:
                    r = await pipeline.ingest_audio(
                        dest.read_bytes(), label,
                        mime_type=_AUDIO_SUFFIX_MIME[suffix],
                    )
                else:
                    content = dest.read_text(encoding="utf-8", errors="ignore")
                    r = await pipeline.ingest_text(content, label)
            elif kind == "url":
                r = await pipeline.ingest_url(item["url"])
            elif kind == "photo":
                import io as _io
                file = await ctx.bot.get_file(item["file_id"])
                bio = _io.BytesIO()
                await file.download_to_memory(out=bio)
                label = f"tg-photo:{item['file_unique_id']}"
                r = await pipeline.ingest_image(
                    bio.getvalue(), label,
                    caption=item.get("caption", ""),
                    mime_type="image/jpeg",
                )
            elif kind in ("voice", "audio"):
                import io as _io
                file = await ctx.bot.get_file(item["file_id"])
                bio = _io.BytesIO()
                await file.download_to_memory(out=bio)
                if kind == "voice":
                    label = f"tg-voice:{item['file_unique_id']}"
                    cap = item.get("caption", "")
                else:
                    label = f"tg-audio:{item['file_unique_id']}"
                    parts = [item.get("title", ""), item.get("caption", "")]
                    cap = "\n".join(p for p in parts if p)
                r = await pipeline.ingest_audio(
                    bio.getvalue(), label, caption=cap,
                    mime_type=item.get("mime_type", "audio/ogg"),
                )
            elif kind == "text":
                r = await pipeline.ingest_text(
                    item["text"], item.get("label", "tg-msg-retry"),
                )
            else:
                return
        except Exception as e:
            item["attempts"] += 1
            if item["attempts"] >= _MAX_RETRY_ATTEMPTS or not _is_retryable(e):
                # Persist to /failed so the user can /failed retry later.
                payload = {k: v for k, v in item.items() if k != "chat_id"}
                _record_failure(
                    "error", title[:140], _explain_error(e),
                    retry_payload=payload,
                )
                await ctx.bot.send_message(
                    chat_id,
                    f"⚠️ ingest 재시도 포기 — {title[:80]}\n{_explain_error(e)}\n"
                    "/failed_retry 로 다시 시도할 수 있습니다.",
                )
                return
            log.info("ingest retry %d/%d: %s",
                     item["attempts"], _MAX_RETRY_ATTEMPTS, title[:80])
            _INGEST_RETRY_QUEUE.append(item)
            _persist_retry_queue()
            return
    summary = _format_results([r])
    await ctx.bot.send_message(chat_id, f"⏰ ingest 재시도\n{summary}")


def main():
    meta.init()
    obsidian.init()
    request = HTTPXRequest(
        connect_timeout=15.0,
        read_timeout=180.0,
        write_timeout=180.0,
        pool_timeout=15.0,
        connection_pool_size=8,
    )
    builder = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .request(request)
        .get_updates_request(request)
    )
    if config.TELEGRAM_BASE_URL:
        base = config.TELEGRAM_BASE_URL.rstrip("/")
        # base ends with "/bot"; file URL is "/file/bot" on the same host
        file_base = base.replace("/bot", "/file/bot")
        builder = (
            builder.base_url(base)
            .base_file_url(file_base)
            .local_mode(True)
        )
        log.info("Telegram local mode at %s", base)
    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("forget_search", cmd_forget_search))
    app.add_handler(CommandHandler("forget_search_all", cmd_forget_search_all))
    app.add_handler(CommandHandler("forget_qna", cmd_forget_qna))
    app.add_handler(CommandHandler("forget_qna_search", cmd_forget_qna_search))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("failed", cmd_failed))
    app.add_handler(CommandHandler("failed_retry", cmd_failed_retry))
    app.add_handler(CommandHandler("failed_clear", cmd_failed_clear))
    app.add_handler(CallbackQueryHandler(
        on_callback_query, pattern=r"^failed_(retry|clear)$"
    ))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("cleanup_confirm", cmd_cleanup_confirm))
    app.add_handler(CommandHandler("dedupe", cmd_dedupe))
    app.add_handler(CommandHandler("dedupe_confirm", cmd_dedupe_confirm))
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CommandHandler("search_my_brain", cmd_search_my_brain))
    app.add_handler(CommandHandler("compare_papers", cmd_compare_papers))
    app.add_handler(CommandHandler("search_papers", cmd_search_papers))
    app.add_handler(CommandHandler("web_search", cmd_web_search))
    app.add_handler(CommandHandler("ingest_url", cmd_ingest_url))
    app.add_handler(CommandHandler("recent_docs", cmd_recent_docs))
    app.add_handler(CommandHandler("reset", cmd_reset))

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND, on_private
    ))

    if app.job_queue:
        app.job_queue.run_repeating(
            _retry_pending,
            interval=_RETRY_INTERVAL_SECONDS,
            first=30,
            name="retry_pending",
        )
        app.job_queue.run_repeating(
            _retry_pending_ingest,
            interval=120,
            first=60,
            name="retry_pending_ingest",
        )
        app.job_queue.run_repeating(
            _refresh_dashboard,
            interval=60,
            first=20,
            name="refresh_dashboard",
        )

    _load_persisted_state()
    vector.warm_bm25_cache()  # background scan; first query stays fast
    log.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
