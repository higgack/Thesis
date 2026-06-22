# Thesis — AI-assisted academic paper workspace

A self-contained project for researching and writing an academic paper / thesis
with Claude Code. It bundles three building blocks:

| Component | Role | How it's integrated |
|-----------|------|---------------------|
| **[Academic Research Skills](https://github.com/Imbad0202/academic-research-skills)** | 4 skills / 27 modes: research → write → review → revise → finalize | **vendored** (CC-BY-NC-4.0) — auto-discovered from `.claude/` |
| **[RAG-Anything](https://github.com/HKUDS/RAG-Anything)** | Multimodal RAG over your reference PDFs/docs (text, tables, equations, images) | **pip dependency + scaffold** in `rag/` (with PyMuPDF page triage) |
| **[rtk](https://github.com/rtk-ai/rtk)** | Token-saving CLI proxy (60–90% output compression) for cheaper AI sessions | **optional install** — `docs/tooling/rtk.md` |
| **[notebooklm-mcp](https://github.com/PleasePrompto/notebooklm-mcp)** | Google NotebookLM via MCP — grounded Q&A + audio overviews (complements `rag/`) | **optional MCP config** — `docs/tooling/notebooklm-mcp.md` |
| **[ppt-master](https://github.com/hugohe3/ppt-master)** | Editable `.pptx` defense/conference slides from your paper | **optional skill install** — `docs/tooling/ppt-master.md` |

> AI is your copilot, not the pilot. Every stage keeps a human in the loop.

## Layout

```
manuscript/          ← your actual paper: drafts, sections, data, figures, refs
rag/                 ← RAG-Anything integration (ingest.py, query.py, triage.py, sources/)
docs/tooling/        ← optional tooling (rtk setup)
.claude/
  skills/   → 4 skills (symlinks)   commands/ → /ars-*   agents/
  settings.json.example  → OPTIONAL hooks (opt-in)
deep-research/ academic-paper/ academic-paper-reviewer/ academic-pipeline/
scripts/  shared/  commands/  agents/  hooks/  audits/  skills/   ← suite internals
ACADEMIC_RESEARCH_SKILLS.md  LICENSE  NOTICE.md  CITATION.cff      ← suite + attribution
```

## Quick start

**Writing & research** — just describe what you want; Claude Code auto-picks the
skill/mode (see `QUICKSTART.md`):

- "Guide me through framing my research question" → `deep-research` (Socratic)
- "Help me write a paper about <topic>" → `academic-paper`
- "Review this draft" (attach it) → `academic-paper-reviewer`
- "Produce a complete paper end-to-end" → `academic-pipeline`

**Ground it in your own sources** — build a RAG index over your reference corpus:

```bash
pip install -r rag/requirements.txt
cp rag/.env.example rag/.env          # add your OPENAI_API_KEY
# put PDFs in rag/sources/, then:
python rag/ingest.py rag/sources/
python rag/query.py "your question"
```

See `rag/README.md` for details and how the skills consume the index.

**Optional tooling** (`docs/tooling/`): rtk (token savings), notebooklm-mcp
(Google NotebookLM grounded Q&A), ppt-master (slides from the paper).

## Notes on integration choices

- The skills are **vendored** so they work every session with no external
  dependency; `scripts/` and `shared/` sit at the repo root because the skills
  reference them by relative path.
- RAG-Anything and rtk are **not** vendored — RAG-Anything is a pip-installable
  framework (its own heavy deps: LightRAG, MinerU…), and rtk is a global CLI binary.
  Vendoring either would be the wrong unit of reuse; they're integrated as a
  dependency and an optional tool respectively.
- The suite's optional hooks (write-scope guard, session announce) are left inert
  in `.claude/settings.json.example`. They run vendored shell/Python automatically,
  so enabling them is an explicit opt-in (copy to `.claude/settings.json` after review).

## Attribution & license

The vendored `academic-research-skills` suite is © **Cheng-I Wu**, licensed
**CC-BY-NC-4.0** (see `LICENSE`, `NOTICE.md`, `CITATION.cff`) — non-commercial
academic use. RAG-Anything (HKUDS) and rtk (rtk-ai) are referenced under their own
upstream licenses and are not redistributed here.
