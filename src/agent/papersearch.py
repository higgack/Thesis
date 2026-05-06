import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

_API = "https://api.semanticscholar.org/graph/v1/paper/search"
_FIELDS = "title,abstract,authors.name,year,venue,openAccessPdf,externalIds,url"


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
async def search(query: str, limit: int = 5) -> list[dict]:
    params = {"query": query, "limit": limit, "fields": _FIELDS}
    async with httpx.AsyncClient(timeout=20,
                                 headers={"User-Agent": "SecondBrainBot"}) as c:
        r = await c.get(_API, params=params)
        r.raise_for_status()
    out = []
    for p in r.json().get("data", []):
        ex = p.get("externalIds") or {}
        arxiv = ex.get("ArXiv")
        pdf = (p.get("openAccessPdf") or {}).get("url")
        link = (
            f"https://arxiv.org/abs/{arxiv}" if arxiv
            else (p.get("url") or pdf or "")
        )
        out.append({
            "title": p.get("title", "").strip(),
            "year": p.get("year"),
            "venue": p.get("venue"),
            "authors": [a.get("name") for a in (p.get("authors") or [])][:3],
            "abstract": (p.get("abstract") or "").strip(),
            "url": link,
            "pdf": pdf,
            "arxiv": arxiv,
        })
    return out
