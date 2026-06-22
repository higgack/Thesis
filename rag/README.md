# RAG over your source corpus (RAG-Anything)

This is a thin integration around [RAG-Anything](https://github.com/HKUDS/RAG-Anything)
(HKUDS, multimodal RAG over PDFs, Office docs, images, tables, equations). It lets
you build a searchable knowledge base from your reference papers so the research and
writing skills can ground claims in *your* corpus.

RAG-Anything is used as a **dependency**, not vendored. You install it with pip.

## Setup

```bash
# 1. Install (from the repo root)
pip install -r rag/requirements.txt
# Office docs also need LibreOffice; extended image formats need Pillow.

# 2. Configure secrets — copy and fill in
cp rag/.env.example rag/.env
#   edit rag/.env: set OPENAI_API_KEY (and OPENAI_BASE_URL if using a proxy)
```

`rag/.env`, `rag/rag_storage/`, and `rag/output/` are git-ignored — keys and
generated indexes never get committed.

## Use

```bash
# Drop your PDFs / docs into rag/sources/ first, then:
python rag/ingest.py rag/sources/some_paper.pdf      # one file
python rag/ingest.py rag/sources/                    # whole folder
python rag/ingest.py rag/sources/ --triage           # triage-filter PDFs first

python rag/query.py "What methods address declining response rates in surveys?"
python rag/query.py --mode hybrid "Summarize the debate on construct validity"
```

`ingest.py` parses + indexes documents into `rag/rag_storage/`; `query.py` runs a
hybrid retrieval query against that index. Both read config from `rag/.env`.

## Pre-flight triage (cheaper ingestion)

`triage.py` (PyMuPDF) extracts near-free signals from each PDF page and sorts it
into **SKIP** (blank), **TEXT_ONLY** (native text, no OCR), **OCR_NEEDED**
(image/scanned), or **LLM_NEEDED** (tables/forms/mixed layouts) — so you only pay
OCR/LLM cost where it's actually needed. Signal extraction is ~0.001x the cost of
OCR (~1x) or LLM (~2–50x).

```bash
python rag/triage.py rag/sources/paper.pdf            # human-readable report
python rag/triage.py rag/sources/paper.pdf --details  # machine-readable JSON (routes + per-page)
```

`ingest.py --triage` uses it to skip entirely-blank PDFs before they reach
RAG-Anything. The `--details` JSON exposes `routes` (skip/text/ocr/llm page lists)
plus helpers (`route_pages`, `extract_text_pages`, `render_pages_for_ocr`) if you
want to build a custom per-page pipeline.

## How it connects to the skills

When you ask the `deep-research` / `academic-paper` skills to use your own sources,
point them at this RAG index: run `query.py` to pull grounded passages, then feed
those into the skill's synthesis/writing step. The skills' citation-verification
gates still apply — RAG retrieval is evidence, not an excuse to skip verification.

> Models: defaults below use OpenAI (`gpt-4o-mini` + `text-embedding-3-large`) because
> that is RAG-Anything's documented path. Swap in another provider via LightRAG's
> model funcs if you prefer.
