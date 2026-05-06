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

URL_RE = re.compile(r"https?://\S+")
_MD_BOLD_RE = re.compile(r"\*\*([^\*\n]{1,200}?)\*\*")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_NUMBERED_SECTION_RE = re.compile(
    r"^(\s*)(\d+)\.\s+(.{3,100}?)[:：]?\s*$", re.MULTILINE
)
_SECTION_EMOJIS = ["📌", "🔹", "🔸", "⚙️", "🧪", "💡", "📊", "🎯", "⚡", "🔧"]
_SEP = "━" * 22


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


async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return
    if config.TELEGRAM_CHANNEL_ID and str(msg.chat.id) != config.TELEGRAM_CHANNEL_ID:
        return
    await _ingest_message(msg, ctx, notify_chat_id=config.TELEGRAM_OWNER_ID)


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
        log.exception("agent failed")
        await update.message.reply_text(f"⚠️ 오류: {e}")
        return
    body = _strip_markdown(result["text"])
    suffix_lines = []
    if result["sources"]:
        suffix_lines.append("📚 " + ", ".join(result["sources"][:5]))
    if result["tool_calls"]:
        suffix_lines.append(f"🔧 {' → '.join(result['tool_calls'])}")
    suffix = ("\n\n" + "\n".join(suffix_lines)) if suffix_lines else ""
    await update.message.reply_text(f"{body}{suffix}")


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
            results.append({"status": "error", "error": str(e)})

    for url in URL_RE.findall(text):
        try:
            results.append(await pipeline.ingest_url(url))
        except Exception as e:
            log.exception("url ingest failed: %s", url)
            results.append({"status": "error", "error": str(e), "source": url})

    plain = URL_RE.sub("", text).strip()
    if plain and not msg.document:
        try:
            results.append(await pipeline.ingest_text(plain, f"tg-msg:{msg.message_id}"))
        except Exception as e:
            log.exception("text ingest failed")
            results.append({"status": "error", "error": str(e)})

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
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("forget_search", cmd_forget_search))
    app.add_handler(CommandHandler("deep", cmd_deep))

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND, on_private
    ))

    log.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
