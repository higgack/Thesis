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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

from .. import config
from . import channel, store

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\")]+")

# 노트 방식(일반/책) 질문의 대기 항목: pid → payload dict.
# 처음엔 메모리 전용이었는데, 대형 파일 여러 개를 올린 사이 배포/워치독
# 재시작이 끼면 버튼 전부가 "만료"로 죽어 사용자가 파일을 전부 다시
# 찾아 올려야 했다 (2026-08-27 실제 발생, 8건 유실). 이제 디스크에
# 저장해 재시작을 넘긴다 — 봇 file_id 는 같은 토큰이면 재시작 후에도
# 유효하므로 파일 본문이 아니라 file_id 만 저장하면 된다.
_PENDING_MODE: dict[str, dict] = {}
_PENDING_TTL_SEC = 48 * 3600     # 이틀 — 이제 재시작에도 살아남으니 넉넉히
_PENDING_PATH = config.DATA_DIR / "notes_pending_mode.json"
_PENDING_LOADED = False


def _persist_pending() -> None:
    import json as _j
    import os as _os
    try:
        tmp = _PENDING_PATH.with_suffix(".tmp")
        tmp.write_text(_j.dumps(_PENDING_MODE, ensure_ascii=False),
                       encoding="utf-8")
        _os.replace(tmp, _PENDING_PATH)
    except Exception:
        log.warning("notes pending persist failed", exc_info=True)


def _load_pending() -> None:
    global _PENDING_LOADED
    if _PENDING_LOADED:
        return
    _PENDING_LOADED = True
    import json as _j
    try:
        if _PENDING_PATH.exists():
            d = _j.loads(_PENDING_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                for k, v in d.items():
                    _PENDING_MODE.setdefault(k, v)
    except Exception:
        log.warning("notes pending load failed", exc_info=True)


def _prune_pending(now: float) -> None:
    for k in [k for k, v in _PENDING_MODE.items()
              if now - float(v.get("ts", 0)) > _PENDING_TTL_SEC]:
        _PENDING_MODE.pop(k, None)


def _stash_pending(msg) -> str:
    import time as _t
    import uuid as _u
    _load_pending()
    now = _t.time()
    _prune_pending(now)
    doc = getattr(msg, "document", None)
    payload = {
        "ts": now,
        "text": (msg.text or msg.caption or ""),
        "doc_file_id": getattr(doc, "file_id", None) if doc else None,
        "doc_file_name": getattr(doc, "file_name", None) if doc else None,
    }
    pid = _u.uuid4().hex[:12]
    _PENDING_MODE[pid] = payload
    _persist_pending()
    return pid


def _msg_from_payload(payload: dict):
    """디스크에서 복원한 payload 를 _process_study_post 가 읽는 msg 형태로.
    질문은 문서/URL/텍스트에만 던지므로 그 세 필드만 있으면 된다."""
    import types as _types
    doc = None
    if payload.get("doc_file_id"):
        doc = _types.SimpleNamespace(file_id=payload["doc_file_id"],
                                     file_name=payload.get("doc_file_name"))
    return _types.SimpleNamespace(
        text=payload.get("text") or None, caption=None, document=doc,
        voice=None, audio=None, video=None, photo=None,
        chat_id=config.TELEGRAM_OWNER_ID)
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
• 🎬 <b>영상</b> (최대 20MB) — Gemini가 화면(텍스트/차트)+음성을 통째로
  읽어서 노트. 20MB 넘으면 짧게 잘라서 다시.
• 🖼 <b>사진</b> — 캡션 ≥80자면 캡션으로(무료), 짧으면 Vision OCR.
  캡션에 [OCR] 넣으면 강제 OCR.
• 문서·URL·텍스트는 올리면 먼저 <b>[📝 일반 / 📚 책 모드]</b> 버튼으로
  방식을 물어봄 — 책 모드는 장문 자료(책·긴 리포트)용으로
  핵심 모델 → 장별 핵심 → 용어집 → 치트시트 구조로 정리
  (음성·영상·사진은 묻지 않고 일반). 호출은 동일 1회, 비용 거의 같음.
• 선택하면 DM으로 <i>📒 노트 만드는 중…</i> → 완료/실패 알림
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
    video = getattr(msg, "video", None)
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
        ("영상" if video else None) or \
        ("사진" if photo else None) or \
        (url_m.group(0) if url_m else "텍스트")
    log.info("study post received: %s", src_label)

    # 일반/책 모드 질문 (사용자 요청 2026-08-27): 문서·URL·순수 텍스트만.
    # 음성·영상·사진은 장 구조가 없어 책 모드가 무의미 → 묻지 않고 일반.
    bookable = bool(doc or url_m
                    or (text.strip() and not (voice or audio or video or photo)))
    if bookable:
        pid = _stash_pending(msg)
        try:
            await ctx.bot.send_message(
                owner,
                f"📒 <b>{src_label}</b>\n노트 방식을 골라줘:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📝 일반",
                                         callback_data=f"notemode:{pid}:normal"),
                    InlineKeyboardButton("📚 책 모드",
                                         callback_data=f"notemode:{pid}:book"),
                ]]))
            return
        except Exception:
            # 질문 전송 실패(네트워크 등) → 기존 동작(일반)으로 진행,
            # 자료가 조용히 사라지는 일은 없게.
            log.warning("note mode ask failed — falling back to normal")
            _PENDING_MODE.pop(pid, None)

    await _process_study_post(msg, ctx, mode="normal")


async def on_notemode_callback(update: Update,
                               ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not _is_owner(update):
        return
    try:
        _, pid, mode = (q.data or "").split(":", 2)
    except ValueError:
        await q.answer()
        return
    _load_pending()
    entry = _PENDING_MODE.pop(pid, None)
    _persist_pending()
    await q.answer()
    if entry is None:
        try:
            await q.edit_message_text(
                "⏳ 만료된 요청이야 (48시간 경과) — 자료를 다시 올려줘.")
        except Exception:
            pass
        return
    msg = _msg_from_payload(entry)
    label = "📚 책 모드" if mode == "book" else "📝 일반"
    try:
        await q.edit_message_text(f"{label} 선택됨 — 노트 만드는 중…")
    except Exception:
        pass
    # 질문 메시지를 진행 메시지로 재사용 — 안 넘기면 처리부가 새 진행
    # 메시지를 만들어 그쪽만 완료로 바뀌고, 이 메시지는 "만드는 중…"에
    # 영영 멈춰 있었다 (2026-08-27 사용자 리포트).
    await _process_study_post(msg, ctx,
                              mode="book" if mode == "book" else "normal",
                              progress_msg=getattr(q, "message", None))


# 대형 자료 여러 개가 동시에 돌면 2 vCPU 에서 서로 밟고 재시작을
# 부른다 — 사용자 요청(2026-08-27 "천천히 하나씩")대로 노트 처리는
# 한 번에 하나만. 나머지는 이 락에서 순서를 기다리고, 기다리는 동안
# 진행 메시지에 대기 중임을 표시한다.
_PROCESS_LOCK = None


def _get_process_lock():
    global _PROCESS_LOCK
    import asyncio as _aio
    if _PROCESS_LOCK is None:
        _PROCESS_LOCK = _aio.Lock()
    return _PROCESS_LOCK


async def _process_study_post(msg, ctx: ContextTypes.DEFAULT_TYPE,
                              mode: str = "normal",
                              progress_msg=None) -> None:
    lock = _get_process_lock()
    if lock.locked() and progress_msg is not None:
        try:
            await ctx.bot.edit_message_text(
                "⏳ 앞 노트 작업이 끝나면 시작할게 (순서 대기 중)",
                chat_id=progress_msg.chat_id,
                message_id=progress_msg.message_id)
        except Exception:
            pass
    async with lock:
        await _process_study_post_inner(msg, ctx, mode, progress_msg)


async def _process_study_post_inner(msg, ctx: ContextTypes.DEFAULT_TYPE,
                                    mode: str = "normal",
                                    progress_msg=None) -> None:
    owner = config.TELEGRAM_OWNER_ID
    text = msg.text or msg.caption or ""
    doc = getattr(msg, "document", None)
    voice = getattr(msg, "voice", None)
    audio = getattr(msg, "audio", None)
    video = getattr(msg, "video", None)
    photo = getattr(msg, "photo", None)
    if doc and not audio and \
            (doc.file_name or "").lower().rsplit(".", 1)[-1] in _AUDIO_EXTS:
        audio, doc = doc, None
    url_m = _URL_RE.search(text)
    src_label = (doc.file_name if doc else None) or \
        ("음성 메모" if voice else None) or \
        ((getattr(audio, "file_name", None) or getattr(audio, "title", None)
          or "오디오") if audio else None) or \
        ("영상" if video else None) or \
        ("사진" if photo else None) or \
        (url_m.group(0) if url_m else "텍스트")

    progress = progress_msg
    if progress is None:
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
            from ..ingest.pipeline import STT_MAX_BYTES
            media = voice or audio
            size = int(getattr(media, "file_size", 0) or 0)
            if size > STT_MAX_BYTES:
                # 다운로드조차 하지 않는다 — 128MB를 RAM에 올렸다 실패하는
                # 경로가 봇을 메모리 한계로 몰았음 (2026-07-04).
                raise ValueError(
                    f"오디오 {size // (1024 * 1024)}MB — STT 한도 20MB 초과. "
                    "20분 안팎(≈15-20MB)으로 분할해서 올려줘.")
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
        # 0-a) 영상 → 두뇌 경로와 같은 Gemini 네이티브 비디오 이해(화면+
        #      음성 한 번에) → 텍스트로 노트. 오디오와 같은 20MB inline
        #      한도(다운로드 전에 file_size로 먼저 거른다).
        elif video:
            from ..ingest.loaders import transcribe_video_async
            from ..ingest.pipeline import VIDEO_MAX_BYTES
            size = int(getattr(video, "file_size", 0) or 0)
            if size > VIDEO_MAX_BYTES:
                raise ValueError(
                    f"영상 {size // (1024 * 1024)}MB — 처리 한도 20MB 초과. "
                    "짧게 잘라서 다시 올리거나, 긴 영상은 유튜브 업로드 후 "
                    "링크로 보내줘.")
            mime = getattr(video, "mime_type", None) or "video/mp4"
            tf = await ctx.bot.get_file(video.file_id)
            data = bytes(await tf.download_as_bytearray())
            transcript = await transcribe_video_async(data, mime_type=mime)
            if transcript:
                cap = (msg.caption or "").strip()
                body = (cap + "\n\n" + transcript).strip() if cap \
                    else transcript
                ref = f"tg-video:{video.file_unique_id}"
                nid = await channel.ingest_text("video", ref, body)
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
            nid = await channel.ingest_file(dest, mode=mode)
            if nid:
                note_ids.append(nid)
        # 2) URLs in the body (blog/web/youtube/arxiv) → learn the LINKED
        #    content (the actual article), not the message text.
        for url in urls:
            nid = await channel.ingest_url(url, mode=mode)
            if nid:
                note_ids.append(nid)
        # 3) Plain text only — ONLY when there is no doc AND no URL. A URL
        #    that failed extraction must NOT fall back to the message text:
        #    that built a misleading note from the Telegram link preview
        #    snippet instead of the real article (blog/news divergence bug).
        if not note_ids and text.strip() and not doc and not urls:
            nid = await channel.ingest_text("text", "study-text", text,
                                            mode=mode)
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
    elif voice or audio or video:
        out = (f"⚠️ 노트를 못 만들었어 ({src_label})\n"
               "음성 전사(STT)/영상 분석이 비었어 — 무음/음악 위주거나 "
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
    app.add_handler(CallbackQueryHandler(on_notemode_callback,
                                         pattern=r"^notemode:"))
    log.info("study-notes telegram handlers registered")
