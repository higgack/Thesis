#!/usr/bin/env bash
# Pre-push verification — automates the CLAUDE.md manual checklist so
# push-time regressions (NameError from a missing lazy import, a
# _HELP_TEXT over the 4000-char Telegram cap, a Python syntax slip,
# an accidentally-committed credential) are caught BEFORE the auto_pull
# rebuild ships them.
#
# Usage:
#   bash scripts/preflight.sh            # check staged + changed .py
#   bash scripts/preflight.sh --all      # check every tracked .py
#
# Exit 0 = safe to push. Exit 1 = a BLOCKING issue (fix first).
# F821 (undefined name) is reported but NOT auto-blocking, because the
# codebase intentionally uses lazy `import x as _x` inside functions and
# forward-ref type-hint strings ("ET.Element") that ruff flags as
# false positives — the script surfaces them for a human glance instead.
#
# This is the lightweight, SDK-friendly substitute for the "Superpowers"
# TDD/verification plugins (which target the interactive Claude Code CLI,
# not this Agent-SDK + GitHub automation setup).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
fail=0
warn=0

# ---- pick files -------------------------------------------------------
if [[ "${1:-}" == "--all" ]]; then
    mapfile -t PYFILES < <(git ls-files '*.py')
else
    # staged + unstaged changes vs HEAD, .py only, still-existing
    mapfile -t PYFILES < <(
        { git diff --name-only HEAD -- '*.py'; git diff --name-only --cached -- '*.py'; } \
        | sort -u | while read -r f; do [[ -f "$f" ]] && echo "$f"; done
    )
fi

if [[ ${#PYFILES[@]} -eq 0 ]]; then
    echo "preflight: no changed .py files — nothing to check."
    PYFILES=()
fi

# ---- 1. syntax (AST) — BLOCKING --------------------------------------
echo "── 1. Python syntax (ast.parse) ──"
for f in "${PYFILES[@]}"; do
    if python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$f" 2>/tmp/pf_ast_err; then
        echo "  ${GRN}OK${RST} $f"
    else
        echo "  ${RED}SYNTAX ERROR${RST} $f"
        sed 's/^/      /' /tmp/pf_ast_err
        fail=1
    fi
done

# ---- 2. undefined names (ruff F821) — WARN ---------------------------
# Catches the real bug class we hit repeatedly: a function using `_html`
# / `re` / etc. without the lazy import line. Lazy-import + forward-ref
# false positives are expected, so this warns rather than blocks.
if command -v ruff >/dev/null 2>&1 && [[ ${#PYFILES[@]} -gt 0 ]]; then
    echo "── 2. Undefined names (ruff F821) ──"
    if ruff check --select F821 "${PYFILES[@]}" 2>/tmp/pf_f821; then
        echo "  ${GRN}none${RST}"
    else
        echo "  ${YEL}review these — real missing-import vs lazy/forward-ref false positive:${RST}"
        grep -E "F821|-->" /tmp/pf_f821 | sed 's/^/      /'
        warn=1
    fi
else
    echo "── 2. ruff not installed — skipping F821 (pip install ruff) ──"
fi

# ---- 3. _HELP_TEXT / guide constants render limits — BLOCKING --------
# _HELP_TEXT must stay <= 4000 (Telegram single-message cap, per
# CLAUDE.md). Guide constants auto-split so they only need to be
# non-empty. Always checks bot.py regardless of the changed-file set,
# since a help edit can ride along with other changes.
echo "── 3. _HELP_TEXT cap + guide constants ──"
python3 - <<'PY'
import re, sys
src = open("src/bot.py", encoding="utf-8").read()
ok = True
m = re.search(r'_HELP_TEXT\s*=\s*"""(.*?)"""', src, re.S)
if not m:
    print("  \033[31mFAIL\033[0m _HELP_TEXT not found"); ok = False
else:
    n = len(m.group(1))
    if n > 4000:
        print(f"  \033[31mFAIL\033[0m _HELP_TEXT {n} > 4000 (Telegram cap)"); ok = False
    else:
        print(f"  \033[32mOK\033[0m _HELP_TEXT {n}/4000 (headroom {4000-n})")
for name in ("_LOOKUP_GUIDE_TEXT", "_PATENTS_GUIDE_TEXT", "_PAPERS_GUIDE_TEXT",
             "_WIKI_GUIDE_TEXT"):
    g = re.search(rf'{name}\s*=\s*"""(.*?)"""', src, re.S)
    if not g or not g.group(1).strip():
        print(f"  \033[31mFAIL\033[0m {name} missing/empty"); ok = False
    else:
        print(f"  \033[32mOK\033[0m {name} ({len(g.group(1))} chars)")
sys.exit(0 if ok else 1)
PY
[[ $? -ne 0 ]] && fail=1

# ---- 4. command handler ↔ help cross-check — WARN --------------------
# Every registered /command should appear somewhere in _HELP_TEXT
# (directly or in brace-shorthand like /kipris_{search,pub,...}).
# Heuristic — warns only, since shorthand can hide a name.
echo "── 4. command handlers present in _HELP_TEXT ──"
python3 - <<'PY'
import re
src = open("src/bot.py", encoding="utf-8").read()
reg = set(re.findall(r'CommandHandler\(\s*"([^"]+)"', src))
help_m = re.search(r'_HELP_TEXT\s*=\s*"""(.*?)"""', src, re.S)
help_txt = help_m.group(1) if help_m else ""
missing = []
for c in sorted(reg):
    # direct mention, or any token of the name (covers brace-shorthand
    # /kipris_{search,...} and "+adv·stats" style listings)
    base = c.split("_")[0]
    if c in help_txt or f"/{base}" in help_txt or base in help_txt:
        continue
    missing.append(c)
if missing:
    print("  \033[33mreview — not obviously in _HELP_TEXT:\033[0m")
    for c in missing:
        print(f"      /{c}")
else:
    print("  \033[32mall %d handlers traceable\033[0m" % len(reg))
PY

# ---- 5. secret scan — BLOCKING --------------------------------------
# Catches a credential accidentally pasted into a TRACKED/STAGED file
# before it reaches the remote (the .env itself is gitignored, so the
# real risk is a key landing in a .py/.md). High-signal token SHAPES
# only — never the generic `API_KEY=...` form — so env-var *names* in
# config.py/CLAUDE.md don't false-positive. This script excludes itself
# (it contains the patterns).
echo "── 5. secret scan (tracked/staged) ──"
python3 - <<'PY'
import re, subprocess, sys, os
pats = {
    "Telegram bot token": re.compile(r'\b\d{8,10}:[A-Za-z0-9_-]{33,46}'),
    "Google API key":     re.compile(r'\bAIza[0-9A-Za-z_\-]{35}\b'),
    "GitHub PAT (classic)": re.compile(r'\bghp_[A-Za-z0-9]{36}\b'),
    "GitHub PAT (fine)":    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{22,}\b'),
    "Private key block":    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
}
def gl(*a):
    try:
        return subprocess.run(["git", *a], capture_output=True,
                              text=True).stdout.split()
    except Exception:
        return []
files = (set(gl("ls-files")) | set(gl("diff", "--cached", "--name-only"))
         | set(gl("diff", "--name-only")))
SELF = {"scripts/preflight.sh"}
hits = []
for f in sorted(files):
    if f in SELF or not os.path.isfile(f):
        continue
    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh, 1):
                for name, pat in pats.items():
                    if pat.search(line):
                        hits.append((f, i, name))
    except Exception:
        continue
if hits:
    print("  \033[31mFAIL — possible secret(s) committed:\033[0m")
    for f, i, name in hits:
        print(f"      {f}:{i}  [{name}]")
    print("  \033[31m→ remove + rotate the key before pushing.\033[0m")
    sys.exit(1)
print("  \033[32mno secrets detected\033[0m")
PY
[[ $? -ne 0 ]] && fail=1

# ---- summary ----------------------------------------------------------
echo
if [[ $fail -ne 0 ]]; then
    echo "${RED}✗ preflight FAILED — blocking issue above. Do NOT push.${RST}"
    exit 1
elif [[ $warn -ne 0 ]]; then
    echo "${YEL}⚠ preflight passed with warnings — glance at section 2/4 above.${RST}"
    exit 0
else
    echo "${GRN}✓ preflight clean.${RST}"
    exit 0
fi
