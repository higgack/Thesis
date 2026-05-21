"""Shared Korean translation for paper/patent search rows.

Single Flash-Lite batched call, parallelized across chunks of
~15 rows each so a 50-row search doesn't overflow max_tokens.
Mutates rows in place by setting `title_ko` and `abstract_ko`.

Used by both bot.py direct slash-command paths and the agent
tools layer (search_papers, search_patents, search_company_patents,
search_kr_papers, search_kr_patents_kisti, ...) so every
paper/patent the user ever sees comes out in Korean regardless
of source or entry point.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import re as _re

from .. import config

log = logging.getLogger(__name__)

_BATCH = 15
_SYSTEM = (
    "You are a precise English-to-Korean translator for academic "
    "papers and patents. Translate each TITLE and ABSTRACT into "
    "natural Korean. Preserve technical terms (model names, "
    "company names, acronyms like HBM/CMP/Cu-Cu/FC-BGA/LLM) in "
    "their original form. Output JSON only:\n"
    '{"items": [{"n": 1, "title_ko": "...", "abstract_ko": "..."}, ...]}\n'
    "Omit fields that were missing in the input. No preamble."
)


def _is_hangul(s: str) -> bool:
    """Return True if the string already contains Hangul. Used to
    skip rows that are already Korean (KIPRIS titles, KISTI Korean
    abstracts) so we don't waste a Flash-Lite call rewriting them."""
    if not s:
        return False
    return any("가" <= ch <= "힣" for ch in s)


async def _translate_one_batch(batch: list[dict],
                               offset: int) -> dict[int, dict]:
    """Translate one batch of ≤15 rows. Returns a {global_index: {title_ko, abstract_ko}}
    map keyed by the absolute row index in the caller's list. Empty
    map on failure (caller falls back to originals)."""
    lines: list[str] = []
    for i, r in enumerate(batch, 1):
        title = (r.get("title") or "").strip()
        abstract = (r.get("abstract") or "").strip()[:800]
        # Skip translation for rows already in Korean — saves ~30% of
        # tokens on /company_patents (KIPRIS) and /kr_papers (KISTI).
        if _is_hangul(title) and (not abstract or _is_hangul(abstract)):
            continue
        lines.append(f"[{i}]")
        if title:
            lines.append(f"TITLE: {title}")
        if abstract:
            lines.append(f"ABSTRACT: {abstract}")
        lines.append("")
    if not lines:
        return {}
    user_text = "\n".join(lines)
    try:
        from ..llm.gemini import complete
        resp = await complete(
            model=config.SUMMARY_MODEL,
            system=_SYSTEM,
            user=user_text,
            max_tokens=16384,
            temperature=0.1,
            purpose="translate",
        )
    except Exception:
        log.exception("translate_to_korean: LLM call failed (batch offset=%d)",
                      offset)
        return {}
    m = _re.search(r"\{.*\}", resp, _re.DOTALL)
    if not m:
        log.warning(
            "translate_to_korean: no JSON block (offset=%d, len=%d, last200=%r)",
            offset, len(resp), resp[-200:],
        )
        return {}
    try:
        data = _json.loads(m.group(0))
    except Exception as e:
        log.warning(
            "translate_to_korean: JSON parse failed (offset=%d, last200=%r): %s",
            offset, resp[-200:], e,
        )
        return {}
    out: dict[int, dict] = {}
    for item in (data.get("items") or []):
        try:
            local_n = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        if 1 <= local_n <= len(batch):
            out[offset + local_n - 1] = item
    return out


async def translate_to_korean(results: list[dict]) -> list[dict]:
    """Batch-translate title + abstract of every row to Korean.

    Mutates each row: sets `title_ko` and `abstract_ko` keys.
    Formatters in bot.py prefer those fields when present. The agent's
    paper/patent tools also set the original `title`/`abstract` to the
    Korean version (see tools.py) so the agent loop synthesizes Korean
    output even when the model doesn't proactively translate.

    Batched in groups of 15 so a 50-row search doesn't blow past
    max_tokens. Batches run concurrently — total latency is one batch,
    not N batches. ~₩5-10 per 50-row search at Flash-Lite pricing.
    """
    if not results:
        return results
    batches: list[tuple[int, list[dict]]] = []
    for start in range(0, len(results), _BATCH):
        batches.append((start, results[start:start + _BATCH]))
    if not batches:
        return results
    maps = await asyncio.gather(
        *(_translate_one_batch(b, offset) for offset, b in batches),
        return_exceptions=False,
    )
    merged: dict[int, dict] = {}
    for m in maps:
        merged.update(m)
    translated = 0
    for idx, r in enumerate(results):
        t = merged.get(idx)
        if not t:
            continue
        tk = (t.get("title_ko") or "").strip()
        ak = (t.get("abstract_ko") or "").strip()
        if tk:
            r["title_ko"] = tk
            translated += 1
        if ak:
            r["abstract_ko"] = ak
    log.info(
        "translate_to_korean: %d/%d rows translated (batches=%d)",
        translated, len(results), len(batches),
    )
    return results


async def translate_kisti_rows(rows: list[dict]) -> list[dict]:
    """Translate KISTI ScienceON rows (metaCode-keyed: Title /
    Title2 / Abstract / Abstract2 / AB / ABE / Definition) to Korean.

    Mutates each row: adds `Title_ko` and `Abstract_ko` for non-Korean
    rows. The bot.py formatter already prefers Title_ko; the agent
    tools layer also prefers Title_ko/Abstract_ko when assembling
    natural-language answers.

    Skips rows whose Title is already Hangul-heavy (Korean-published
    papers / patents / reports) so we don't waste a Flash-Lite call
    re-rendering Korean as Korean. Bound by the same 15-row batch
    cadence as `translate_to_korean`.
    """
    if not rows:
        return rows
    norm: list[dict] = []
    idx_map: list[int] = []
    for i, r in enumerate(rows):
        title = (r.get("Title") or r.get("Title2")
                 or r.get("TI") or r.get("TIE")
                 # NTIS public_project / related_content shapes:
                 or r.get("ProjectTitle") or r.get("과제명")
                 or r.get("논문명") or r.get("특허명") or r.get("보고서명")
                 or r.get("KOR_RPT_TITLE_NM") or r.get("KOR_TITLE_NM")
                 or r.get("ENG_RPT_TITLE_NM") or r.get("ENG_TITLE_NM")
                 or "").strip()
        abstract = (r.get("Abstract") or r.get("Abstract2")
                    or r.get("AB") or r.get("ABE")
                    or r.get("Definition")
                    # NTIS project: Goal / Effect carry the prose body
                    or r.get("Goal") or r.get("Effect")
                    or "").strip()
        lang = (r.get("Lang") or "").strip().lower()
        if lang in ("ko", "kor", "korean", "한국어", "한국", "kr"):
            continue
        if not title and not abstract:
            continue
        if _is_hangul(title) and (not abstract or _is_hangul(abstract)):
            continue
        norm.append({"title": title, "abstract": abstract})
        idx_map.append(i)
    if not norm:
        return rows
    await translate_to_korean(norm)
    for j, src in enumerate(norm):
        target = rows[idx_map[j]]
        if src.get("title_ko"):
            target["Title_ko"] = src["title_ko"]
        if src.get("abstract_ko"):
            target["Abstract_ko"] = src["abstract_ko"]
    return rows


async def translate_and_overwrite(results: list[dict]) -> list[dict]:
    """Same as `translate_to_korean`, but also overwrites the
    `title` and `abstract` keys with the Korean text when present.

    Used by the agent tools layer so the LLM that synthesizes the
    final answer sees Korean strings directly in the tool result —
    even if the model's render-time instinct is to keep the source
    language verbatim, the context is already Korean.
    """
    await translate_to_korean(results)
    for r in results:
        if r.get("title_ko"):
            r["title"] = r["title_ko"]
        if r.get("abstract_ko"):
            r["abstract"] = r["abstract_ko"]
    return results
