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
    # staged + unstaged changes vs HEAD, PLUS untracked new files (`git
    # diff` never sees a file that hasn't been `git add`ed at all — a
    # brand-new .py with a syntax error was invisible to section 1 until
    # staged; --others --exclude-standard covers it without pulling in
    # .gitignore'd junk).
    mapfile -t PYFILES < <(
        { git diff --name-only HEAD -- '*.py'
          git diff --name-only --cached -- '*.py'
          git ls-files --others --exclude-standard -- '*.py'; } \
        | sort -u | while read -r f; do [[ -f "$f" ]] && echo "$f"; done
    )
fi

if [[ ${#PYFILES[@]} -eq 0 ]]; then
    echo "preflight: no changed .py files — nothing to check."
    PYFILES=()
fi

# ---- 1. syntax (AST) — BLOCKING --------------------------------------
echo "── 1. Python syntax (compile) ──"
# compile(), NOT ast.parse: ast.parse misses symtable-stage SyntaxErrors —
# "name X is used prior to global declaration" parses fine but fails at
# IMPORT, which is how a broken regenerate.py shipped on 2026-08-26 and
# killed every dashboard tick while this section reported OK.
for f in "${PYFILES[@]}"; do
    if python3 -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" "$f" 2>/tmp/pf_ast_err; then
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
        body = g.group(1)
        # Telegram-send safety. These constants are sent with
        # parse_mode="HTML" through _split_for_telegram(), which only
        # splits on paragraph/line boundaries — so a chunk is valid only
        # if every tag it opens also closes inside it, and it fits the
        # 4000-char soft limit. A tag left open across a split makes
        # Telegram reject that message with a parse error, and until now
        # this check only verified the constant was non-empty. Mirrors
        # bot.py's _split_for_telegram exactly.
        LIMIT = 4000
        def _split(text, limit=LIMIT):
            if len(text) <= limit:
                return [text]
            chunks, buf = [], ""
            for para in text.split("\n\n"):
                if len(para) > limit:
                    if buf:
                        chunks.append(buf); buf = ""
                    lb = ""
                    for line in para.split("\n"):
                        cand = (lb + "\n" + line) if lb else line
                        if len(cand) > limit and lb:
                            chunks.append(lb); lb = line
                        else:
                            lb = cand
                    if lb:
                        buf = lb
                    continue
                cand = (buf + "\n\n" + para) if buf else para
                if len(cand) > limit and buf:
                    chunks.append(buf); buf = para
                else:
                    buf = cand
            if buf:
                chunks.append(buf)
            return chunks
        problems = []
        for i, ch in enumerate(_split(body), 1):
            if len(ch) > LIMIT:
                problems.append(f"chunk {i} is {len(ch)} > {LIMIT}")
            for tg in ("b", "i", "code", "a", "u", "s", "pre"):
                o = len(re.findall(rf'<{tg}(?:\s[^>]*)?>', ch))
                c = len(re.findall(rf'</{tg}>', ch))
                if o != c:
                    problems.append(
                        f"chunk {i}: <{tg}> {o} open / {c} close")
        if problems:
            print(f"  \033[31mFAIL\033[0m {name} would break on send:")
            for pr in problems:
                print(f"      {pr}")
            ok = False
        else:
            print(f"  \033[32mOK\033[0m {name} ({len(body)} chars, "
                  f"{len(_split(body))} tg chunk(s), tags balanced)")
sys.exit(0 if ok else 1)
PY
[[ $? -ne 0 ]] && fail=1

# ---- 4. command handler ↔ help cross-check — WARN --------------------
# Every registered /command should appear somewhere in _HELP_TEXT
# (directly, or as one item of a brace-shorthand like
# /kipris_{search,pub,...}). The old heuristic fell back to "prefix
# before the first underscore appears ANYWHERE in the help text", which
# passes for basically any command sharing a family prefix (e.g.
# wiki_prune_confirm passes just because "/wiki" appears elsewhere for
# unrelated wiki commands) — confirmed false-clean on wiki_prune_confirm/
# wiki_fix_confirm/kipris_status. Replaced with: exact "/full_name"
# match, or exact membership in an expanded /{prefix}_{a,b,c} group.
echo "── 4. command handlers present in _HELP_TEXT ──"
python3 - <<'PY'
import glob, re
src = open("src/bot.py", encoding="utf-8").read()
# Handlers are not all registered in bot.py — src/notes/telegram.py adds
# its own (/notes, /notes_guide). Scanning bot.py alone reported "all 112
# handlers traceable" while silently skipping those two, so a study-notes
# command could drop out of the help text without this check noticing.
reg = set()
for f in sorted(glob.glob("src/**/*.py", recursive=True)):
    with open(f, encoding="utf-8") as fh:
        reg |= set(re.findall(r'CommandHandler\(\s*"([^"]+)"', fh.read()))
help_m = re.search(r'_HELP_TEXT\s*=\s*"""(.*?)"""', src, re.S)
help_txt = help_m.group(1) if help_m else ""

brace_expanded = set()
for prefix, items in re.findall(r'/(\w+)_\{([^}]+)\}', help_txt):
    for item in items.split(","):
        item = item.strip()
        if item:
            brace_expanded.add(f"{prefix}_{item}")

missing = []
for c in sorted(reg):
    if f"/{c}" in help_txt or c in brace_expanded:
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

echo "── 6. blocking SQLite writers on the event loop ──"
python3 - <<'PY'
# Every meta.py function that opens _wconn() takes _W_LOCK, whose
# contract (meta.py) is "callers already run in an asyncio.to_thread
# worker, so blocking here never touches the event loop". Calling one
# inline from an `async def` breaks that contract and freezes the whole
# loop for as long as a worker holds the lock — 431s on 2026-08-26,
# caught by data/loop_stalls.log in summarize.py, with a second instance
# sitting in vector.py's ingest hot path. Nothing flagged it, so this
# does: a direct Call node inside an async def is a violation, while a
# reference passed to to_thread(...) is not a Call and is correctly
# ignored. Calls inside a nested plain `def` are skipped too — that
# closure is what gets offloaded.
import ast, pathlib, sys

meta = pathlib.Path("src/store/meta.py")
WRITERS = set()
if meta.exists():
    for n in ast.walk(ast.parse(meta.read_text(encoding="utf-8"))):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if "'_wconn'" in ast.dump(n):
                WRITERS.add(n.name)
WRITERS.discard("_wconn")

def base_and_attr(call):
    f = call.func
    if isinstance(f, ast.Attribute):
        b = f.value
        return (getattr(b, "id", None) or getattr(b, "attr", None)), f.attr
    return None, None

bad = []
for path in sorted(pathlib.Path("src").rglob("*.py")):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        stack = [fn]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue          # nested def: offloaded / counted alone
                if isinstance(child, ast.Call):
                    b, a = base_and_attr(child)
                    if a in WRITERS and b and "meta" in b.lower():
                        bad.append((str(path), child.lineno, f"{b}.{a}", fn.name))
                stack.append(child)

if not WRITERS:
    print("  \033[33mskipped — could not parse src/store/meta.py\033[0m")
elif bad:
    print("  \033[31mFAIL — meta.db writer called directly on the event loop:\033[0m")
    for f, l, c, inside in sorted(set(bad)):
        print(f"      {f}:{l}  {c}()  inside async def {inside}()")
    print("  \033[31m→ wrap it: await asyncio.to_thread(<fn>, ...)\033[0m")
    sys.exit(1)
print(f"  \033[32mno loop-blocking writers ({len(WRITERS)} guarded fns)\033[0m")
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
