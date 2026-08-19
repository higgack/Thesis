#!/usr/bin/env python3
"""paper_lookup — 실재 학술 문헌 검색 (arXiv + Semantic Scholar, 무키·무의존성).

scientific-agent-skills(K-Dense-AI, MIT)의 'Paper Lookup' 아이디어를 우리 환경에 맞춰
표준 라이브러리(urllib)만으로 경량 구현한 것. LLM 키 불필요.

용도: §2 문헌보강·인용 검증을 위해 **실재 논문**을 찾는다(환각 방지). 결과는 검색일 뿐,
정확한 서지(권/호/쪽)는 Stage 2.5 무결성 게이트에서 1차 출처로 확정한다.

주의: 이 워크스페이스(원격 컨테이너)는 프록시가 arXiv/Semantic Scholar 직접호출을 차단할 수 있다
(그 경우 이 스크립트는 동작 안 함 → WebSearch로 대체). 네트워크 되는 로컬/학생크레딧 환경에서 사용.

사용:
    python rag/paper_lookup.py "hybrid bonding patent analysis" --source both --limit 8
"""
from __future__ import annotations
import sys, json, urllib.parse, urllib.request, xml.etree.ElementTree as ET

UA = {"User-Agent": "paper-lookup/1.0 (academic use)"}


def arxiv(query: str, limit: int = 8):
    url = ("http://export.arxiv.org/api/query?search_query="
           + urllib.parse.quote(f"all:{query}")
           + f"&start=0&max_results={limit}")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        root = ET.fromstring(r.read())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in root.findall("a:entry", ns):
        out.append({
            "source": "arXiv",
            "title": (e.findtext("a:title", default="", namespaces=ns) or "").strip().replace("\n", " "),
            "authors": [a.findtext("a:name", default="", namespaces=ns) for a in e.findall("a:author", ns)],
            "year": (e.findtext("a:published", default="", namespaces=ns) or "")[:4],
            "url": e.findtext("a:id", default="", namespaces=ns),
        })
    return out


def semantic_scholar(query: str, limit: int = 8):
    url = ("https://api.semanticscholar.org/graph/v1/paper/search?query="
           + urllib.parse.quote(query)
           + f"&limit={limit}&fields=title,year,venue,authors,externalIds")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        data = json.load(r)
    out = []
    for p in data.get("data", []):
        doi = (p.get("externalIds") or {}).get("DOI")
        out.append({
            "source": "SemanticScholar",
            "title": p.get("title", ""),
            "authors": [a.get("name", "") for a in (p.get("authors") or [])],
            "year": p.get("year"),
            "venue": p.get("venue"),
            "url": f"https://doi.org/{doi}" if doi else None,
        })
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__); raise SystemExit(2)
    args = sys.argv[1:]
    src = "both"; limit = 8
    if "--source" in args:
        src = args[args.index("--source") + 1]
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    query = args[0]
    results = []
    try:
        if src in ("arxiv", "both"):
            results += arxiv(query, limit)
    except Exception as e:
        print(f"[arXiv 실패: {e}]", file=sys.stderr)
    try:
        if src in ("ss", "semanticscholar", "both"):
            results += semantic_scholar(query, limit)
    except Exception as e:
        print(f"[Semantic Scholar 실패: {e}]", file=sys.stderr)
    for r in results:
        au = ", ".join(r.get("authors") or [])[:80]
        print(f"- [{r['source']}] {r['title']} ({r.get('year')}) — {au}"
              + (f" · {r['venue']}" if r.get('venue') else "")
              + (f"\n  {r['url']}" if r.get('url') else ""))
    print(f"\n총 {len(results)}건. 서지 확정은 1차 출처 대조 후(무결성 게이트).")


if __name__ == "__main__":
    main()
