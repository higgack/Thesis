# Architecture

Thesis evaluation follows Honey principles: minimal code, terse output, compressed agent handoffs.

## Agents (read-only, return compressed JSON)

```
eval_orchestrator (init)
  ├── eval_worker (run per model) → {"c":["metric","val"],"r":[...], "n":N}
  ├── eval_worker (run per model) → {"c":["metric","val"],"r":[...], "n":N}
  └── eval_reviewer (aggregate) → {"summary":"...", "threshold_breach":[], "n":X}
```

Each agent:
- Takes one input batch (model, test_tier, max_timeout)
- Runs evaluation under Lever-1 ladder (no extra features)
- Returns compressed JSON (Lever 3), not pretty-printed
- Never repeats column headers across messages

## Files

**Minimal code structure**:
- `eval.py` ≤150 lines — orchestrator + metrics (or use sklearn)
- `eval.test.py` — one happy path, one error path
- `eval.yaml` — config (test_tiers, models, thresholds), not hardcoded
- `.instructions.md` — Honey rules (this skill)
- `HANDOFF.md` — agent output format (Lever 3)

No:
- `utils/validation.py` (one function?) — use stdlib or inline
- `models/model_cache.py` (not asked for) — cache in memory dict
- `pipeline/pipeline_manager.py` (speculative) — call functions, don't abstract 1-call chains

## Invoke

```bash
# Stage 1: Run evaluations, get compressed results per model
python eval.py --model claude-opus-4.8 --tier unit --timeout 300

# Expect: {"c":["metric","value"],"r":[["accuracy",0.95],...],"n":3}
```

Stdout = valid JSON only (parseable by next agent).
Stderr = logs (human read).

---

*Lever 1+2+3 applied: code <200 LOC, output <2KB per eval, zero token waste on narration.*
