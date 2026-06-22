# rtk — token-saving CLI proxy (optional)

[rtk](https://github.com/rtk-ai/rtk) ("Rust Token Killer") is a CLI proxy that
compresses verbose command output (git, test runners, build tools, etc.) by
60–90% before it reaches an LLM. It cuts token usage and API cost during AI-assisted
work on this project. It is a **developer-workflow tool**, not part of the paper
itself — so it is integrated by *install + opt-in config*, never vendored.

## Install

```bash
# pick one
brew install rtk-ai/tap/rtk          # Homebrew
cargo install rtk                     # Rust toolchain
curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/main/install.sh | bash
```

## Enable for Claude Code

```bash
rtk init -g          # global auto-rewrite hook for your AI tool
```

After that, normal commands (`git status`, test/build runs, …) are transparently
rewritten to their compact `rtk` equivalents, so the model sees filtered output.
Check savings with `rtk stats`.

## Why it's opt-in here

`rtk init` installs a shell/agent hook that rewrites commands automatically. That is
machine-level configuration, so we don't wire it into this repo's
`.claude/settings.json` for you — review what `rtk init` does, then run it yourself
if you want it. Nothing else in this project depends on rtk.
