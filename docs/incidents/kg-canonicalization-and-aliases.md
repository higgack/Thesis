# KG canonicalization and aliases

Moved VERBATIM out of `AGENTS.md` (`## Knowledge graph, bullets 'Entity canonicalization' + 'The ONE exception: _ENTITY_ALIASES'`) on 2026-10-18 — text as of commit bf28456 — so the always-loaded rules file stays short. `AGENTS.md` keeps the rules and links here; nothing below was reworded. The `_ENT_STOP` and boot-sweep rules stayed in AGENTS.md verbatim.

---

- **Entity canonicalization** (2026-07-29): `_canon_key()` strips
  whitespace/corp-suffix formatting so "삼성전자"/"삼성 전자"/
  "삼성전자(주)" resolve to one node — live at insert time
  (`_resolve_entity`) plus a retroactive `merge_duplicate_entities()`
  sweep run once per boot. **Relation canonicalization** (2026-08-09)
  is the same pattern one level down for the `rel` string
  ("works_at"/"works at"/"Works-At" → one label) via
  `_resolve_relation()` / `merge_duplicate_relations()`. Both sweeps
  commit per canon-group (not one giant transaction) so they never hold
  the write lock long enough to push a concurrent `add_edges()` past its
  30s busy_timeout. Both are deliberately FORMAT-only, not a synonym/
  meaning merger — collapsing on meaning risks merging genuinely
  distinct entities/relations an LLM pass would need to judge
  case-by-case.
- **The ONE exception: `_ENTITY_ALIASES`** (2026-08-20, explicit user
  decision — canonical form is the **English** spelling). Format-only
  normalization cannot fold "엔비디아" into "NVIDIA" (no shared
  characters), so the graph carried both as separate top nodes —
  엔비디아 3,315 vs NVIDIA 1,759, plus OpenAI/오픈AI, SpaceX/스페이스X,
  Anthropic/앤트로픽, 메타/Meta. "메타" is the one entry that is not
  purely a transliterated company name (it is also the Korean prefix for
  "meta-"); the risk was raised and the user included it anyway
  (2026-08-20) since in this corpus 메타 1,895 sits beside Meta 1,721.
  Dropping an alias later does NOT undo a merge — the sweep rewrites
  edge rows, so the folded-away spelling is gone. It is a hand-curated 1:1
  transliteration dict, NOT an algorithm, and only lists pairs observed
  with both spellings live. `_canon_key()` folds the variant onto the
  English key; `_ALIAS_CANONICAL` pins the display form so
  `merge_duplicate_entities()`'s most-used-variant rule can't hand the
  win back to the more frequent Korean spelling. Extending it = adding
  one line — **but confirm the affected edge counts with the user first,
  exactly like `_ENT_STOP` below.** The two are the same class of action:
  one line of source, irreversible, thousands of edge rows rewritten at
  the next boot sweep (엔비디아 3,315 folded into NVIDIA). `_ENT_STOP`
  carried that confirmation requirement and this did not — an asymmetry
  nobody noticed until an instruction audit on 2026-09-06. Merging the
  wrong pair is worse than a bad stopword: a stopword deletes edges the
  graph can re-extract, a wrong alias silently attributes one entity's
  edges to another. Do NOT generalize it into automatic synonym merging
  — the paragraph above still holds for everything else.
