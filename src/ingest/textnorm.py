"""Invisible-Unicode hygiene + sentence-safe clipping for text.

watermarks-remover(guillaumemeyer)의 Layer A 아이디어 차용 (2026-08-27).
웹 복사·AI 생성 텍스트에 섞여 오는 zero-width/bidi/BOM 문자는 눈에는 안
보이지만 셋 다 실측으로 재현된 실제 문제를 만든다:
  (1) content_hash 가 달라져 같은 글을 두 번 학습(중복 과금);
  (2) FTS 키워드 매칭이 단어 중간에서 끊김 ('삼성​전자'에
      '삼성전자' 불일치);
  (3) KG 개체명 fork — kg._norm_token 의 \\s 는 Cf(format) 문자를 잡지
      못해 같은 회사가 두 노드로 갈라진다.
제거는 결정적(비 LLM·stdlib str.translate, C 속도)이고 표시되는 문자는
건드리지 않는다. U+200D(ZWJ)는 이모지 시퀀스(👨‍👩‍👧)를 묶으므로 남긴다.
NBSP 계열은 삭제하면 단어가 붙어버리므로 일반 공백으로 치환한다.
"""

_REMOVE = (
    "​"   # zero width space — 웹/AI 텍스트 최다 빈도
    "‌"   # zero width non-joiner
    "⁠"   # word joiner
    "﻿"   # BOM / zero width no-break space
    "­"   # soft hyphen
    "᠎"   # mongolian vowel separator
    "‎‏"          # LRM / RLM
    "‪‫‬‭‮"  # bidi embedding/override 컨트롤
    "⁦⁧⁨⁩"        # bidi isolate 컨트롤
)
_TABLE: dict[int, str | None] = {ord(c): None for c in _REMOVE}
_TABLE.update({0x00A0: " ", 0x202F: " ", 0x2007: " "})  # NBSP 계열 → 공백


def strip_invisible(text: str) -> str:
    """Remove invisible/format Unicode, map NBSP variants to space.
    Deterministic, display-preserving; safe on empty input."""
    if not text:
        return text
    return text.translate(_TABLE)


def truncate_at_sentence(text: str, cap: int) -> str:
    """Trim `text` to roughly `cap` chars, breaking at the nearest
    sentence boundary inside the cap window. Adds " …" when it cuts, so
    a clipped passage is never mistaken for a finished one — a bare
    `text[:cap]` slice ends mid-word and reads like the source itself
    stopped there. Korean sentences end 다./요./니다./까? + space;
    English ". ". Falls back to a hard cut + "…" when no boundary is
    found in the search window.

    Lives here (stdlib-only, no project imports) so bot.py and
    store/wiki.py share ONE implementation instead of each growing
    their own.
    """
    if not text or len(text) <= cap:
        return text
    cutoff = text[:cap]
    search_from = max(0, cap - 250)
    end = max(
        cutoff.rfind("다. ", search_from), cutoff.rfind("다.\n", search_from),
        cutoff.rfind("요. ", search_from), cutoff.rfind("요.\n", search_from),
        cutoff.rfind("니다. ", search_from), cutoff.rfind("니다.\n", search_from),
        cutoff.rfind(". ", search_from), cutoff.rfind(".\n", search_from),
        cutoff.rfind("? ", search_from), cutoff.rfind("! ", search_from),
    )
    if end < 0:
        return cutoff.rstrip() + "…"
    # Walk forward past the punctuation that defined the sentence end.
    while end < len(cutoff) and cutoff[end] not in (" ", "\n"):
        end += 1
    return cutoff[:end] + " …"
