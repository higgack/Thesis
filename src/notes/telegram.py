"""Telegram surface for the study-notes subsystem.

- handle_study_post(): a message in the dedicated study channel →
  parse (reusing loaders) → synthesise a note → notify the owner.
- /notes       : count + dashboard link.
- /notes_guide : detailed usage (also repliable in-channel for pinning).

No spaced-repetition review UI — the user browses notes daily on the
dashboard. The SRS data layer (note_srs, srs.py) stays dormant in case
it's wanted again.

Owner-gated. Registered from bot.py via register(app); on_channel_post
routes the study channel here (config.STUDY_CHANNEL_ID) before the
normal brain-ingest path, so study material never pollutes the wiki.
"""
from __future__ import annotations

import logging
import os
import re

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .. import config
from . import channel, store

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\")]+")
_DOC_EXTS = ("pdf", "pptx", "docx", "xlsx", "xls")
# 파일 첨부로 온 오디오(msg.document)도 음성 경로로 재라우팅하기 위한 확장자.
_AUDIO_EXTS = ("mp3", "m4a", "wav", "ogg", "oga", "flac", "aac", "opus",
               "wma")

_NOTES_GUIDE_TEXT = """📒 <b>학습 노트 사용법</b>

내가 공부한 자료를 다시 읽고 되새김질하는 개인 노트 시스템.

<b>1. 자료 넣기</b>
전용 학습 채널에 그냥 올리면 자동 노트화 (두뇌 학습과 동일 타입):
• URL · 유튜브 · PDF · PPTX · DOCX · XLSX · 텍스트
• 🎙 <b>음성/오디오</b> (mp3·m4a·wav 등, 파일 첨부도 OK) — Gemini STT
  전사 → 노트. 캡션을 달면 전사문 앞에 붙음.
• 🖼 <b>사진</b> — 캡션 ≥80자면 캡션으로(무료), 짧으면 Vision OCR.
  캡션에 [OCR] 넣으면 강제 OCR.
• 올리면 DM으로 <i>📒 노트 만드는 중…</i> → 완료/실패 알림
• 텍스트 살아있는 자료가 최적 (스캔 PDF는 OCR, 최대 7p)

<b>2. 노트 = 요약이 아님</b>
🎯 한 줄 요지 · 🧠 개념 지도(Mermaid 다이어그램) · 📖 정리(표·수식 보존) · 🔑 핵심 용어. 수식은 $...$로 렌더링.

<b>3. 명령어</b>
• /notes — 노트 개수 + 대시보드 링크
• /notes_guide — 이 도움말

<b>4. 대시보드</b>
노트 본문(KaTeX 수식·마크다운 표·Mermaid 다이어그램) + 노트별 💰비용·⏱시간 + 오늘/이번달 노트 비용 + 🗑 삭제. Archive·Wiki·Commands와 상호 연결.

<b>5. 비용</b>
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

async def _channel_command(cmd: str, chat_id, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to a command posted *in* the study channel (so the guide can
    be pinned). Telegram never echoes the bot's own channel posts back,
    so this can't re-trigger handle_study_post."""
    if cmd in ("notes_guide", "help", "guide", "start", "notes_help"):
        await ctx.bot.send_message(chat_id, _NOTES_GUIDE_TEXT,
                                   parse_mode="HTML", disable_web_page_preview=True)
    elif cmd == "notes":
        st = store.stats()
        await ctx.bot.send_message(
            chat_id, f"📒 <b>학습 노트</b> · 총 {st['notes']}개\n"
            "ℹ️ 사용법: /notes_guide", parse_mode="HTML")
    else:
        await ctx.bot.send_message(
            chat_id, f"알 수 없는 명령: /{cmd}\nℹ️ 사용법: /notes_guide")


async def handle_study_post(msg, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Route one study-channel message to the notes pipeline, with a
    live progress message so the owner is never left in the dark
    (success, empty-extraction, or error all get surfaced)."""
    owner = config.TELEGRAM_OWNER_ID
    text = msg.text or msg.caption or ""

    # Commands typed in the channel → reply in the channel (pinnable),
    # never ingested as study material.
    stripped = text.strip()
    if stripped.startswith("/"):
        cmd = stripped[1:].split()[0].split("@")[0].lower() if len(stripped) > 1 else ""
        log.info("study channel command: /%s", cmd)
        try:
            await _channel_command(cmd, msg.chat_id, ctx)
        except Exception:
            log.warning("study channel command reply failed: /%s", cmd)
        return

    doc = getattr(msg, "document", None)
    voice = getattr(msg, "voice", None)
    audio = getattr(msg, "audio", None)
    photo = getattr(msg, "photo", None)
    # 오디오가 일반 파일 첨부(document)로 오면 음성 경로로 재라우팅 —
    # 두뇌(RAG) 학습과 동일하게 모든 인입 타입을 지원한다 (2026-07 요청).
    if doc and not audio and \
            (doc.file_name or "").lower().rsplit(".", 1)[-1] in _AUDIO_EXTS:
        audio, doc = doc, None
    url_m = _URL_RE.search(text)
    src_label = (doc.file_name if doc else None) or \
        ("음성 메모" if voice else None) or \
        ((getattr(audio, "file_name", None) or getattr(audio, "title", None)
          or "오디오") if audio else None) or \
        ("사진" if photo else None) or \
        (url_m.group(0) if url_m else "텍스트")
    log.info("study post received: %s", src_label)

    progress = None
    try:
        progress = await ctx.bot.send_message(
            owner, f"📒 노트 만드는 중… ({src_label})")
    except Exception:
        progress = None

    urls = _URL_RE.findall(text)
    note_ids: list[str] = []
    err: str | None = None
    try:
        # 0) 음성/오디오 → 두뇌 경로와 같은 Gemini STT → 전사문으로 노트.
        #    임시 파일로 받고 전사 후 즉시 삭제 (노트는 텍스트만 보존;
        #    1.5h 강의 mp3를 data/files에 쌓아둘 이유가 없다).
        if voice or audio:
            import asyncio as _aio
            import tempfile
            from pathlib import Path as _P
            from ..ingest.loaders import transcribe_audio_async
            media = voice or audio
            mime = getattr(media, "mime_type", None) or (
                "audio/ogg" if voice else "audio/mpeg")
            with tempfile.TemporaryDirectory(prefix="note_audio_") as td:
                tmp = _P(td) / "audio.bin"
                tf = await ctx.bot.get_file(media.file_id)
                await tf.download_to_drive(str(tmp))
                data = await _aio.to_thread(tmp.read_bytes)
            transcript = await transcribe_audio_async(
                data, mime_type=mime, purpose="notes")
            if transcript:
                cap = (msg.caption or "").strip()
                body = (cap + "\n\n" + transcript).strip() if cap \
                    else transcript
                ref = f"tg-audio:{media.file_unique_id}"
                nid = await channel.ingest_text(
                    "audio", ref, body,
                    title=None if src_label in ("음성 메모", "오디오")
                    else src_label)
                if nid:
                    note_ids.append(nid)
        # 0-b) 사진 → 캡션 우선, 부족하면 Vision OCR (두뇌 경로와 동일
        #      정책: 캡션 ≥80자는 무료 경로, "[OCR]" 태그로 강제 OCR).
        elif photo:
            from ..ingest.loaders import ocr_image_async
            p = photo[-1]  # 최대 해상도
            tf = await ctx.bot.get_file(p.file_id)
            data = bytes(await tf.download_as_bytearray())
            cap = (msg.caption or "").strip()
            force_ocr = "[OCR]" in cap.upper()
            cap_clean = cap.replace("[OCR]", "").replace("[ocr]", "").strip()
            if len(cap_clean) >= 80 and not force_ocr:
                body = cap_clean
            else:
                ocr_text = await ocr_image_async(
                    data, mime_type="image/jpeg") or ""
                body = (cap_clean + "\n\n" + ocr_text).strip() \
                    if cap_clean else ocr_text
            if body.strip():
                nid = await channel.ingest_text(
                    "image", f"tg-photo:{p.file_unique_id}", body)
                if nid:
                    note_ids.append(nid)
        # 1) Attached document (pdf/office) → download + ingest.
        if doc and (doc.file_name or "").lower().rsplit(".", 1)[-1] in _DOC_EXTS:
            dest = config.DATA_DIR / "files" / (doc.file_name or f"{doc.file_id}.bin")
            tf = await ctx.bot.get_file(doc.file_id)
            await tf.download_to_drive(str(dest))
            nid = await channel.ingest_file(dest)
            if nid:
                note_ids.append(nid)
        # 2) URLs in the body (blog/web/youtube/arxiv) → learn the LINKED
        #    content (the actual article), not the message text.
        for url in urls:
            nid = await channel.ingest_url(url)
            if nid:
                note_ids.append(nid)
        # 3) Plain text only — ONLY when there is no doc AND no URL. A URL
        #    that failed extraction must NOT fall back to the message text:
        #    that built a misleading note from the Telegram link preview
        #    snippet instead of the real article (blog/news divergence bug).
        if not note_ids and text.strip() and not doc and not urls:
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
        out = "\n".join(lines)
    elif err:
        out = f"⚠️ 노트 생성 실패: {err}"
    elif urls:
        # A link was given but its content couldn't be extracted — say so
        # honestly rather than fabricating a note from the link preview.
        out = (f"⚠️ 링크 본문을 못 가져왔어 ({src_label})\n"
               "JS 렌더링/로그인 전용/차단 페이지일 수 있어. 잠시 후 다시 "
               "시도하거나, 원문 본문을 복사해서 텍스트로 붙여넣어줘. "
               "(미리보기 요약으로 가짜 노트를 만들지 않으려고 일부러 중단함)")
    elif voice or audio:
        out = (f"⚠️ 노트를 못 만들었어 ({src_label})\n"
               "음성 전사(STT)가 비었어 — 무음/음악 위주 파일이거나 "
               "전사 실패일 수 있어. 다시 보내보거나 짧게 잘라서 시도해봐.")
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
    st = store.stats()
    lines = ["📒 <b>학습 노트</b>", f"• 총 노트: {st['notes']}개"]
    link = _dash_link()
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


def register(app: Application) -> None:
    """Wire the study-notes commands into the bot."""
    app.add_handler(CommandHandler("notes", cmd_notes))
    app.add_handler(CommandHandler("notes_guide", cmd_notes_guide))
    log.info("study-notes telegram handlers registered")
