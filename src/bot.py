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
from .store import meta, vector, obsidian
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


def _persist_retry_queue() -> None:
    import json
    try:
        _RETRY_QUEUE_PATH.write_text(
            json.dumps(_INGEST_RETRY_QUEUE, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.exception("retry queue persist failed")


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
    "recent_documents": "🧠",
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


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await update.message.reply_text(
        "Second Brain 봇이에요.\n"
        "• 채널에 링크/PDF/유튜브/텍스트 → 자동 수집·요약·Obsidian 동기화\n"
        "• 여기 DM에 자연어로 무엇이든 말씀하세요. 봇이 알아서 도구를 골라요:\n"
        "  - 저장된 자료에서 답하기\n"
        "  - arXiv/Semantic Scholar 검색\n"
        "  - URL을 학습/저장\n"
        "  - 최근 저장 목록\n"
        "• /deep <질문> - 어려운 질문은 Gemini Pro로\n"
        "• /find <제목 일부> - 답변 출처 원본 (URL/Obsidian) 찾기\n"
        "• /failed - 실패한 ingest 목록 (/failed_retry · /failed_clear)\n"
        "• /queue - 자동 재시도 대기 중인 항목\n"
        "• /stats /recent /forget <id>"
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
    # Rough cost — text/url ≈ ₩4, pdf/pptx/xlsx ≈ ₩6, audio ≈ ₩55, image ≈ ₩2.
    cost_table = {
        "text": 4, "url": 5, "pdf": 6, "paper": 6,
        "pptx": 6, "docx": 5, "xlsx": 5, "image": 2, "audio": 55,
    }
    cost_24h = sum(cost_table.get(t, 4) * 1 for t, _ in s["types"]) * (s["last_24h"] / max(s["total"], 1))
    out = (
        "📊 봇 사용 현황\n"
        f"\n총 문서: {s['total']}개  /  청크: {chunks}개"
        f"\n\n📥 ingest 속도"
        f"\n  • 24h: {s['last_24h']}건"
        f"\n  • 7d:  {s['last_7d']}건"
        f"\n  • 30d: {s['last_30d']}건"
        f"\n\n📂 type별 분포"
        f"\n  {types_line}"
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


def _format_sources_with_url(titles: list[str], cap: int = 15) -> str:
    """Look up each cited doc title in meta and append the source URL
    if it is an http(s) link, so the user can click straight from the
    bot reply to the original article. Limits to `cap` items."""
    formatted: list[str] = []
    for title in titles[:cap]:
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


async def _send_agent_reply(send, result):
    raw, mermaid_blocks = _extract_mermaid(result["text"])
    body = _strip_markdown(raw)
    suffix_lines = []
    if result.get("warning"):
        suffix_lines.append(result["warning"])
    if result.get("sources"):
        suffix_lines.append("📚 출처:" + _format_sources_with_url(result["sources"]))
    if result.get("tool_calls"):
        suffix_lines.append(_format_tool_calls(result["tool_calls"]))
    suffix = ("\n\n" + "\n".join(suffix_lines)) if suffix_lines else ""
    await send(f"{body}{suffix}")
    return mermaid_blocks


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

    mermaid_blocks = await _send_agent_reply(_send, result)
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
    try:
        result = await agent.run(text, deep=deep)
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
    raw, mermaid_blocks = _extract_mermaid(result["text"])
    body = _strip_markdown(raw)
    suffix_lines = []
    if result.get("warning"):
        suffix_lines.append(result["warning"])
    if result["sources"]:
        suffix_lines.append("📚 출처:" + _format_sources_with_url(result["sources"]))
    if result["tool_calls"]:
        suffix_lines.append(_format_tool_calls(result["tool_calls"]))
    suffix = ("\n\n" + "\n".join(suffix_lines)) if suffix_lines else ""
    await update.message.reply_text(f"{body}{suffix}")
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
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("failed", cmd_failed))
    app.add_handler(CommandHandler("failed_retry", cmd_failed_retry))
    app.add_handler(CommandHandler("failed_clear", cmd_failed_clear))
    app.add_handler(CallbackQueryHandler(
        on_callback_query, pattern=r"^failed_(retry|clear)$"
    ))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("dedupe", cmd_dedupe))
    app.add_handler(CommandHandler("deep", cmd_deep))

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

    _load_persisted_state()
    vector.warm_bm25_cache()  # background scan; first query stays fast
    log.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
