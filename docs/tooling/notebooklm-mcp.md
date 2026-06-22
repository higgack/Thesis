# notebooklm-mcp — Google NotebookLM via MCP (optional)

[notebooklm-mcp](https://github.com/PleasePrompto/notebooklm-mcp) is an MCP server
that lets Claude Code query **Google NotebookLM**: grounded, citation-backed answers
over documents you've uploaded, plus source ingestion and Audio Overviews.

It is **complementary** to the local `rag/` (RAG-Anything) pipeline:

| | `rag/` (RAG-Anything) | notebooklm-mcp |
|---|---|---|
| Where it runs | Local, self-hosted | Google's hosted NotebookLM |
| Cost / keys | Your OpenAI key | Google account (free tier) |
| Strength | Multimodal, fully private, scriptable | Strong grounded QA + audio overviews, zero infra |

Use RAG-Anything when you want everything local/scriptable; use NotebookLM for quick
grounded Q&A and audio summaries of your source set.

## Enable (opt-in)

Requires **Node ≥ 18** and **Chrome** (a one-time interactive Google login is stored
in a Chrome profile). It is not vendored — it runs via `npx`.

```bash
cp .mcp.json.example .mcp.json     # adds the "notebooklm" MCP server for this repo
```

Then restart Claude Code in this repo; on first use it launches
`npx notebooklm-mcp@latest` and walks you through the Google login. Tool profiles
(`minimal` / `standard` / `full`) control how much context the server's tools consume —
see the upstream README.

## Why opt-in

A committed `.mcp.json` auto-starts the server (downloading via `npx`, launching Chrome,
prompting Google auth) for anyone who opens the repo. That's intrusive and
machine-specific, so we ship it as `.mcp.json.example` and let you turn it on after review.
