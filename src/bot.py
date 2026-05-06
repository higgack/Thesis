import base64
import logging
import re
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

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
    text = msg.text or msg.caption or ""
    results = []

    if msg.document:
        try:
            file = await ctx.bot.get_file(msg.document.file_id)
            dest = Path(config.DATA_DIR) / "files" / msg.document.file_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            await file.download_to_drive(custom_path=dest)
            label = f"tg-doc:{msg.document.file_unique_id}:{msg.document.file_name}"
            suffix = dest.suffix.lower()
            if suffix == ".pdf":
                results.append(await pipeline.ingest_pdf(dest, label))
            elif suffix == ".pptx":
                results.append(await pipeline.ingest_pptx(dest, label))
            elif suffix == ".docx":
                results.append(await pipeline.ingest_docx(dest, label))
            elif suffix in {".ppt", ".doc"}:
                results.append({
                    "status": "error",
                    "error": f"{suffix} (구버전 포맷)은 지원 안 됩니다. {suffix}x로 변환해서 다시 보내주세요.",
                })
            else:
                content = dest.read_text(encoding="utf-8", errors="ignore")
                results.append(await pipeline.ingest_text(content, label))
        except Exception as e:
            log.exception("file ingest failed")
            results.append({"status": "error", "error": _explain_error(e)})

    urls, plain = _extract_urls(text)
    for url in urls:
        try:
            results.append(await pipeline.ingest_url(url))
        except Exception as e:
            log.exception("url ingest failed: %s", url)
            results.append({"status": "error", "error": _explain_error(e), "source": url})

    if plain and not msg.document and len(plain) >= 80:
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
        else:
            lines.append(f"❌ {r.get('error', 'error')}")
    return "\n".join(lines)


def main():
    meta.init()
    obsidian.init()
    builder = Application.builder().token(config.TELEGRAM_BOT_TOKEN)
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

    log.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
