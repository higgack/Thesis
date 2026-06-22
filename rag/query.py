#!/usr/bin/env python3
"""Query the RAG-Anything index built by ingest.py.

Usage:
    python rag/query.py "your question"
    python rag/query.py --mode hybrid "your question"

Modes: hybrid (default), local, global, naive. See RAG-Anything / LightRAG docs.
"""

from __future__ import annotations

import asyncio
import sys

from _common import build_rag


async def main(question: str, mode: str) -> None:
    rag = build_rag()
    answer = await rag.aquery(question, mode=mode)
    print(answer)


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = "hybrid"
    if args and args[0] == "--mode":
        if len(args) < 3:
            print(__doc__)
            raise SystemExit(2)
        mode = args[1]
        args = args[2:]
    if not args:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(main(" ".join(args), mode))
