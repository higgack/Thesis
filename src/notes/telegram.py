"""Telegram surface for the study-notes subsystem.

- handle_study_post(): a message in the dedicated study channel →
  parse (reusing loaders) → synthesise a note → notify the owner.
- /notes   : stats + dashboard link.
- /review  : show today's due notes with [😵/🤔/✅] self-grade buttons.
- grade callback (^nrev:): apply the grade via SM-2 lite, reschedule,
  edit the message to show the next review date.

Owner-gated. Registered from bot.py via register(app); on_channel_post
routes the study channel here (config.STUDY_CHANNEL_ID) before the
normal brain-ingest path, so study material never pollutes the wiki.
"""
from __future__ import annotations

import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes,
)

from .. import config
from . import channel, recall, store

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\")]+")
_DOC_EXTS = ("pdf", "pptx", "docx", "xlsx", "xls")

_GRADE_LABELS = {0: "😵 까먹음", 1: "🤔 가물가물", 2: "✅ 기억함"}

_NOTES_GUIDE_TEXT = """📒 <b>학습 노트 (체화) 사용법</b>

검색용 위키와 달리, <b>내가 공부한 걸 다시 읽고 되새김질해 오래 체화</b>하는 개인 노트 시스템.

<b>1. 자료 넣기</b>
전용 학습 채널에 그냥 올리면 자동 노트화:
• URL · 유튜브 · PDF · PPTX · DOCX · XLSX · 텍스트
• 올리면 DM으로 <i>📒 노트 만드는 중…</i> → 완료/실패 알림
• 텍스트 살아있는 자료가 최적 (스캔 PDF는 OCR, 최대 7p)

<b>2. 노트 = 요약이 아님</b>
🎯 한 줄 요지 · 🧠 개념 지도 · 📖 정리(표·수식 보존) · 🔑 핵심 용어 · ❓ 복습 질문(능동 회상). 수식은 $...$로 렌더링.

<b>3. 복습 — 체화의 핵심</b>
• /review → 오늘 복습할 노트 + [😵 까먹음 / 🤔 가물가물 / ✅ 기억함]
• 자가평가 → SM-2로 <b>다음 복습일 자동 조정</b> (까먹음=1일, 기억함=점점 길게)
• 새 노트는 내일 첫 복습 예약

<b>4. 명령어</b>
• /notes — 통계(총 노트·오늘 복습·누적·비용) + 대시보드 링크
• /review — 오늘 복습 큐 + 자가평가 버튼
• /notes_guide — 이 도움말

<b>5. 대시보드</b>
읽기·복습 전용: 노트 본문(KaTeX 수식·표) + 복습질문 펼치기 + 노트별 💰비용·⏱시간 + 오늘/이번달 비용. Archive·Wiki와 상호 연결.

<b>6. 비용</b>
노트당 flash 합성 ~₩수(저렴) · 파싱 무료(로컬) · OCR 페이지만 유료."""


def _is_owner(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == config.TELEGRAM_OWNER_ID)


def _dash_link(note_id: str | None = None) -> str:
    token = (os.getenv("DASHBOARD_TOKEN", "") or "").strip()
    base = (os.getenv("DASHBOARD_BASE_URL", "") or "").strip().rstrip("/")
    if not token or not base:
        return ""
    if note_id:
        return f"{base}/{token}/notes/note-{note_id}.html"
    return f"{base}/{token}/notes/"


# ----------------------------------------------------------- ingest ---

async def handle_study_post(msg, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Route one study-channel message to the notes pipeline, with a
    live progress message so the owner is never left in the dark
    (success, empty-extraction, or error all get surfaced)."""
    owner = config.TELEGRAM_OWNER_ID
    text = msg.text or msg.caption or ""
    doc = getattr(msg, "document", None)
    url_m = _URL_RE.search(text)
    src_label = (doc.file_name if doc else None) or (
        url_m.group(0) if url_m else "텍스트")
    log.info("study post received: %s", src_label)

    progress = None
    try:
        progress = await ctx.bot.send_message(
            owner, f"📒 노트 만드는 중… ({src_label})")
    except Exception:
        progress = None

    note_ids: list[str] = []
    err: str | None = None
    try:
        # 1) Attached document (pdf/office) → download + ingest.
        if doc and (doc.file_name or "").lower().rsplit(".", 1)[-1] in _DOC_EXTS:
            dest = config.DATA_DIR / "files" / (doc.file_name or f"{doc.file_id}.bin")
            tf = await ctx.bot.get_file(doc.file_id)
            await tf.download_to_drive(str(dest))
            nid = await channel.ingest_file(dest)
            if nid:
                note_ids.append(nid)
        # 2) URLs in the body (web/youtube/arxiv).
        for url in _URL_RE.findall(text):
            nid = await channel.ingest_url(url)
            if nid:
                note_ids.append(nid)
        # 3) Plain text only (no doc, no url) → note from the text.
        if not note_ids and text.strip() and not doc:
            nid = await channel.ingest_text("text", "study-text", text)
            if nid:
                note_ids.append(nid)
    except Exception as e:
        log.exception("study post failed")
        err = str(e)[:160]

    # Build the outcome message (success / empty / error).
    if note_ids:
        lines = ["📒 <b>학습 노트 생성 완료</b>"]
        for nid in note_ids:
            n = store.get_note(nid)
            title = (n or {}).get("title") or nid
            link = _dash_link(nid)
            lines.append(f"• <b>{title}</b>" + (f"\n  🔗 {link}" if link else ""))
        lines.append("\n내일 첫 복습 예정 · /review 로 복습")
        out = "\n".join(lines)
    elif err:
        out = f"⚠️ 노트 생성 실패: {err}"
    else:
        out = (f"⚠️ 노트를 못 만들었어 ({src_label})\n"
               "본문이 너무 짧거나 추출 실패 — 스캔 PDF/이미지거나 차단 호스트일 수 "
               "있어. 텍스트가 살아있는 자료/URL로 다시 시도해봐.")
    log.info("study post done: %s → %d note(s)%s",
             src_label, len(note_ids), f" err={err}" if err else "")

    try:
        if progress is not None:
            await ctx.bot.edit_message_text(
                out, chat_id=owner, message_id=progress.message_id,
                parse_mode="HTML", disable_web_page_preview=True)
        else:
            await ctx.bot.send_message(owner, out, parse_mode="HTML",
                                       disable_web_page_preview=True)
    except Exception:
        log.warning("study post outcome notify failed")


# --------------------------------------------------------- commands ---

async def cmd_notes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    st = recall.stats()
    link = _dash_link()
    lines = [
        "📒 <b>학습 노트 (체화)</b>",
        f"• 총 노트: {st['notes']}개",
        f"• 오늘 복습: {st['due_today']}개",
        f"• 누적 복습: {st['total_reviews']}회",
    ]
    if st["due_today"]:
        lines.append("\n복습하려면 /review")
    if link:
        lines.append(f"🔗 대시보드: {link}")
    lines.append("ℹ️ 상세 사용법: /notes_guide")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                    disable_web_page_preview=True)


async def cmd_notes_guide(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    await update.message.reply_text(_NOTES_GUIDE_TEXT, parse_mode="HTML",
                                    disable_web_page_preview=True)


def _grade_kb(rowid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("😵 까먹음", callback_data=f"nrev:0:{rowid}"),
        InlineKeyboardButton("🤔 가물가물", callback_data=f"nrev:1:{rowid}"),
        InlineKeyboardButton("✅ 기억함", callback_data=f"nrev:2:{rowid}"),
    ]])


async def cmd_review(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    due = store.due_notes()
    if not due:
        await update.message.reply_text("✅ 오늘 복습할 노트가 없어요.")
        return
    await update.message.reply_text(
        f"🔁 오늘 복습할 노트 {len(due)}개. 각 노트를 떠올려본 뒤 자가평가하세요.")
    for n in due[:10]:
        link = _dash_link(n["id"])
        body = f"📒 <b>{n['title']}</b>"
        if link:
            body += f"\n🔗 {link}"
        await ctx.bot.send_message(
            update.effective_chat.id, body, parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_grade_kb(int(n["rowid"])))


async def on_grade_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    if not _is_owner(update):
        await q.answer()
        return
    try:
        _, grade_s, rowid_s = q.data.split(":", 2)
        grade = int(grade_s)
        rowid = int(rowid_s)
    except Exception:
        await q.answer("잘못된 요청")
        return
    note_id = store.id_for_rowid(rowid)
    if not note_id:
        await q.answer("노트를 찾을 수 없음")
        return
    new = recall.grade(note_id, grade)
    await q.answer(_GRADE_LABELS.get(grade, ""))
    nxt = (new or {}).get("next_due", "?")
    try:
        await q.edit_message_text(
            f"{(q.message.text or '').splitlines()[0]}\n"
            f"→ {_GRADE_LABELS.get(grade,'')} · 다음 복습 {nxt}",
            parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        pass


def register(app: Application) -> None:
    """Wire the study-notes commands + grade callback into the bot."""
    app.add_handler(CommandHandler("notes", cmd_notes))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("notes_guide", cmd_notes_guide))
    app.add_handler(CallbackQueryHandler(on_grade_callback, pattern=r"^nrev:"))
    log.info("study-notes telegram handlers registered")
