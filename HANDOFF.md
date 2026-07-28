# Agent Handoff Format

Agents communicate via compressed JSON to minimize token cost.

## Evaluation Results

```json
{
  "c": ["model", "metric", "value", "status"],
  "r": [
    ["claude-opus-4.8", "accuracy", 0.95, "pass"],
    ["claude-sonnet-4.6", "accuracy", 0.92, "pass"],
    ["claude-haiku-4.5", "accuracy", 0.88, "pass"]
  ],
  "n": 3,
  "summary": "All models within 7% accuracy band"
}
```

### Schema
- `c`: Column headers (schema once)
- `r`: Rows as arrays (values only, no keys)
- `n`: Row count (checksum)
- `summary`: Single-clause finding only

## Change Manifest (Hive-Builder style)

```json
{
  "changes": [
    {"id": "C1", "file": "src/eval.py", "action": "edit", "lines": "+12 -3", "summary": "remove redundant validation"},
    {"id": "C2", "file": "test/eval.test.py", "action": "edit", "lines": "+4 -0", "summary": "cover edge case"}
  ],
  "verify": "pytest -q → 47 passed",
  "n": 2
}
```

### Rules
- Address by stable `id`, never by position.
- One-clause summary, no narrative.
- `verify` = exact command + result.
- Auth/delete/migration edits: full `summary` + explicit verify (run before return).

## Handoff Boundaries

Safety carve-outs:
- Auth, money, migration, delete → always explicit, never compressed
- Anything user explicitly asked for → keep as-is
- Never elide secrets, validation, error handling

---

*Lever 3 in action: ~25% token reduction on handoffs, zero loss of comprehension.*
