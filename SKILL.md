---
name: thesis-honey
description: >
  Apply Honey (I Shrunk the AI) principles to Thesis model evaluation:
  Lever 1 (minimum code), Lever 2 (say less), Lever 3 (compress agent handoffs).
tools: Read, Grep, Edit, Bash, Powershell
---

# Thesis Honey Skill

**Goal**: Reduce token spend per evaluation cycle by 30-50% without losing accuracy.

## Lever 1 — Model Evaluation Code

Walk the ladder:

1. **Needs to exist?** Can this be a config in `eval.yaml` instead of code?
2. **Stdlib + common metrics** — use `sklearn.metrics`, don't hand-roll `accuracy`, `f1`, `auc`.
3. **Language native** — dict comprehension > helper function (unless reused 3+).
4. **Installed dependency** — is it in `requirements.txt`? Use it before adding new lib.
5. **One line before block** — if-check before loading all test data.
6. **Min function** — no speculative params like `enable_future_feature=False`.

**Never cut**:
- Input validation (file exists? schema valid?)
- Error handling (caught/logged exceptions for missing models, corrupt files)
- Auth (API keys, secrets) — never compress

## Lever 2 — Evaluation Report Prose

Current (bad): "The evaluation framework performs a comprehensive assessment of each model across multiple metrics..."

Better: Framework metrics: accuracy, F1, latency. Models: Opus, Sonnet, Haiku.

### Rules
- Drop: "I will now", "importantly", "as you can see"
- Keep: "raises `ValueError` on empty input — use `.get(key, [])`"
- Lists > paragraphs: "Metrics: accuracy, F1, latency" vs "The framework calculates several metrics including..."
- No restating prompt: the user already knows they asked for eval

## Lever 3 — Agent Result Format

**Between evaluators** (eval_orchestrator → reviewer → reporter):

```json
{
  "c": ["model", "test", "score", "pass"],
  "r": [
    ["opus", "unit", 0.98, true],
    ["sonnet", "unit", 0.95, true],
    ["haiku", "unit", 0.91, true]
  ],
  "n": 3,
  "threshold_breach": [],
  "notes": "all models pass unit tier"
}
```

Not:
```json
{
  "results": [
    {"model": "opus", "test": "unit", "score": 0.98, "pass": true},
    ...
  ]
}
```

**Savings**: ~25% tokens on handoff, same comprehension (every stdlib parses columnar JSON).

### Safety Carve-outs
- Model ranking changes → always explicit, never compressed to a number
- Threshold breaches → full detail, not "warning=true"
- Failed auth → full error message, never elided
