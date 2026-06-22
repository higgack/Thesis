"""Shared RAGAnything setup for ingest.py and query.py.

Builds a configured RAGAnything instance from environment variables (rag/.env).
Kept deliberately small — see rag/README.md. RAG-Anything is an external
dependency (pip install -r rag/requirements.txt), not vendored here.
"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "python-dotenv is missing. Run: pip install -r rag/requirements.txt"
        ) from exc
    load_dotenv(ENV_PATH)


def require(name: str) -> str:
    value = os.getenv(name)
    if not value or value.startswith("your_"):
        raise SystemExit(
            f"{name} is not set. Copy rag/.env.example to rag/.env and fill it in."
        )
    return value


def build_rag():
    """Return a configured RAGAnything instance."""
    _load_env()

    try:
        from raganything import RAGAnything, RAGAnythingConfig
        from lightrag.llm.openai import openai_complete_if_cache, openai_embed
        from lightrag.utils import EmbeddingFunc
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "raganything is missing. Run: pip install -r rag/requirements.txt"
        ) from exc

    api_key = require("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL") or None
    llm_model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    vision_model = os.getenv("VISION_MODEL", "gpt-4o")
    embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
    embedding_dim = int(os.getenv("EMBEDDING_DIM", "3072"))
    working_dir = os.getenv("WORKING_DIR", str(REPO_ROOT / "rag" / "rag_storage"))

    config = RAGAnythingConfig(
        working_dir=working_dir,
        parser=os.getenv("PARSER", "mineru"),
        parse_method=os.getenv("PARSE_METHOD", "auto"),
        enable_image_processing=True,
        enable_table_processing=True,
        enable_equation_processing=True,
    )

    def llm_model_func(prompt, system_prompt=None, history_messages=None, **kwargs):
        return openai_complete_if_cache(
            llm_model,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            api_key=api_key,
            base_url=base_url,
            **kwargs,
        )

    def vision_model_func(prompt, system_prompt=None, history_messages=None,
                          image_data=None, **kwargs):
        return openai_complete_if_cache(
            vision_model,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            image_data=image_data,
            api_key=api_key,
            base_url=base_url,
            **kwargs,
        )

    embedding_func = EmbeddingFunc(
        embedding_dim=embedding_dim,
        max_token_size=8192,
        func=partial(
            openai_embed.func,
            model=embedding_model,
            api_key=api_key,
            base_url=base_url,
        ),
    )

    return RAGAnything(
        config=config,
        llm_model_func=llm_model_func,
        vision_model_func=vision_model_func,
        embedding_func=embedding_func,
    )
