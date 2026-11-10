# Model selection guide (모델 선택 기준)

Source: claude.dev Playbooks, "Which Claude model should you build with?"
(2026-11-09). The repo owner asked that every session follow this.

## Default

Start most work on **Opus 5.5 at effort medium**. Move from there:

| Work | Model | Starting effort |
|---|---|---|
| Thesis chapters, documents built from many sources, complex analysis | Opus 5.5 | medium, then high |
| Everyday slides, scripts, docs; well-scoped edits and bug fixes | Sonnet 5.5 | medium, then high |
| Chat-style quick replies | Sonnet 5.5 | low or medium |
| Classification, tagging, bulk extraction | Haiku 5.5 | medium |
| Subagents that search files, read logs/PDFs, or summarise | Haiku 5.5 or Sonnet 5.5 | model default |
| Deep literature research, statistical/quantitative design, major restructuring, leading many subagents | Fable 5.2 | medium, then high |
| Unsure | Opus 5.5 | medium |

## When a result falls short, in this order

1. Give the model a way to check its own work (a test, a build, a script, a recount).
2. Raise effort one level.
3. Move up one model, starting at a *lower* effort than before.
   Past high, test the next model up instead of raising effort further.
   Effort adds thinking and tool calls, not knowledge.

## Mixing models

- Keep judgment calls and edits on the main model. Give cheaper subagents
  only work whose mistakes are cheap to spot (finding files, reading logs,
  summarising sources).
- Keep the main context lean; let subagents do the verbose reading.
- Switch the main model at a natural break, and run `/compact` first
  (the prompt cache belongs to the model that wrote it). Moving down a
  tier drops the earlier model's reasoning.

## How the owner may specify (any wording is fine)

"하이쿠로", "소넷 high", "패블로 깊게", "opus medium", "effort 올려" and the
like are instructions to follow as given.

- Subagent model / effort: Claude sets it directly. Change effort only
  when the owner names one; otherwise use the defaults above.
- Main session model: Claude cannot switch it mid-session. Tell the owner
  when another model fits better; they switch with `/model` (`/fast` for
  speed), ideally after `/compact`.
