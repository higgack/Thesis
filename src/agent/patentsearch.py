"""Patent search backends.

Active:
  • KIPRIS Plus (Korean Intellectual Property Office) — free with
    KIPRIS_API_KEY, registered at https://plus.kipris.or.kr →
    활용신청 → 특허/실용신안 정보검색서비스. Korean patents only,
    applicant-name lookup (search_by_applicant) — different shape
    from a free-text search.
  • EPO OPS (European Patent Office Open Patent Services) — free
    4GB/month rolling tier, OAuth 2.0 client_credentials flow.
    Registered at https://developers.epo.org/, requires
    EPO_API_KEY + EPO_API_SECRET in .env. Covers EP / WO / US /
    KR / JP / DE / CN etc. via DOCDB.

Disabled / pending:
  • The Lens — removed. 14-day trial only.
  • USPTO ODP — deferred (ID.me identity-verification overhead).

The global free-text `search()` calls EPO when credentials are
present, otherwise returns [] and the bot shows a "no backend"
message pointing the user at /company_patents (KIPRIS).
"""
import asyncio
import base64
import logging
import os
import time

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

_KIPRIS_API = "http://plus.kipris.or.kr/openapi/rest"
_KIPRIS_APPLICANT_PATH = "/patUtiModInfoSearchSevice/applicantNameSearchInfo"
_KIPRIS_APPNUM_PATH = "/patUtiModInfoSearchSevice/applicationNumberSearchInfo"
_KIPRIS_CITING_PATH = "/CitingService/citingInfo"

_EPO_BASE = "https://ops.epo.org/3.2"
_EPO_TOKEN_URL = f"{_EPO_BASE}/auth/accesstoken"
_EPO_SEARCH_URL = f"{_EPO_BASE}/rest-services/published-data/search/biblio"
# In-process token cache. EPO tokens last ~20 min (expires_in=1200);
# we refresh 60s before expiry so an in-flight request doesn't hit a
# stale token. Single-process bot → asyncio.Lock is enough; no Redis.
_EPO_TOKEN_CACHE: dict = {"token": None, "expires_at": 0.0}
_EPO_TOKEN_LOCK: asyncio.Lock | None = None


def _epo_token_lock() -> asyncio.Lock:
    """Lazy-init lock so module import doesn't require a running
    event loop (some test paths import patentsearch without one)."""
    global _EPO_TOKEN_LOCK
    if _EPO_TOKEN_LOCK is None:
        _EPO_TOKEN_LOCK = asyncio.Lock()
    return _EPO_TOKEN_LOCK


def _empty(p: dict, source: str) -> dict:
    """Normalize a backend row into the shared schema.

    Core fields always present (title, patent_number, date, etc.);
    EPO-specific richer fields (ipc, cpc, family_id, applicant_countries,
    publication_date, application_date, priority_date, kind, kind_label,
    assignees, country, inventors_total) pass through when the backend
    provides them. KIPRIS rows just lack those fields → the formatter
    silently skips empty entries."""
    return {
        "title": (p.get("title") or "").strip(),
        "patent_number": p.get("patent_number") or "",
        "country": p.get("country") or "",
        "kind": p.get("kind") or "",
        "kind_label": p.get("kind_label") or "",
        "date": p.get("date") or "",
        "publication_date": p.get("publication_date") or p.get("date") or "",
        "application_date": p.get("application_date") or "",
        "priority_date": p.get("priority_date") or "",
        "year": p.get("year"),
        "inventors": p.get("inventors") or [],
        "inventors_total": p.get("inventors_total") or len(p.get("inventors") or []),
        "assignee": p.get("assignee") or "",
        "assignees": p.get("assignees") or
                    ([p["assignee"]] if p.get("assignee") else []),
        "applicant_countries": p.get("applicant_countries") or [],
        "abstract": (p.get("abstract") or "").strip(),
        "claims_count": p.get("claims_count"),
        "ipc": p.get("ipc") or [],
        "cpc": p.get("cpc") or [],
        "family_id": p.get("family_id") or "",
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


async def _epo_token() -> str | None:
    """Get a cached or fresh EPO access token via the OAuth 2.0
    client_credentials flow. Returns None when keys are missing or
    the token endpoint fails (bot falls back to []).

    Caching: tokens last ~20 min; we refresh 60 s before expiry so a
    concurrent request doesn't race a stale cache. The lock prevents
    a thundering herd from minting N tokens at the same time."""
    key = os.getenv("EPO_API_KEY", "").strip()
    secret = os.getenv("EPO_API_SECRET", "").strip()
    if not (key and secret):
        return None
    async with _epo_token_lock():
        now = time.time()
        cached = _EPO_TOKEN_CACHE.get("token")
        if cached and _EPO_TOKEN_CACHE["expires_at"] > now + 60:
            return cached
        auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(
                    _EPO_TOKEN_URL,
                    headers={
                        "Authorization": f"Basic {auth}",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                    content="grant_type=client_credentials",
                )
        except Exception as e:
            log.warning("epo token fetch failed: %s", e)
            return None
        if r.status_code != 200:
            log.warning("epo token %d: %r",
                        r.status_code, r.text[:200])
            return None
        try:
            data = r.json()
        except Exception:
            log.warning("epo token JSON decode failed: %r", r.text[:200])
            return None
        token = data.get("access_token")
        if not token:
            return None
        try:
            ttl = int(data.get("expires_in", 1200))
        except (TypeError, ValueError):
            ttl = 1200
        _EPO_TOKEN_CACHE["token"] = token
        _EPO_TOKEN_CACHE["expires_at"] = now + ttl
        return token


def _epo_text(obj) -> str:
    """EPO's JSON wraps scalar values like {'$': 'value'}. Unwrap
    safely whether the input is the wrapper, a bare string, or None."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        v = obj.get("$")
        if isinstance(v, str):
            return v.strip()
    return ""


def _epo_to_unified(doc: dict) -> dict | None:
    """Walk one exchange-document JSON node into the shared schema.
    Returns None when the doc lacks a usable title (silently drop —
    EPO occasionally returns withdrawn / merged docs with stub data).

    EPO's JSON structure is deeply nested + uses ':' in keys
    (ops:world-patent-data, ops:biblio-search). Most fields can be
    either a single dict or a list of dicts depending on count, so
    we normalise to list at every step."""
    try:
        ex_doc = doc.get("exchange-document") or {}
        biblio = ex_doc.get("bibliographic-data") or {}
        # Family ID is a top-level attribute on exchange-document.
        family_id = ex_doc.get("@family-id") or ""
        # ------------------------------------------------------------------
        # Publication reference — docdb has country/number/kind/date.
        # ------------------------------------------------------------------
        pub_ref = (biblio.get("publication-reference") or {}).get(
            "document-id") or []
        if isinstance(pub_ref, dict):
            pub_ref = [pub_ref]
        country = doc_num = kind = pub_date_raw = ""
        for ref in pub_ref:
            if not isinstance(ref, dict):
                continue
            if ref.get("@document-id-type") == "docdb":
                country = _epo_text(ref.get("country"))
                doc_num = _epo_text(ref.get("doc-number"))
                kind = _epo_text(ref.get("kind"))
                pub_date_raw = _epo_text(ref.get("date"))
                break
        # ------------------------------------------------------------------
        # Application reference — filing date (often earlier than pub date).
        # ------------------------------------------------------------------
        app_ref = (biblio.get("application-reference") or {}).get(
            "document-id") or []
        if isinstance(app_ref, dict):
            app_ref = [app_ref]
        app_date_raw = ""
        for ref in app_ref:
            if not isinstance(ref, dict):
                continue
            if ref.get("@document-id-type") == "docdb":
                app_date_raw = _epo_text(ref.get("date"))
                break
        # ------------------------------------------------------------------
        # Priority claims — earliest priority date across all claims.
        # ------------------------------------------------------------------
        priorities = (biblio.get("priority-claims") or {}).get(
            "priority-claim") or []
        if isinstance(priorities, dict):
            priorities = [priorities]
        priority_dates: list[str] = []
        for pc in priorities:
            if not isinstance(pc, dict):
                continue
            pc_ids = pc.get("document-id") or []
            if isinstance(pc_ids, dict):
                pc_ids = [pc_ids]
            for pid in pc_ids:
                if not isinstance(pid, dict):
                    continue
                d = _epo_text(pid.get("date"))
                if d and len(d) == 8:
                    priority_dates.append(d)
                    break
        priority_date_raw = min(priority_dates) if priority_dates else ""
        # ------------------------------------------------------------------
        # Title — prefer English, then any.
        # ------------------------------------------------------------------
        titles = biblio.get("invention-title") or []
        if isinstance(titles, dict):
            titles = [titles]
        title_en = ""
        title_any = ""
        for t in titles:
            if not isinstance(t, dict):
                continue
            txt = _epo_text(t)
            if not txt:
                continue
            if t.get("@lang") == "en":
                title_en = txt
            if not title_any:
                title_any = txt
        title = title_en or title_any
        if not title:
            return None
        # ------------------------------------------------------------------
        # Applicants → assignee column (epodoc human-readable format).
        # Also keep applicant country codes for aggregation by-country.
        # ------------------------------------------------------------------
        parties = biblio.get("parties") or {}
        applicants_raw = (parties.get("applicants") or {}).get(
            "applicant") or []
        if isinstance(applicants_raw, dict):
            applicants_raw = [applicants_raw]
        assignees: list[str] = []
        applicant_countries: list[str] = []
        for a in applicants_raw:
            if not isinstance(a, dict):
                continue
            if a.get("@data-format") != "epodoc":
                continue
            name = _epo_text(
                (a.get("applicant-name") or {}).get("name"))
            if name and name not in assignees:
                assignees.append(name)
            cc = _epo_text(
                (a.get("residence") or {}).get("country"))
            if cc and cc not in applicant_countries:
                applicant_countries.append(cc)
        # ------------------------------------------------------------------
        # Inventors (epodoc).
        # ------------------------------------------------------------------
        inv_raw = (parties.get("inventors") or {}).get("inventor") or []
        if isinstance(inv_raw, dict):
            inv_raw = [inv_raw]
        inventors: list[str] = []
        for i in inv_raw:
            if not isinstance(i, dict):
                continue
            if i.get("@data-format") != "epodoc":
                continue
            name = _epo_text(
                (i.get("inventor-name") or {}).get("name"))
            if name and name not in inventors:
                inventors.append(name)
        # ------------------------------------------------------------------
        # IPC classifications — codes like "H01L21/02". The text field
        # is space-padded: "H01L  21/02       20060101AFI20260315BHEP"
        # so we slice off the prefix (first 14 chars carry the code).
        # ------------------------------------------------------------------
        ipc_raw = (biblio.get("classifications-ipcr") or {}).get(
            "classification-ipcr") or []
        if isinstance(ipc_raw, dict):
            ipc_raw = [ipc_raw]
        ipc_codes: list[str] = []
        for c in ipc_raw:
            if not isinstance(c, dict):
                continue
            text = _epo_text(c.get("text"))
            if not text:
                continue
            # First 14 chars are the section + class + subclass + group/subgroup,
            # space-collapsed.
            code = " ".join(text[:14].split())
            if code and code not in ipc_codes:
                ipc_codes.append(code)
        # CPC (more granular, US/EP harmonised). Optional — biblio may omit.
        cpc_raw = (biblio.get("patent-classifications") or {}).get(
            "patent-classification") or []
        if isinstance(cpc_raw, dict):
            cpc_raw = [cpc_raw]
        cpc_codes: list[str] = []
        for c in cpc_raw:
            if not isinstance(c, dict):
                continue
            if _epo_text(c.get("classification-scheme")
                        if isinstance(c.get("classification-scheme"), dict)
                        else None).upper() != "CPC":
                # Tag-attribute scheme — fall back to checking the
                # 'scheme' element value directly.
                scheme_el = c.get("classification-scheme")
                if isinstance(scheme_el, dict):
                    if (scheme_el.get("@scheme") or "").upper() != "CPC":
                        continue
            section = _epo_text(c.get("section"))
            cls = _epo_text(c.get("class"))
            subcls = _epo_text(c.get("subclass"))
            mg = _epo_text(c.get("main-group"))
            sg = _epo_text(c.get("subgroup"))
            if section and cls and subcls:
                code = f"{section}{cls}{subcls}{mg}/{sg}" if mg and sg else \
                       f"{section}{cls}{subcls}"
                if code not in cpc_codes:
                    cpc_codes.append(code)
        # ------------------------------------------------------------------
        # Abstract — prefer English.
        # ------------------------------------------------------------------
        abstract = ""
        abs_raw = ex_doc.get("abstract") or []
        if isinstance(abs_raw, dict):
            abs_raw = [abs_raw]
        for a in abs_raw:
            if not isinstance(a, dict):
                continue
            ps = a.get("p") or []
            if isinstance(ps, (dict, str)):
                ps = [ps]
            parts = [_epo_text(p) for p in ps if p]
            joined = " ".join(p for p in parts if p)
            if a.get("@lang") == "en":
                abstract = joined
                break
            if not abstract:
                abstract = joined

        def _fmt(d: str) -> str:
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else ""

        pub_date = _fmt(pub_date_raw)
        app_date = _fmt(app_date_raw)
        priority_date = _fmt(priority_date_raw)
        year = int(pub_date_raw[:4]) if len(pub_date_raw) >= 4 \
            and pub_date_raw[:4].isdigit() else None
        patent_number = (f"{country}{doc_num}{kind}"
                         if country and doc_num else "")
        url = (f"https://patents.google.com/patent/{patent_number}"
               if patent_number else "")
        # Kind code → human-readable status hint.
        kind_label = {
            "A": "공개", "A1": "출원공개", "A2": "출원공개(재발행)",
            "A9": "정정공개", "B1": "등록", "B2": "등록(재발행)",
            "C": "심사완료", "U": "실용신안",
        }.get(kind, "")
        return _empty({
            "title": title,
            "patent_number": patent_number,
            "country": country,
            "kind": kind,
            "kind_label": kind_label,
            "date": pub_date,            # publication date (legacy field)
            "publication_date": pub_date,
            "application_date": app_date,
            "priority_date": priority_date,
            "year": year,
            "inventors": inventors[:5],
            "inventors_total": len(inventors),
            "assignee": ", ".join(assignees[:3]),
            "assignees": assignees,
            "applicant_countries": applicant_countries,
            "abstract": abstract.strip(),
            "claims_count": None,
            "ipc": ipc_codes[:8],        # cap for readability
            "cpc": cpc_codes[:8],
            "family_id": family_id,
            "url": url,
        }, "EPO")
    except Exception:
        log.exception("epo doc parse failed (skipping row)")
        return None


async def _epo_search(query: str, limit: int) -> list[dict]:
    """EPO OPS free-text biblio search.

    Uses CQL: txt=<term> spans title + abstract + claims.
    Multi-word queries get wrapped in quotes so they're matched as a
    phrase (otherwise CQL treats whitespace as implicit AND, which
    over-narrows on common multi-word inputs like "hybrid bonding").
    Range header caps per-call results (EPO max=100; we cap at 25 to
    stay well inside the 4GB/month free tier even at heavy use)."""
    token = await _epo_token()
    if not token:
        return []
    q = query.strip()
    if not q:
        return []
    cql = f'txt="{q}"' if " " in q else f"txt={q}"
    end = min(max(limit, 1), 25)
    range_hdr = f"1-{end}"

    async def _call(tok: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30,
                                     follow_redirects=True) as c:
            return await c.get(
                _EPO_SEARCH_URL,
                headers={
                    "Authorization": f"Bearer {tok}",
                    "Accept": "application/json",
                    "X-OPS-Range": range_hdr,
                },
                params={"q": cql, "Range": range_hdr},
            )

    try:
        r = await _call(token)
        # 401 = token expired between cache check and request — invalidate
        # and retry once. EPO occasionally rotates tokens early.
        if r.status_code == 401:
            log.info("epo 401, refreshing token and retrying once")
            _EPO_TOKEN_CACHE["expires_at"] = 0
            token2 = await _epo_token()
            if not token2:
                return []
            r = await _call(token2)
        if r.status_code != 200:
            log.warning("epo search %d: %r",
                        r.status_code, r.text[:300])
            return []
        data = r.json()
    except Exception as e:
        log.warning("epo search failed: %s", e)
        return []
    # Navigate the ops:world-patent-data > ops:biblio-search >
    # ops:search-result > exchange-documents path. Each level can be
    # absent on empty results.
    try:
        wpd = data.get("ops:world-patent-data") or {}
        result = (wpd.get("ops:biblio-search") or {}).get(
            "ops:search-result") or {}
        docs = result.get("exchange-documents") or []
        if isinstance(docs, dict):
            docs = [docs]
    except Exception:
        log.warning("epo response shape unexpected: %r", str(data)[:300])
        return []
    out: list[dict] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        row = _epo_to_unified(doc)
        if row:
            out.append(row)
        if len(out) >= limit:
            break
    return out


def has_global_backend() -> bool:
    """True when EPO OPS credentials are present. Used by the bot to
    show a "no backend" message on /search_patents instead of a
    silent empty result when keys are missing."""
    return bool(os.getenv("EPO_API_KEY", "").strip()
                and os.getenv("EPO_API_SECRET", "").strip())


async def search(query: str, limit: int = 15) -> list[dict]:
    """Free-text global patent search entry point.

    Currently routes to EPO OPS only. If keys are missing, returns
    [] and the bot falls back to a "no global backend" message that
    points the user at /company_patents (KIPRIS) for KR queries.

    Korean company patent lookup goes through search_by_applicant()
    (KIPRIS), wired up regardless of global-backend status."""
    if not has_global_backend():
        log.info(
            "patent search: no global backend active "
            "(EPO_API_KEY/EPO_API_SECRET missing). Returning [] for %r.",
            query[:60],
        )
        return []
    try:
        return await _epo_search(query, limit)
    except Exception as e:
        log.warning("patent search EPO failed (%s)", type(e).__name__)
        return []


# ---------------------------------------------------------------------------
# Advanced EPO search — builds a CQL query from structured filters so
# the bot can offer applicant / IPC / country / year refinements via
# /search_patents_advanced. CQL operators: AND / OR / NOT, fields:
# txt (text), pa (applicant), in (inventor), ic (IPC), cl (country),
# pd (publication date YYYY/YYYYMMDD).
# ---------------------------------------------------------------------------


def _build_cql(query: str, applicant: str = "", inventor: str = "",
               ipc: str = "", country: str = "",
               year_from: int | None = None,
               year_to: int | None = None) -> str:
    """Compose a CQL string from optional filters. The text part is
    required; everything else is AND-joined when present."""
    parts: list[str] = []
    q = query.strip()
    if q:
        parts.append(f'txt="{q}"' if " " in q else f"txt={q}")
    if applicant:
        parts.append(f'pa="{applicant.strip()}"')
    if inventor:
        parts.append(f'in="{inventor.strip()}"')
    if ipc:
        parts.append(f"ic={ipc.strip()}")
    if country:
        parts.append(f"cl={country.strip().upper()}")
    if year_from and year_to:
        parts.append(f"pd within \"{year_from} {year_to}\"")
    elif year_from:
        parts.append(f"pd>={year_from}")
    elif year_to:
        parts.append(f"pd<={year_to}")
    return " AND ".join(parts) if parts else ""


async def search_advanced(query: str, limit: int = 15, applicant: str = "",
                          inventor: str = "", ipc: str = "",
                          country: str = "",
                          year_from: int | None = None,
                          year_to: int | None = None) -> list[dict]:
    """EPO OPS search with structured filters."""
    if not has_global_backend():
        return []
    cql = _build_cql(query, applicant=applicant, inventor=inventor,
                     ipc=ipc, country=country,
                     year_from=year_from, year_to=year_to)
    if not cql:
        return []
    token = await _epo_token()
    if not token:
        return []
    end = min(max(limit, 1), 25)
    range_hdr = f"1-{end}"
    try:
        async with httpx.AsyncClient(timeout=30,
                                     follow_redirects=True) as c:
            r = await c.get(
                _EPO_SEARCH_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "X-OPS-Range": range_hdr,
                },
                params={"q": cql, "Range": range_hdr},
            )
        if r.status_code != 200:
            log.warning("epo advanced %d: %r",
                        r.status_code, r.text[:300])
            return []
        data = r.json()
    except Exception as e:
        log.warning("epo advanced failed: %s", e)
        return []
    try:
        wpd = data.get("ops:world-patent-data") or {}
        result = (wpd.get("ops:biblio-search") or {}).get(
            "ops:search-result") or {}
        docs = result.get("exchange-documents") or []
        if isinstance(docs, dict):
            docs = [docs]
    except Exception:
        return []
    out: list[dict] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        row = _epo_to_unified(doc)
        if row:
            out.append(row)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Bulk fetch for aggregate analytics. EPO allows Range=1-100 per
# call; we paginate up to max_count to assemble a statistically
# meaningful sample. 400 docs ≈ 200 KB ≈ 0.005% of the 4GB/month
# free tier — even called daily this is negligible.
# ---------------------------------------------------------------------------


async def _epo_search_bulk(query: str, max_count: int = 400) -> list[dict]:
    """Fetch up to max_count patents matching the query across multiple
    Range requests (100 per call). Returns the deduplicated unified
    rows; rows that fail to parse are silently skipped."""
    if not has_global_backend():
        return []
    token = await _epo_token()
    if not token:
        return []
    q = query.strip()
    if not q:
        return []
    cql = f'txt="{q}"' if " " in q else f"txt={q}"
    max_count = max(1, min(max_count, 2000))
    out: list[dict] = []
    seen: set[str] = set()
    cursor = 1
    page_size = 100
    while cursor <= max_count:
        end = min(cursor + page_size - 1, max_count)
        range_hdr = f"{cursor}-{end}"
        try:
            async with httpx.AsyncClient(timeout=40,
                                         follow_redirects=True) as c:
                r = await c.get(
                    _EPO_SEARCH_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "X-OPS-Range": range_hdr,
                    },
                    params={"q": cql, "Range": range_hdr},
                )
        except Exception as e:
            log.warning("epo bulk page %s failed: %s", range_hdr, e)
            break
        if r.status_code == 404:
            # EPO returns 404 when the requested range overshoots the
            # total result count — totally fine for the last page.
            break
        if r.status_code == 401:
            _EPO_TOKEN_CACHE["expires_at"] = 0
            token = await _epo_token()
            if not token:
                break
            continue
        if r.status_code != 200:
            log.warning("epo bulk %s: %d %r",
                        range_hdr, r.status_code, r.text[:200])
            break
        try:
            data = r.json()
        except Exception:
            log.warning("epo bulk %s JSON decode failed", range_hdr)
            break
        try:
            wpd = data.get("ops:world-patent-data") or {}
            result = (wpd.get("ops:biblio-search") or {}).get(
                "ops:search-result") or {}
            docs = result.get("exchange-documents") or []
            if isinstance(docs, dict):
                docs = [docs]
        except Exception:
            break
        if not docs:
            break
        added = 0
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            row = _epo_to_unified(doc)
            if not row:
                continue
            key = row.get("patent_number") or row.get("family_id") or ""
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append(row)
            added += 1
        if added == 0 or end >= max_count:
            break
        cursor = end + 1
    return out


# ---------------------------------------------------------------------------
# Pure aggregation helpers — no I/O, work off an already-fetched
# patent list. Each returns a small dict/list shape ready for the
# bot's text formatter.
# ---------------------------------------------------------------------------


def _norm_assignee(name: str) -> str:
    """Collapse minor name variants ('Samsung Electronics Co Ltd' /
    'SAMSUNG ELECTRONICS CO., LTD.') so a single org doesn't get
    split across the leaderboard. Light-touch only — strip suffixes
    + case-fold, no fuzzy matching."""
    if not name:
        return ""
    s = name.strip()
    # Strip common corporate suffixes that fragment counts
    suffixes = [
        " CO LTD", " CO., LTD.", " CO.,LTD.", " CO.,LTD", " CO LTD.",
        " CO.,LIMITED", " CO., LIMITED",
        " LTD.", " LTD", " LIMITED", " LIMITED.",
        " INC.", " INC", " INCORPORATED",
        " GMBH", " GMBH.", " AG", " SA", " S.A.", " B.V.", " BV",
        " LLC", " L.L.C.", " CORPORATION", " CORP.", " CORP",
        " COMPANY", " ENTERPRISES",
    ]
    upper = s.upper()
    for sfx in suffixes:
        if upper.endswith(sfx):
            s = s[:len(s) - len(sfx)].strip()
            upper = s.upper()
    # Display in UPPERCASE — corporate leaderboards read naturally
    # in caps (TSMC / SAMSUNG ELECTRONICS / SK HYNIX), and using the
    # uppercase form as both display and group key avoids the
    # "Sk Hynix" / "Tsmc" artefacts that .title() produces on
    # acronyms.
    return upper.strip()


def compute_patent_stats(patents: list[dict]) -> dict:
    """Aggregate-by-everything overview: counts by applicant, country,
    year, IPC. Returns a stats dict with top-N lists ready for render."""
    from collections import Counter
    from_country = Counter()
    by_applicant = Counter()
    by_year = Counter()
    by_ipc = Counter()
    by_kind = Counter()
    for p in patents:
        cc = p.get("country") or ""
        if cc:
            from_country[cc] += 1
        for asg in (p.get("assignees") or []):
            norm = _norm_assignee(asg)
            if norm:
                by_applicant[norm] += 1
        yr = p.get("year")
        if yr:
            by_year[yr] += 1
        for ipc in (p.get("ipc") or []):
            # Group at subclass level (H01L21/02 → H01L21) for cleaner buckets
            head = ipc.split("/")[0].strip()
            if head:
                by_ipc[head] += 1
        kind = p.get("kind") or ""
        if kind:
            by_kind[kind] += 1
    return {
        "total": len(patents),
        "by_applicant": by_applicant.most_common(15),
        "by_country": from_country.most_common(10),
        "by_year": sorted(by_year.items(), reverse=True),
        "by_ipc": by_ipc.most_common(10),
        "by_kind": by_kind.most_common(10),
    }


def compute_patent_trend(patents: list[dict],
                         top_applicants: int = 5) -> dict:
    """Year-over-year time series for the top-N applicants. Returns
    {'years': [...], 'series': {applicant_name: [count_per_year]}} —
    ready to feed into a mermaid xychart or text bar grid."""
    from collections import Counter, defaultdict
    counts = Counter()
    for p in patents:
        for asg in (p.get("assignees") or []):
            norm = _norm_assignee(asg)
            if norm:
                counts[norm] += 1
    top = [name for name, _ in counts.most_common(top_applicants)]
    years_set: set[int] = set()
    grid: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for p in patents:
        yr = p.get("year")
        if not yr:
            continue
        years_set.add(yr)
        for asg in (p.get("assignees") or []):
            norm = _norm_assignee(asg)
            if norm in top:
                grid[norm][yr] += 1
    years = sorted(years_set)
    series = {name: [grid[name].get(y, 0) for y in years] for name in top}
    return {"years": years, "series": series, "top_applicants": top}


def compute_patent_newcomers(patents: list[dict],
                             cutoff_months: int = 12) -> list[dict]:
    """Applicants whose FIRST publication in this corpus is within
    `cutoff_months`. Useful for spotting new entrants into the field.
    Returns [{name, first_year, total_count}, ...]."""
    from collections import defaultdict
    from datetime import datetime
    cutoff_year = datetime.utcnow().year - (cutoff_months // 12)
    cutoff_month = datetime.utcnow().month
    first_dates: dict[str, str] = {}  # YYYYMMDD-ish
    totals: dict[str, int] = defaultdict(int)
    for p in patents:
        pub = (p.get("publication_date") or "").replace("-", "")
        if not pub:
            continue
        for asg in (p.get("assignees") or []):
            norm = _norm_assignee(asg)
            if not norm:
                continue
            totals[norm] += 1
            cur = first_dates.get(norm)
            if cur is None or pub < cur:
                first_dates[norm] = pub
    newcomers: list[dict] = []
    for name, first in first_dates.items():
        if len(first) < 6:
            continue
        fy, fm = int(first[:4]), int(first[4:6])
        # within cutoff months of "now"
        months_ago = (datetime.utcnow().year - fy) * 12 + \
                     (datetime.utcnow().month - fm)
        if months_ago <= cutoff_months:
            newcomers.append({
                "name": name,
                "first_date": f"{first[:4]}-{first[4:6]}-{first[6:8]}"
                              if len(first) == 8 else first[:4],
                "total": totals[name],
            })
    newcomers.sort(key=lambda x: x["total"], reverse=True)
    return newcomers[:20]


def compute_patent_coapplicants(patents: list[dict]) -> list[dict]:
    """Pairs of applicants who appear together on the same patent.
    Surfaces collaboration / joint-development relationships."""
    from collections import Counter
    from itertools import combinations
    pair_counts: Counter = Counter()
    for p in patents:
        assignees = sorted({_norm_assignee(a)
                            for a in (p.get("assignees") or []) if a})
        if len(assignees) < 2:
            continue
        for a, b in combinations(assignees, 2):
            if not (a and b):
                continue
            pair_counts[(a, b)] += 1
    return [{"a": a, "b": b, "count": c}
            for (a, b), c in pair_counts.most_common(20)]


async def extract_patent_keywords(patents: list[dict],
                                  max_phrases: int = 30) -> list[str]:
    """Use Gemini Flash-Lite to mine technical noun phrases from the
    abstracts of the supplied patents. Single batched call (~₩3-5),
    returns deduplicated phrase list sorted by frequency-like signal."""
    from .. import config
    from ..llm.gemini import complete
    import json as _json
    import re as _re
    if not patents:
        return []
    snippets = []
    for i, p in enumerate(patents[:80], 1):
        ab = (p.get("abstract") or "")[:600]
        if ab:
            snippets.append(f"[{i}] {ab}")
    if not snippets:
        return []
    user = "\n".join(snippets)
    system = (
        "You are a technical-domain keyword extractor. Read the patent "
        "abstracts below and return the most representative technical "
        "noun phrases (concepts, methods, materials, structures). "
        "Prefer multi-word phrases over single words. Preserve original "
        "casing for acronyms (HBM, CMP, Cu-Cu, FCBGA). Output JSON only:\n"
        '{"keywords": ["phrase1", "phrase2", ...]}'
        f"\nReturn at most {max_phrases} phrases."
    )
    try:
        resp = await complete(
            model=config.SUMMARY_MODEL,
            system=system,
            user=user,
            max_tokens=2048,
            temperature=0.2,
            purpose="patent_keywords",
        )
        m = _re.search(r"\{.*\}", resp, _re.DOTALL)
        if not m:
            return []
        data = _json.loads(m.group(0))
    except Exception:
        log.exception("patent keyword extraction failed")
        return []
    out: list[str] = []
    seen: set[str] = set()
    for kw in (data.get("keywords") or []):
        kw_clean = (kw or "").strip()
        if not kw_clean:
            continue
        key = kw_clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(kw_clean)
        if len(out) >= max_phrases:
            break
    return out
