from notion_client import AsyncClient
from .. import config

_client = AsyncClient(auth=config.NOTION_TOKEN) if config.NOTION_TOKEN else None


def enabled() -> bool:
    return _client is not None and config.NOTION_DATABASE_ID is not None


def _chunk_text(s: str, n: int = 1900) -> list[str]:
    return [s[i:i + n] for i in range(0, len(s), n)] or [""]


async def create_page(title: str, source: str, doc_type: str,
                      summary: str, doc_id: str, body: str) -> str | None:
    if not enabled():
        return None
    props = {
        "Title": {"title": [{"text": {"content": title[:200]}}]},
        "Source": {"url": source if source.startswith("http") else None},
        "Type": {"select": {"name": doc_type}},
        "Summary": {"rich_text": [{"text": {"content": summary[:1990]}}]},
        "Doc ID": {"rich_text": [{"text": {"content": doc_id}}]},
        "Ingested": {"date": {"start": _today()}},
    }
    children = [
        {"object": "block", "type": "heading_2",
         "heading_2": {"rich_text": [{"text": {"content": "Summary"}}]}},
        *[_para(p) for p in _chunk_text(summary)],
        {"object": "block", "type": "heading_2",
         "heading_2": {"rich_text": [{"text": {"content": "Original"}}]}},
        *[_para(p) for p in _chunk_text(body)[:90]],
    ]
    page = await _client.pages.create(
        parent={"database_id": config.NOTION_DATABASE_ID},
        properties=props,
        children=children,
    )
    return page["id"]


def _para(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": text}}]}}


def _today() -> str:
    from datetime import date
    return date.today().isoformat()
