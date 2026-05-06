import logging
import re
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

from . import config
from .store import meta, vector
from .ingest import pipeline
from .agent import answer as agent_answer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

URL_RE = re.compile(r"https?://\S+")


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
        "• 채널에 링크/PDF/유튜브/텍스트를 올리면 자동 수집·요약\n"
        "• 여기 DM으로 자연어 질문\n"
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

    if msg.document or msg.text and URL_RE.search(msg.text or ""):
        await _ingest_message(msg, ctx, notify_chat_id=msg.chat.id)
        return

    text = msg.text or ""
    if not text.strip():
        return
    await _typing(update, ctx)
    try:
        result = await agent_answer.answer(text)
    except Exception as e:
        log.exception("answer failed")
        await msg.reply_text(f"⚠️ 오류: {e}")
        return
    suffix = ""
    if result["sources"]:
        suffix = "\n\n📚 " + ", ".join(result["sources"][:5])
    await msg.reply_text(f"{result['text']}{suffix}")


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
            if dest.suffix.lower() == ".pdf":
                results.append(await pipeline.ingest_pdf(dest, label))
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
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("forget", cmd_forget))

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND, on_private
    ))

    log.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
