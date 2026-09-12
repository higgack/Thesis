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

echo "── 7. dashboard design tokens (DESIGN.md ⇄ code, 대비) ──"
python3 - <<'PY7'
# Section 7 — dashboard design tokens.
#
# 7a (pre-existing, BLOCKING): the Q&A / KG / Note dashboards must not
#     re-declare their own :root palette; they pull widgets.DESIGN_TOKENS_CSS.
# 7b..7e (added 2026-09-12, WARN): DESIGN.md is the agent-readable copy of
#     that palette. A doc nobody verifies rots, so these cross-check it
#     against the live CSS and ratchet the colour debt that motivated it
#     (three greens, two oranges — all introduced by an agent inventing a
#     Tailwind colour instead of using the token that already existed).
#
# Every added check is a RATCHET, not a rule: today's debt is recorded as a
# baseline and only an INCREASE warns. Without that, preflight would print
# ~100 findings on every run forever, which is the "notification with no
# off-switch" failure AGENTS.md bans.
import re, sys
from pathlib import Path

SHARED = {"kg_render.py", "notes_render.py", "regenerate.py"}
ALLOWED_OWN = {"wiki_render.py": "Wikipedia palette",
               "universe_render.py": "graph-canvas tokens"}
BLK = re.compile(r"(:root|\[data-theme=[\"']?dark[\"']?\])\s*\{")

d = Path("src/dashboard")
if not d.is_dir():
    print("  \033[33mskipped — src/dashboard not found\033[0m"); sys.exit(0)

# ---- 7a: no duplicated palette blocks (BLOCKING) ----------------------
bad, unknown = [], []
for f in sorted(d.glob("*.py")):
    hits = BLK.findall(f.read_text(encoding="utf-8"))
    if not hits:
        continue
    if f.name in SHARED:
        bad.append((f.name, len(hits)))
    elif f.name not in ALLOWED_OWN and f.name != "widgets.py":
        unknown.append((f.name, len(hits)))

if bad:
    print("  \033[31mFAIL — these must use widgets.DESIGN_TOKENS_CSS:\033[0m")
    for n, c in bad:
        print(f"      {n}: defines its own :root/dark ({c} block(s))")
    print("  \033[31m→ replace the block with the shared constant, or add the"
          " file to ALLOWED_OWN here with a reason\033[0m")
    sys.exit(1)

warn = []
if unknown:
    for n, c in unknown:
        warn.append(f"new dashboard defines its own palette — intended? "
                    f"{n} ({c} block(s))")

# ---- colour helpers ---------------------------------------------------
def _rgb(v):
    """CSS colour literal -> (r,g,b,a) or None. Only the forms this
    codebase actually writes; anything else is left to a human."""
    v = v.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", v)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"#([0-9a-fA-F]{3})", v)
    if m:
        h = m.group(1)
        return tuple(int(c * 2, 16) for c in h) + (1.0,)
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*"
                     r"(?:,\s*([\d.]+)\s*)?\)", v)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                float(m.group(4)) if m.group(4) else 1.0)
    return None

# #fff / #000 are universal, not palette drift — a white label on a
# coloured button is not someone re-typing --panel.
NEUTRAL = {(255, 255, 255), (0, 0, 0)}

# ---- live tokens out of widgets.py ------------------------------------
wid_path = d / "widgets.py"
wid_src = wid_path.read_text(encoding="utf-8")
m = re.search(r'DESIGN_TOKENS_CSS\s*=\s*"""(.*?)"""', wid_src, re.S)
css_tokens = {}          # name -> {theme: value}
tok_block = ""
if m:
    tok_block = m.group(0)
    css = m.group(1)
    for theme, pat in (("light", r":root\{(.*?)\}"),
                       ("dark", r'\[data-theme="dark"\]\{(.*?)\}')):
        body = re.search(pat, css, re.S)
        if not body:
            continue
        for k, v in re.findall(r"--([a-z-]+)\s*:\s*([^;]+?)\s*(?=;|$)",
                               body.group(1), re.S):
            css_tokens.setdefault(k, {})[theme] = v.strip()
else:
    warn.append("widgets.DESIGN_TOKENS_CSS not found — 7b~7e skipped")

# ---- 7b: DESIGN.md must match the live tokens -------------------------
# Deliberately a hand-rolled parse of a flat 2-level mapping rather than
# PyYAML: preflight must run with nothing installed, and PyYAML is only
# here transitively (chromadb). The file is written to stay in that subset.
dmd = Path("DESIGN.md")
doc_state = "DESIGN.md in sync"
if css_tokens:
    if not dmd.exists():
        doc_state = "DESIGN.md 없음"
        warn.append("DESIGN.md missing — the palette has no agent-readable "
                    "copy (see AGENTS.md '## Dashboard design tokens')")
    else:
        txt = dmd.read_text(encoding="utf-8")
        fm = re.match(r"---\n(.*?)\n---\n", txt, re.S)
        if not fm:
            doc_state = "DESIGN.md front matter 없음"
            warn.append("DESIGN.md has no YAML front matter")
        else:
            doc = {}
            cur = None
            for line in fm.group(1).splitlines():
                if re.match(r"^(colors|colorsDark):\s*$", line):
                    cur = {"colors": "light",
                           "colorsDark": "dark"}[line.split(":")[0]]
                    continue
                if re.match(r"^\S", line):
                    cur = None
                    continue
                if cur is None:
                    continue
                kv = re.match(r"^\s{2}([a-z-]+):\s*\"?([^\"\n]+?)\"?\s*$", line)
                if kv:
                    doc.setdefault(cur, {})[kv.group(1)] = kv.group(2)
            drift = []
            for theme in ("light", "dark"):
                live = {k: v[theme] for k, v in css_tokens.items()
                        if theme in v and _rgb(v[theme])}
                docd = doc.get(theme, {})
                for k in sorted(set(live) | set(docd)):
                    lv, dv = live.get(k), docd.get(k)
                    if lv is None:
                        drift.append(f"{theme}.{k}: DESIGN.md만 있음 ({dv})")
                    elif dv is None:
                        drift.append(f"{theme}.{k}: widgets.py만 있음 ({lv})")
                    elif _rgb(lv) != _rgb(dv):
                        drift.append(f"{theme}.{k}: widgets.py={lv} "
                                     f"≠ DESIGN.md={dv}")
            if drift:
                doc_state = f"DESIGN.md {len(drift)}건 불일치"
                warn.append("DESIGN.md ⇄ widgets.py 토큰 불일치 "
                            f"{len(drift)}건 — 같은 커밋에서 맞출 것:")
                warn.extend("    · " + x for x in drift[:12])

# ---- literal scan (shared by 7c and 7d) -------------------------------
PAL = {}                 # (r,g,b) -> {token names}
for k, themes in css_tokens.items():
    for v in themes.values():
        c = _rgb(v)
        if c:
            PAL.setdefault(c[:3], set()).add(k)

LIT = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b"
                 r"|rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*(?:,\s*[\d.]+\s*)?\)")
retyped, offpal = {}, {}
for f in sorted(d.glob("*.py")):
    src = f.read_text(encoding="utf-8")
    if f.name == "widgets.py" and tok_block:
        src = src.replace(tok_block, "")   # the definitions are not drift
    for lit in LIT.findall(src):
        c = _rgb(lit)
        if c is None or c[:3] in NEUTRAL:
            continue
        (retyped if c[:3] in PAL else offpal).setdefault(c[:3], []).append(f.name)

# 7c: a literal that IS a token — always fixable, never ambiguous.
RETYPED_BUDGET = 19      # occurrences, 2026-09-12. Lower this as they go.
n_retyped = sum(len(v) for v in retyped.values())
# 7d: a colour the palette does not contain at all.
OFFPAL_BUDGET = 89       # distinct colours, 2026-09-12. Lower as they go.
n_offpal = len(offpal)

if n_retyped > RETYPED_BUDGET:
    warn.append(f"토큰과 같은 값을 숫자로 쓴 곳 {n_retyped}회 "
                f"(기준선 {RETYPED_BUDGET}) — var(--토큰)을 쓸 것:")
    for c, fs in sorted(retyped.items(), key=lambda x: -len(x[1]))[:6]:
        warn.append(f"    · #{'%02x%02x%02x' % c} = --"
                    f"{'/--'.join(sorted(PAL[c]))} ×{len(fs)}"
                    f" [{','.join(sorted(set(fs)))}]")
if n_offpal > OFFPAL_BUDGET:
    warn.append(f"팔레트 밖 색 {n_offpal}종 (기준선 {OFFPAL_BUDGET}) — "
                "새 색을 지어내지 말고 DESIGN.md의 토큰을 쓸 것:")
    for c, fs in sorted(offpal.items(), key=lambda x: -len(x[1]))[:6]:
        warn.append(f"    · #{'%02x%02x%02x' % c} ×{len(fs)}"
                    f" [{','.join(sorted(set(fs)))}]")

# ---- 7e: WCAG contrast on rules that set BOTH bg and fg literally -----
# var()-driven pairs can't be resolved statically and are skipped; the
# literal ones are exactly the off-palette set this section is about.
def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

def _lum(rgb):
    return .2126 * _lin(rgb[0]) + .7152 * _lin(rgb[1]) + .0722 * _lin(rgb[2])

def _over(fg, bg):
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3))

BASE = {"light": _rgb(css_tokens.get("bg", {}).get("light", "#ffffff")),
        "dark": _rgb(css_tokens.get("bg", {}).get("dark", "#000000"))}
RULE = re.compile(r"([^{}\n]+)\{([^{}]*)\}")
# Selectors already below AA on 2026-09-12. Listed, not counted, so a NEW
# offender is named even if an old one was fixed in the same diff.
CONTRAST_DEBT = {
    "notes_render.py .cat-투자론", "regenerate.py .cmd-badge.paid",
    "notes_render.py .ndel:hover", "regenerate.py .del-btn:hover",
    "notes_render.py .cat-반도체", "notes_render.py .cat-코인",
    "notes_render.py .controls .bookfilter.active", "notes_render.py .cat-종목",
    "wiki_render.py .wiki-badge.recent", "wiki_render.py .wiki-badge.new",
    "notes_render.py .cat-스터디", "regenerate.py .cmd-badge.mutation",
    "notes_render.py .cat-대학원", "notes_render.py .cat-공부",
    "notes_render.py .cat-AI", "notes_render.py .cat-산업",
    "kg_render.py .alarm-set", "notes_render.py .alarm-set",
    "regenerate.py .alarm-set", "widgets.py .alarm-set,.alarm-setdt",
    "wiki_render.py .topic-memo .alarm-set",
}
new_bad, still_bad = [], 0
if BASE["light"] and BASE["dark"]:
    for f in sorted(d.glob("*.py")):
        for sel, body in RULE.findall(f.read_text(encoding="utf-8")):
            b = re.search(r"background(?:-color)?\s*:\s*([^;}]+)", body)
            c = re.search(r"(?<![-\w])color\s*:\s*([^;}]+)", body)
            if not (b and c):
                continue
            bg, fg = _rgb(b.group(1)), _rgb(c.group(1))
            if not bg or not fg:
                continue
            sel = " ".join(sel.split())
            theme = "dark" if ("data-theme" in sel and "dark" in sel) else "light"
            bgc = _over(bg, BASE[theme])
            fgc = _over(fg, bgc)
            hi, lo = max(_lum(bgc), _lum(fgc)), min(_lum(bgc), _lum(fgc))
            ratio = (hi + .05) / (lo + .05)
            if ratio >= 4.5:
                continue
            key = f"{f.name} {sel}"
            if key in CONTRAST_DEBT:
                still_bad += 1
            else:
                new_bad.append((round(ratio, 2), key))
if new_bad:
    warn.append(f"명암비 AA(4.5:1) 미달 규칙이 새로 {len(new_bad)}개 생김 "
                "— 작은 글씨(11~13px)라 AA가 적용된다:")
    for r, k in sorted(new_bad):
        warn.append(f"    · {r}:1  {k}")

# ---- report -----------------------------------------------------------
allow = ", ".join(f"{k.split('_')[0]}={v}" for k, v in ALLOWED_OWN.items())
summary = (f"tokens shared by {len(SHARED)} dashboards, "
           f"{len(ALLOWED_OWN)} allowlisted ({allow})"
           f" · {doc_state} · 부채 retyped {n_retyped}/{RETYPED_BUDGET}"
           f", off-palette {n_offpal}/{OFFPAL_BUDGET}"
           f", contrast {still_bad}/{len(CONTRAST_DEBT)}")
if warn:
    print("  \033[33m%d finding(s):\033[0m" % len([w for w in warn
                                                   if not w.startswith("    ")]))
    for w in warn:
        print(("      " + w) if not w.startswith("    ") else ("    " + w))
    print(f"  \033[33m(baseline: {summary})\033[0m")
    sys.exit(2)
print("  \033[32m%s\033[0m" % summary)
sys.exit(0)
PY7
rc=$?
[[ $rc -eq 1 ]] && fail=1
[[ $rc -eq 2 ]] && warn=1

# ---- 8. guard / fallback deletion — WARN ------------------------------
echo "── 8. guard·fallback deletion in this diff ──"
python3 - <<'PY8'
# AGENTS.md: "폴백·호환 경로는 지우지 않는다. 평소 안 타는 코드 대부분이
# 실패 경로다." That rule has been prose-only, enforced by whoever
# remembers it. This is the mechanical half — borrowed from
# oh-my-hermes' completion_integrity.py, which refuses a completion
# claim whose diff removes a guard without an adversarial regression to
# replace it.
#
# WARNING, never blocking: deleting a guard is sometimes exactly right
# (a fallback whose failure mode is gone). The point is that it should
# be a decision, not a slip. A check that cried wolf would get ignored,
# which is worse than not having it — so the vocabulary is deliberately
# narrow and a line that merely MOVED is not reported.
import re, subprocess, sys

# Vocabulary derived from what THIS repo actually writes, not from the
# words OMH's classifier uses. The first version matched \bsemaphore\b
# and missed `with _GLOBAL_OCR_SEM:` entirely — a real guard deletion
# sailed through the check in testing. Every concurrency guard here is
# an abbreviated _..._SEM / _..._LOCK name.
GUARD = re.compile(
    r"(_[A-Z0-9_]*(?:SEM|LOCK)[A-Z0-9_]*"          # _GLOBAL_OCR_SEM, _W_LOCK
    r"|\bexcept\b|\bfinally\b"                     # failure paths
    r"|\b(?:fallback|back-compat|backcompat|legacy|guard)\b"
    r"|\.bak\b|폴백|가드)", re.I)
# An added line mentioning any of these says the removal was considered.
EXCUSE = re.compile(r"\b(regression|adversarial|guard|fallback|폴백|"
                    r"replaced by|대체)\b", re.I)

try:
    diff = subprocess.run(
        ["git", "diff", "HEAD", "--unified=0", "--", "*.py", "*.sh"],
        capture_output=True, text=True, timeout=30).stdout
except Exception as e:
    print(f"  \033[33mskipped — git diff failed ({e})\033[0m"); sys.exit(0)

# Per FILE, not per diff. A first version pooled every addition in the
# diff, so unrelated edits elsewhere that happened to contain the word
# "guard" silenced a real removal in another file.
removed, added = {}, {}
cur = None
for line in diff.splitlines():
    if line.startswith("diff --git "):
        cur = line.rsplit(" b/", 1)[-1]
        removed.setdefault(cur, []); added.setdefault(cur, [])
    elif cur is None:
        continue
    elif line.startswith("-") and not line.startswith("---"):
        removed[cur].append(line[1:].strip())
    elif line.startswith("+") and not line.startswith("+++"):
        added[cur].append(line[1:].strip())

hits = []
for f, rem in removed.items():
    add = added.get(f, [])
    add_set = set(add)
    gone = [r for r in rem if r and GUARD.search(r) and r not in add_set]
    if not gone:
        continue
    if EXCUSE.search("\n".join(add)):
        continue          # same file explains itself
    hits.extend((f, r) for r in gone)

if not hits:
    print("  \033[32mno unexplained guard/fallback removals\033[0m"); sys.exit(0)
print(f"  \033[33m{len(hits)} guard/fallback line(s) removed with nothing "
      f"replacing them:\033[0m")
for f, r in hits[:6]:
    print(f"      - {f}: {r[:80]}")
if len(hits) > 6:
    print(f"      … {len(hits) - 6} more")
print("  \033[33m→ AGENTS.md: 삭제 전에 어떤 사고가 이걸 만들었는지 먼저 찾을 것. "
      "의도한 삭제면 무시해도 됨\033[0m")
sys.exit(2)
PY8
rc=$?
[[ $rc -eq 1 ]] && fail=1
[[ $rc -eq 2 ]] && warn=1

# ---- 9. AGENTS.md number drift — WARN ---------------------------------
echo "── 9. AGENTS.md numbers vs live code ──"
python3 - <<'PY9'
# Borrowed from oh-my-hermes' maintenance/drift.py: hardcoded counts and
# budgets in docs drift away from the values they describe, and nothing
# notices. Reports EVERY drift in one pass (drift.py's own choice) so a
# doc pass fixes them together instead of one per push.
#
# Two comparisons, both mechanical:
#   (a) env-var defaults AGENTS.md states vs os.getenv() in src/
#   (b) the src/bot.py line count AGENTS.md quotes
import re, sys
from pathlib import Path

doc = Path("AGENTS.md")
if not doc.is_file():
    print("  \033[33mskipped — AGENTS.md not found\033[0m"); sys.exit(0)
text = doc.read_text(encoding="utf-8")
findings = []

# (a) env defaults ------------------------------------------------------
code_defaults = {}
for f in Path("src").rglob("*.py"):
    for m in re.finditer(r'os\.getenv\(\s*["\'](\w+)["\']\s*,\s*["\']([^"\']+)["\']',
                         f.read_text(encoding="utf-8")):
        code_defaults.setdefault(m.group(1), m.group(2))

# Only the explicit "(default N)" form. The `NAME=N` form is NOT a code
# default in this file — AGENTS.md uses it for LIVE .env values, several
# of which deliberately differ from the code ("코드 기본값은 여전히 1이라
# .env가 이긴다" for LOCAL_RERANKER_ENABLED, 4≠8 for
# URL_WORK_CONCURRENCY). Matching those reported a documented decision as
# drift, which is how a check earns being ignored.
claimed = {}
for m in re.finditer(r'`?(\b[A-Z][A-Z0-9_]{3,})`?[^\n]{0,40}?\(default\s+(\d+)\)', text):
    claimed.setdefault(m.group(1), m.group(2))

for name, want in claimed.items():
    have = code_defaults.get(name)
    if have is not None and have != want:
        findings.append(f"{name}: AGENTS.md says {want}, code default is {have}")

# (b) bot.py size -------------------------------------------------------
bot = Path("src/bot.py")
if bot.is_file():
    lines = sum(1 for _ in bot.open(encoding="utf-8"))
    # Only a SIZE claim — `bot.py(~15.1k줄)`. A bare "~17k lines" also
    # appears in the CodeGraph trigger sentence, which is a threshold,
    # not a claim about today's size; matching it made this check report
    # its own trigger line as drift.
    for m in re.finditer(r"bot\.py\(~?(\d+(?:[.,]\d+)?)k\s*(?:줄|lines)", text):
        claim_k = float(m.group(1).replace(",", "."))
        if abs(lines / 1000 - claim_k) >= 0.3:
            findings.append(
                f"src/bot.py: AGENTS.md says ~{m.group(1)}k lines, actual "
                f"{lines:,}")
    # Threshold read OUT of AGENTS.md, not hardcoded here — the whole
    # point of this section is that a number in one place drifts from
    # the number in another, and a copy of it in this script would be
    # the next instance of exactly that.
    trig = re.search(r"hits\s+~?(\d+)k\s+lines", text)
    if trig and lines >= int(trig.group(1)) * 1000:
        findings.append(
            f"BACKLOG TRIGGER REACHED — src/bot.py is {lines:,} lines and "
            f"AGENTS.md's CodeGraph trial fires at ~{trig.group(1)}k "
            "(owner's call, do not start it unasked)")

if not findings:
    print("  \033[32mno drift (env defaults + bot.py size)\033[0m"); sys.exit(0)
print(f"  \033[33m{len(findings)} drift finding(s) — fix them in one pass:\033[0m")
for f in findings:
    print(f"      · {f}")
sys.exit(2)
PY9
rc=$?
[[ $rc -eq 1 ]] && fail=1
[[ $rc -eq 2 ]] && warn=1

# ---- summary ----------------------------------------------------------
echo
if [[ $fail -ne 0 ]]; then
    echo "${RED}✗ preflight FAILED — blocking issue above. Do NOT push.${RST}"
    exit 1
elif [[ $warn -ne 0 ]]; then
    echo "${YEL}⚠ preflight passed with warnings — glance at sections 2/4/7/8/9 above.${RST}"
    exit 0
else
    echo "${GRN}✓ preflight clean.${RST}"
    exit 0
fi
