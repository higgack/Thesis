# ppt-master — presentation/defense slides from your paper (optional)

[ppt-master](https://github.com/hugohe3/ppt-master) (MIT) is a Claude Code **skill**
that turns a source document (PDF/DOCX/Markdown/URL) into a natively **editable
`.pptx`** — real shapes, speaker notes, optional audio narration — via a multi-step
SVG → PPTX pipeline. Good fit for **defense/conference slides** once the paper is done.

It is **not vendored by default**: the skill body is ~97 MB (template + reference
assets), and slides are an end-stage, occasional need — bloating the thesis repo with
it now isn't worth it. Install it as a skill when you actually need slides.

## Install as a skill (when you need it)

```bash
# from the repo root
git clone --depth 1 https://github.com/hugohe3/ppt-master.git /tmp/ppt-master
cp -r /tmp/ppt-master/skills/ppt-master .claude/skills/ppt-master
pip install -r .claude/skills/ppt-master/requirements.txt   # python-pptx, pillow, etc.
# optional: image-gen / narration keys
cp .claude/skills/ppt-master/.env.example .claude/skills/ppt-master/.env   # then edit
```

`.claude/skills/ppt-master/` is git-ignored by default (see `.gitignore`) so the 97 MB
of assets don't land in the repo. Once installed, just ask Claude Code:

> "Make a defense deck from manuscript/draft.pdf"

and the skill drives the design → SVG → PPTX export workflow.

## Notes

- Optional API keys (OpenAI `gpt-image`, Gemini, Pexels/Pixabay) only matter if you want
  AI-generated/searched imagery; the core pipeline works without them.
- Pandoc is only needed for legacy input formats (`.doc/.odt/.rtf/...`).
- If you'd rather have it permanently vendored into the repo (offline, version-pinned),
  say so and it can be committed despite the size.
