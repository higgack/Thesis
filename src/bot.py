import asyncio
import base64
import logging
import re
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)
from telegram.request import HTTPXRequest

# Allow up to 3 concurrent ingests. Each ingest internally parallelizes
# summary + chunk embedding (different Gemini endpoints), so 3 in-flight
# stays well under per-model RPM caps while ~3x'ing throughput on bulk
# drops. Drop to 1 if 429s start appearing.
_INGEST_SEM = asyncio.Semaphore(3)
_INGEST_RETRY_QUEUE: list[dict] = []

from . import config
from .store import meta, vector, obsidian
from .ingest import pipeline
from .agent import agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

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

    urls = [u for u in urls if not _INTERNAL_TG_RE.match(u)][:_MAX_URLS_PER_MSG]
    return urls, plain
_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)\n```", re.DOTALL)
_NUMBERED_SECTION_RE = re.compile(
    r"^(\s*)(\d+)\.\s+(.{3,100}?)[:：]?\s*$", re.MULTILINE
)
_SECTION_EMOJIS = ["📌", "🔹", "🔸", "⚙️", "🧪", "💡", "📊", "🎯", "⚡", "🔧"]
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
        "• /stats /recent /forget <id>"
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await update.message.reply_text(
        f"문서 {meta.count()}개 / 청크 {vector.chunk_count()}개"
    )


async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    items = meta.recent(10)
    if not items:
        await update.message.reply_text("아직 비어있어요.")
        return
    lines = [f"`{r['id']}` [{r['type']}] {r['title']}" for r in items]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
        f"  • `{r['id']}` {(r['title'] or r['source'])[:55]}"
        for r in noisy[:15]
    )
    more = f"\n... 외 {len(noisy)-15}건" if len(noisy) > 15 else ""
    await update.message.reply_text(
        f"노이즈 후보 {len(noisy)}건 (text 타입, 본문 짧음):\n{preview}{more}\n\n"
        f"전부 삭제하려면: `/cleanup confirm`",
        parse_mode="Markdown",
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
        title = (g[0].get("title") or g[0].get("source") or "")[:55]
        lines.append(f"\n중복 {len(g)}건 — {title}")
        for d in g:
            chars = len(d.get("summary") or "")
            lines.append(f"  • `{d['id']}` [{d['type']}] {chars}자")
    more = f"\n\n... 외 {len(groups)-8}그룹" if len(groups) > 8 else ""
    total = sum(len(g) - 1 for g in groups)
    await update.message.reply_text(
        f"중복 {len(groups)}그룹 / 삭제 후보 {total}건\n"
        + "".join(lines) + more +
        f"\n\n각 그룹에서 본문 가장 긴 것 1개만 남기고 삭제: `/dedupe confirm`",
        parse_mode="Markdown",
    )


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
            f"  • `{m['id']}` [{m['type']}] {m['title'][:60]}"
            for m in matches[:8]
        )
        await update.message.reply_text(
            f"⚠️ {len(matches)}개 매칭 — 너무 많아서 자동 삭제 안 함.\n"
            f"더 구체적인 검색어로 다시 시도하거나 `/forget <id>` 직접 사용:\n{preview}",
            parse_mode="Markdown",
        )
        return
    forgotten = []
    for m in matches:
        n = vector.delete_doc(m["id"])
        meta.delete(m["id"])
        forgotten.append(f"  ✅ {m['title'][:60]} ({n} chunks)")
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


async def _send_agent_reply(send, result):
    raw, mermaid_blocks = _extract_mermaid(result["text"])
    body = _strip_markdown(raw)
    suffix_lines = []
    if result.get("warning"):
        suffix_lines.append(result["warning"])
    if result.get("sources"):
        suffix_lines.append("📚 " + ", ".join(result["sources"][:5]))
    if result.get("tool_calls"):
        suffix_lines.append(f"🔧 {' → '.join(result['tool_calls'])}")
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

    if msg.document:
        await _ingest_message(msg, ctx, notify_chat_id=msg.chat.id)
        return

    text = msg.text or ""
    if not text.strip():
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
        suffix_lines.append("📚 " + ", ".join(result["sources"][:5]))
    if result["tool_calls"]:
        suffix_lines.append(f"🔧 {' → '.join(result['tool_calls'])}")
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


async def _ingest_message(msg, ctx: ContextTypes.DEFAULT_TYPE, notify_chat_id: int):
    """Cap concurrent ingests via semaphore. Up to 3 files may run in
    parallel; the 4th waits. Keeps Gemini below RPM caps while letting
    bulk file drops finish ~3x faster."""
    async with _INGEST_SEM:
        return await _ingest_message_locked(msg, ctx, notify_chat_id)


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
    if suffix in {".ppt", ".doc"}:
        return {"status": "error",
                "error": f"{suffix} (구버전 포맷)은 지원 안 됩니다. {suffix}x로 변환해서 다시 보내주세요."}
    content = dest.read_text(encoding="utf-8", errors="ignore")
    return await pipeline.ingest_text(content, label)


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
                results.append({"status": "queued",
                                "title": msg.document.file_name})
            else:
                results.append({"status": "error", "error": _explain_error(e)})

    if msg.photo:
        try:
            results.append(await _ingest_photo_attachment(msg, ctx))
        except Exception as e:
            log.exception("photo ingest failed")
            results.append({"status": "error", "error": _explain_error(e)})

    urls, plain = _collect_message_urls(msg)
    for url in urls:
        try:
            results.append(await pipeline.ingest_url(url))
        except Exception as e:
            log.exception("url ingest failed: %s", url)
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    "kind": "url",
                    "url": url,
                    "chat_id": notify_chat_id,
                    "attempts": 0,
                })
                results.append({"status": "queued", "title": url})
            else:
                results.append({"status": "error",
                                "error": _explain_error(e), "source": url})

    if plain and not msg.document and not msg.photo and len(plain) >= 80:
        try:
            results.append(await pipeline.ingest_text(plain, f"tg-msg:{msg.message_id}"))
        except Exception as e:
            log.exception("text ingest failed")
            results.append({"status": "error", "error": _explain_error(e)})

    if not results:
        return
    summary = _format_results(results)
    try:
        await ctx.bot.send_message(notify_chat_id, summary)
    except Exception:
        log.exception("notify failed")


def _format_results(results: list[dict]) -> str:
    lines = []
    for r in results:
        s = r.get("status")
        if s == "ok":
            lines.append(f"✅ {r['title']}  ({r['type']}, {r['chunks']} chunks)")
        elif s == "duplicate":
            lines.append(f"♻️ 이미 있음: {r['title']}")
        elif s == "empty":
            lines.append(f"⚠️ 본문 비어있음: {r.get('title', '')}")
        elif s == "queued":
            lines.append(f"⏳ 재시도 대기 (자동): {r.get('title', '')}")
        else:
            lines.append(f"❌ {r.get('error', 'error')}")
    return "\n".join(lines)


async def _retry_pending_ingest(ctx: ContextTypes.DEFAULT_TYPE):
    """Drain one queued ingest per tick, sharing the same semaphore as live
    ingests so total concurrent ingests stays bounded."""
    if not _INGEST_RETRY_QUEUE:
        return
    item = _INGEST_RETRY_QUEUE.pop(0)
    chat_id = item["chat_id"]
    title = item.get("file_name") or item.get("url") or "(unknown)"
    async with _INGEST_SEM:
        try:
            if item["kind"] == "doc":
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
                else:
                    content = dest.read_text(encoding="utf-8", errors="ignore")
                    r = await pipeline.ingest_text(content, label)
            elif item["kind"] == "url":
                r = await pipeline.ingest_url(item["url"])
            else:
                return
        except Exception as e:
            item["attempts"] += 1
            if item["attempts"] >= _MAX_RETRY_ATTEMPTS or not _is_retryable(e):
                await ctx.bot.send_message(
                    chat_id,
                    f"⚠️ ingest 재시도 포기 — {title[:80]}\n{_explain_error(e)}",
                )
                return
            log.info("ingest retry %d/%d: %s",
                     item["attempts"], _MAX_RETRY_ATTEMPTS, title[:80])
            _INGEST_RETRY_QUEUE.append(item)
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
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("forget_search", cmd_forget_search))
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

    log.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
