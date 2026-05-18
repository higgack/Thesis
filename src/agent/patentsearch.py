"""Patent search backends.

Active:
  • KIPRIS Plus (Korean Intellectual Property Office) — free with
    KIPRIS_API_KEY, registered at https://plus.kipris.or.kr →
    활용신청 → 특허/실용신안 정보검색서비스. Korean patents only,
    applicant-name lookup (search_by_applicant) — different shape
    from a free-text search.

Disabled / pending:
  • The Lens — removed. 14-day trial only, institutional access
    required ITK subscription approval which never came through.
    Per user policy (5/17) we don't keep paid backends around.
  • EPO OPS — user registered 5/16, account activation pending
    (1-2 business days, then OAuth + XML parsing). Will be added
    here once credentials land.
  • USPTO ODP — deferred (ID.me identity-verification overhead).

The global free-text `search()` entry point therefore returns []
right now; `/search_patents <keyword>` shows a message pointing to
/company_patents instead. When EPO OPS activates we drop the EPO
backend into `search()` here without touching the bot layer.
"""
import logging
import os

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

_KIPRIS_API = "http://plus.kipris.or.kr/openapi/rest"
_KIPRIS_APPLICANT_PATH = "/patUtiModInfoSearchSevice/applicantNameSearchInfo"
_KIPRIS_APPNUM_PATH = "/patUtiModInfoSearchSevice/applicationNumberSearchInfo"
_KIPRIS_CITING_PATH = "/CitingService/citingInfo"


def _empty(p: dict, source: str) -> dict:
    """Normalize a backend row into the shared schema."""
    return {
        "title": (p.get("title") or "").strip(),
        "patent_number": p.get("patent_number") or "",
        "date": p.get("date") or "",
        "year": p.get("year"),
        "inventors": p.get("inventors") or [],
        "assignee": p.get("assignee") or "",
        "abstract": (p.get("abstract") or "").strip(),
        "claims_count": p.get("claims_count"),
        "url": p.get("url") or "",
        "source": source,
    }


def _kipris_to_unified(item) -> dict:
    """Map a KIPRIS PatentUtilityInfo XML node into our shared schema."""
    def _t(tag: str) -> str:
        child = item.find(tag)
        if child is None or not child.text:
            return ""
        return child.text.strip()

    app_num = _t("ApplicationNumber")
    title = _t("InventionName")
    app_date_raw = _t("ApplicationDate")  # YYYYMMDD
    applicant = _t("Applicant")
    reg_num = _t("RegistrationNumber")
    reg_date_raw = _t("RegistrationDate")
    open_num = _t("OpeningNumber")
    open_date_raw = _t("OpeningDate")

    def _fmt_date(yyyymmdd: str) -> str:
        if len(yyyymmdd) == 8 and yyyymmdd.isdigit():
            return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
        return yyyymmdd

    # Show registration date when granted, otherwise application date.
    primary_raw = reg_date_raw or open_date_raw or app_date_raw
    date = _fmt_date(primary_raw)
    year = None
    if len(primary_raw) >= 4 and primary_raw[:4].isdigit():
        year = int(primary_raw[:4])

    # patent_number = registered KR number if granted, else KR-A
    # (published / application). Google Patents resolves both forms.
    if reg_num:
        patent_number = f"KR{reg_num}"
        url = f"https://patents.google.com/patent/KR{reg_num}"
    elif open_num:
        patent_number = f"KR{open_num}A"
        url = f"https://patents.google.com/patent/KR{open_num}A"
    else:
        patent_number = f"KR-출원{app_num}" if app_num else ""
        # KIPRIS biblio frame works for raw application numbers
        # (this is the KIPRIS web detail page when Google Patents has
        # no match yet).
        url = (
            f"https://kpat.kipris.or.kr/kpat/biblioa.do?"
            f"method=biblioFrame&applno={app_num}"
        ) if app_num else ""

    return _empty({
        "title": title,
        "patent_number": patent_number,
        "date": date,
        "year": year,
        # KIPRIS applicantNameSearchInfo doesn't return inventor or
        # abstract in the list response — only application number,
        # title, applicant, dates, status. We could fetch detail per
        # patent (applicationNumberSearchInfo + Abstract) but that's
        # N extra round trips — defer until we see real demand.
        "inventors": [],
        "assignee": applicant,
        "abstract": "",
        "claims_count": None,
        "url": url,
    }, "KIPRIS")


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
async def _kipris_by_applicant(applicant: str, limit: int) -> list[dict]:
    """Search Korean patents by applicant name (출원인) via KIPRIS Plus.

    Free, requires KIPRIS_API_KEY (register at https://plus.kipris.or.kr
    → 활용신청 → 특허/실용신안 정보검색서비스). Silently returns []
    when no key is set so the bot keeps working.

    Backend used by /company_patents and by the agent when a question
    targets a specific Korean entity (삼성전자 / SK하이닉스 etc.).
    Returns most-recent-first.
    """
    key = os.getenv("KIPRIS_API_KEY", "").strip()
    if not key:
        log.info("kipris: no KIPRIS_API_KEY set, skipping")
        return []
    params = {
        "applicant": applicant,
        "docsStart": "1",
        # KIPRIS allows up to 500 per page; we cap at 30 to keep the
        # XML response small + give the user a curated top slice.
        "docsCount": str(min(limit, 30)),
        "patent": "true",
        "utility": "false",
        # status: A=공개, R=등록, J=거절, 빈값=전체. Pull everything;
        # _kipris_to_unified surfaces the latest published/granted
        # form so the user sees what actually exists.
        "lastvalue": "",
        "accessKey": key,
    }
    import xml.etree.ElementTree as _ET
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "SecondBrainBot"}) as c:
        r = await c.get(_KIPRIS_API + _KIPRIS_APPLICANT_PATH, params=params)
        if r.status_code != 200:
            log.warning(
                "kipris %d on applicant search — key_prefix=%r body=%r",
                r.status_code, key[:8], r.text[:300].replace("\n", " "),
            )
            r.raise_for_status()
    try:
        root = _ET.fromstring(r.content)
    except _ET.ParseError as e:
        log.warning("kipris XML parse failed: %s", e)
        return []
    out = []
    for item in root.findall(".//PatentUtilityInfo"):
        try:
            out.append(_kipris_to_unified(item))
        except Exception:
            log.exception("kipris row parse failed (skipping)")
    return out


async def search_by_applicant(applicant: str, limit: int = 15) -> list[dict]:
    """Korean company patent lookup via KIPRIS.

    Separate entry point from search() because the input is an
    applicant name (회사명), not a free-text query — KIPRIS's
    applicantNameSearchInfo endpoint doesn't do keyword matching
    against the title/abstract.

    Returns [] on backend failure so callers (bot command, agent
    tool) can render an empty-result message instead of crashing.
    """
    try:
        return await _kipris_by_applicant(applicant, limit)
    except Exception as e:
        log.warning("patent search_by_applicant kipris failed (%s)",
                    type(e).__name__)
        return []


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
async def _kipris_patent_detail(application_number: str) -> dict | None:
    """Fetch a single KIPRIS patent's detail block by application
    number. Returns the unified-schema dict (incl. abstract) so the
    bot can deep-link from /company_patents results."""
    key = os.getenv("KIPRIS_API_KEY", "").strip()
    if not key:
        log.info("kipris: no KIPRIS_API_KEY set, skipping detail fetch")
        return None
    params = {
        "applicationNumber": application_number,
        "docsStart": "1",
        "accessKey": key,
    }
    import xml.etree.ElementTree as _ET
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "SecondBrainBot"}) as c:
        r = await c.get(_KIPRIS_API + _KIPRIS_APPNUM_PATH, params=params)
        if r.status_code != 200:
            log.warning(
                "kipris %d on detail — body=%r",
                r.status_code, r.text[:300].replace("\n", " "),
            )
            r.raise_for_status()
    try:
        root = _ET.fromstring(r.content)
    except _ET.ParseError as e:
        log.warning("kipris detail XML parse failed: %s", e)
        return None
    item = root.find(".//PatentUtilityInfo")
    if item is None:
        return None
    row = _kipris_to_unified(item)
    # The detail endpoint returns Abstract + IPC fields not present
    # in the applicant-search list response. Patch them in.
    abstract_el = item.find("Abstract")
    if abstract_el is not None and abstract_el.text:
        row["abstract"] = abstract_el.text.strip()
    ipc_el = item.find("InternationalpatentclassificationNumber")
    if ipc_el is not None and ipc_el.text:
        # IPC class stuffed into the venue slot so the existing
        # formatter ("· venue ·") renders it.
        row["venue"] = ipc_el.text.strip()
    return row


async def get_patent_detail(application_number: str) -> dict | None:
    """Public entry — single-patent detail by KR application number.
    Returns None on failure / missing key so callers can fall back."""
    try:
        return await _kipris_patent_detail(application_number)
    except Exception as e:
        log.warning("patent_detail kipris failed (%s)",
                    type(e).__name__)
        return None


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
async def _kipris_citing(application_number: str) -> list[dict]:
    """Patents that CITE the given application number. KIPRIS
    Citing API returns lightweight rows (target's app#, target's
    status, citation literature type). We map each into the shared
    schema so the bot can render them via _format_patents_text."""
    key = os.getenv("KIPRIS_API_KEY", "").strip()
    if not key:
        log.info("kipris: no KIPRIS_API_KEY set, skipping citing fetch")
        return []
    params = {
        "standardCitationApplicationNumber": application_number,
        "accessKey": key,
    }
    import xml.etree.ElementTree as _ET
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "SecondBrainBot"}) as c:
        r = await c.get(_KIPRIS_API + _KIPRIS_CITING_PATH, params=params)
        if r.status_code != 200:
            log.warning(
                "kipris %d on citing — body=%r",
                r.status_code, r.text[:300].replace("\n", " "),
            )
            r.raise_for_status()
    try:
        root = _ET.fromstring(r.content)
    except _ET.ParseError as e:
        log.warning("kipris citing XML parse failed: %s", e)
        return []
    out = []
    for item in root.findall(".//citingInfo"):
        def _t(tag: str) -> str:
            child = item.find(tag)
            if child is None or not child.text:
                return ""
            return child.text.strip()
        citing_app = _t("ApplicationNumber")
        status_name = _t("StandardStatusCodeName")
        cite_type = _t("CitationLiteratureTypeCodeName")
        if not citing_app:
            continue
        # Build a thin unified row — title/abstract not in citing
        # response (would need a second detail fetch per row).
        url = (
            f"https://kpat.kipris.or.kr/kpat/biblioa.do?"
            f"method=biblioFrame&applno={citing_app}"
        )
        out.append(_empty({
            "title": f"[인용 특허] {status_name or '상태 미상'}",
            "patent_number": f"KR-출원{citing_app}",
            "date": "",
            "year": None,
            "inventors": [],
            "assignee": cite_type or "",
            "abstract": "",
            "claims_count": None,
            "url": url,
        }, "KIPRIS"))
    return out


async def get_citing_patents(application_number: str) -> list[dict]:
    """Public entry — patents that cite the given KR application.
    Returns [] on failure / missing key."""
    try:
        return await _kipris_citing(application_number)
    except Exception as e:
        log.warning("citing_patents kipris failed (%s)",
                    type(e).__name__)
        return []


def has_global_backend() -> bool:
    """True when at least one global free-text patent backend is
    configured. Used by the bot to show a helpful "no backend yet"
    message on /search_patents instead of a silent empty result.
    EPO OPS will flip this to True once OAuth credentials are in
    .env (EPO_API_KEY + EPO_API_SECRET)."""
    # Future: return bool(os.getenv("EPO_API_KEY")) or
    #         bool(os.getenv("USPTO_ODP_API_KEY"))
    return False


async def search(query: str, limit: int = 15) -> list[dict]:
    """Free-text patent search entry point.

    Currently returns [] — Lens was removed (paid trial only), EPO
    OPS pending account activation, USPTO ODP deferred (ID.me
    overhead). When EPO comes online we plug its async function in
    here behind a `try: await _epo(...) except: []` block matching
    papersearch.search()'s pattern.

    Korean company patent lookup goes through search_by_applicant()
    (KIPRIS), which is wired up regardless of global-backend status.
    """
    log.info(
        "patent search: no global backend active "
        "(Lens removed, EPO OPS pending). Returning [] for %r.",
        query[:60],
    )
    return []
