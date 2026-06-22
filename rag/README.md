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

python rag/query.py "What methods address declining response rates in surveys?"
python rag/query.py --mode hybrid "Summarize the debate on construct validity"
```

`ingest.py` parses + indexes documents into `rag/rag_storage/`; `query.py` runs a
hybrid retrieval query against that index. Both read config from `rag/.env`.

## How it connects to the skills

When you ask the `deep-research` / `academic-paper` skills to use your own sources,
point them at this RAG index: run `query.py` to pull grounded passages, then feed
those into the skill's synthesis/writing step. The skills' citation-verification
gates still apply — RAG retrieval is evidence, not an excuse to skip verification.

> Models: defaults below use OpenAI (`gpt-4o-mini` + `text-embedding-3-large`) because
> that is RAG-Anything's documented path. Swap in another provider via LightRAG's
> model funcs if you prefer.
