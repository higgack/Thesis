"""Mark the text revised in a new source version in red: every word of NEW that is not in OLD is wrapped in
[..]{.red} (v72+; the build turns {.red} into the Word character style "Red", coloured red by docx_polish).

Usage: python3 tools/red_mark.py sources/ko_v71 sources/ko_v72        (rewrites the NEW files in place)

Paragraphs are compared one to one (the two versions must have the same paragraphs). Within a changed paragraph the
units are Hangul runs, Latin/number runs, spaces and single punctuation marks; short unchanged islands (one or two
words) inside a revised stretch are coloured with it so a rewritten sentence reads as one red run. Existing yellow
marks ([..]{.mark}), bold/italic markers, escapes and table pipes stay outside the red spans, so a red span sits
inside a yellow one when revised text is highlighted. Check: NEW without the red spans is unchanged.
"""
import re, sys, difflib, pathlib

TOK = re.compile(r"\\.|\]\{\.mark\}|\]\{\.red\}|\[|\]|\*\*|\*|\||\n|\^|[가-힣]+|[A-Za-z0-9À-ÿ]+|[ \t]+|.", re.S)
SYNTAX = re.compile(r"\\.|\]\{\.mark\}|\]\{\.red\}|\[|\]|\*\*|\*|\||\n|\^")
RED = re.compile(r'\[([^\[\]]*)\]\{\.red\}')

def mark_paragraph(old, new):
    toks = TOK.findall(new)
    syn = [bool(SYNTAX.fullmatch(t)) for t in toks]
    content_old = [t for t in TOK.findall(old) if not SYNTAX.fullmatch(t)]
    idx = [i for i, s in enumerate(syn) if not s]
    content_new = [toks[i] for i in idx]
    changed = [False] * len(toks)
    sm = difflib.SequenceMatcher(None, content_old, content_new, autojunk=False)
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op in ('replace', 'insert'):
            for k in range(b1, b2):
                changed[idx[k]] = True
    # words only: spaces are decided below from their neighbours
    word = [not syn[i] and not toks[i].isspace() for i in range(len(toks))]
    for i in range(len(toks)):
        if not word[i]:
            changed[i] = False
    # absorb short unchanged islands (<= 2 words, no syntax) between revised words
    words = [i for i in range(len(toks)) if word[i]]
    k = 0
    while k < len(words):
        if changed[words[k]]:
            k += 1; continue
        j = k
        while j < len(words) and not changed[words[j]]:
            j += 1
        if 0 < k and j < len(words) and j - k <= 2:
            lo, hi = words[k - 1], words[j]
            if not any(syn[t] for t in range(lo, hi)):
                for t in range(k, j):
                    changed[words[t]] = True
        k = j
    # spaces between two revised words (no syntax in between) are revised too
    for i in range(len(toks)):
        if toks[i].isspace() and not syn[i]:
            p = i - 1
            while p >= 0 and toks[p].isspace() and not syn[p]: p -= 1
            n = i + 1
            while n < len(toks) and toks[n].isspace() and not syn[n]: n += 1
            if 0 <= p and n < len(toks) and word[p] and word[n] and changed[p] and changed[n]:
                changed[i] = True
    out, open_ = [], False
    for t, c in zip(toks, changed):
        if c and not open_:
            out.append('['); open_ = True
        elif not c and open_:
            out.append(']{.red}'); open_ = False
        out.append(t)
    if open_:
        out.append(']{.red}')
    res = ''.join(out)
    assert RED.sub(r'\1', res) == new, 'red marking changed the text'
    return res

def main(old_dir, new_dir):
    old_dir, new_dir = pathlib.Path(old_dir), pathlib.Path(new_dir)
    n_par = n_span = 0
    for f in sorted(new_dir.glob('0*.md')):
        new_text = f.read_text(encoding='utf-8')
        assert '{.red}' not in new_text, f'{f} is already marked'
        pa = (old_dir / f.name).read_text(encoding='utf-8').split('\n\n')
        pb = new_text.split('\n\n')
        assert len(pa) == len(pb), f'{f.name}: paragraph count differs ({len(pa)} vs {len(pb)})'
        out = []
        for a, b in zip(pa, pb):
            if a == b:
                out.append(b); continue
            m = mark_paragraph(a, b)
            n_par += 1; n_span += m.count(']{.red}')
            out.append(m)
        f.write_text('\n\n'.join(out), encoding='utf-8')
    print(f'red: {n_par} paragraphs, {n_span} spans')

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
