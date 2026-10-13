"""Mark every change between two versions of a manuscript (markdown) for review:
added or changed text in yellow ([..]{.mark}), deleted text struck through (~~..~~, red in the Word file).

Usage: python3 tools/diff_mark.py OLD.md NEW.md OUT.md [OUT.docx]
       e.g. python3 tools/diff_mark.py manuscript_v67_ko.md manuscript_v69_ko.md manuscript_v69_ko_changes.md manuscript_v69_ko_changes.docx

Existing yellow marks in either file are ignored (the comparison is on the plain text). Paragraphs are aligned
first, then words within changed paragraphs (Hangul runs, Latin/number runs, spaces and single punctuation marks are
the units, so a changed particle marks only that word; text only added inside a word marks just the addition). Markdown syntax (|, *, #, [, ], !, `, line breaks) is never
put inside a mark; deleted text may not contain it (checked).
Checks: OUT without the struck text and the marks is NEW; OUT without the marked text and the strikes is OLD
(exactly, up to runs of spaces and blank lines, which markdown renders the same). The Word file is built like the manuscripts (pandoc, docx_polish --caption-pt 10 --front-black).
"""
import re, os, sys, difflib, pathlib

MARK = re.compile(r'\[([^\[\]]*)\]\{\.mark\}')
TOK = re.compile(r"[가-힣]+|[A-Za-z0-9À-ÿ]+|\s+|.", re.S)
UNSAFE = re.compile(r'([\n|*#\[\]!`_~])')

def plain(t):
    return MARK.sub(r'\1', t)

def wrap(chunk, kind):
    """chunk of added (kind='+') or deleted (kind='-') text -> marked markdown. Syntax characters stay outside the
    marks (and may not be deleted); spaces at the ends stay outside too (pandoc needs ~~ and [ next to text)."""
    out = []
    for seg in UNSAFE.split(chunk):
        if not seg:
            continue
        if UNSAFE.fullmatch(seg):
            assert kind == '+', f'deleted text contains markdown syntax: {chunk[:80]!r}'
            out.append(seg); continue
        core = seg.strip()
        if not core:
            out.append(seg); continue
        lead = seg[:len(seg) - len(seg.lstrip())]; tail = seg[len(seg.rstrip()):]
        out.append(lead + (f'[{core}]{{.mark}}' if kind == '+' else f'~~{core}~~') + tail)
    return ''.join(out)

def diff_tokens(a, b):
    ta, tb = TOK.findall(a), TOK.findall(b)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op == 'equal':
            out.append(''.join(ta[i1:i2])); continue
        o, n = ''.join(ta[i1:i2]), ''.join(tb[j1:j2])
        if op == 'replace':   # text only added inside a word ("기술융합" -> "기술융합과 지식 통합"): mark just the addition
            k = len(os.path.commonprefix([o, n])); o2, n2 = o[k:], n[k:]
            m = len(os.path.commonprefix([o2[::-1], n2[::-1]]))
            mid = o2[:len(o2) - m] or n2[:len(n2) - m]
            if (not o2[:len(o2) - m] or not n2[:len(n2) - m]) and mid == mid.strip() and not UNSAFE.search(mid):
                out.append(o[:k] + wrap(o2[:len(o2) - m], '-') + wrap(n2[:len(n2) - m], '+') + o2[len(o2) - m:]); continue
        if op in ('delete', 'replace'):
            out.append(wrap(o, '-'))
        if op in ('insert', 'replace'):
            out.append(wrap(n, '+'))
    return ''.join(out)

def diff_text(old, new):
    pa, pb = old.split('\n\n'), new.split('\n\n')
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, pa, pb, autojunk=False).get_opcodes():
        if op == 'equal':
            out.extend(pa[i1:i2])
        elif op == 'insert':
            out.extend(wrap(p, '+') for p in pb[j1:j2])
        elif op == 'delete':
            out.extend(wrap(p, '-') for p in pa[i1:i2])
        else:
            out.append(diff_tokens('\n\n'.join(pa[i1:i2]), '\n\n'.join(pb[j1:j2])))
    return '\n\n'.join(out)

def norm_ws(t):
    return re.sub(r'\n{3,}', '\n\n', re.sub(r'[ \t]+', ' ', t).replace(' \n', '\n'))   # an added or deleted paragraph leaves a blank line

def reconstruct(t, keep):
    """keep='new': drop struck text, unwrap marks; keep='old': drop marked text, unwrap strikes."""
    if keep == 'new':
        return MARK.sub(r'\1', re.sub(r'~~[^~]*~~', '', t))
    return re.sub(r'~~([^~]*)~~', r'\1', MARK.sub('', t))

if __name__ == '__main__':
    old_f, new_f, out_md = sys.argv[1:4]
    old = plain(pathlib.Path(old_f).read_text(encoding='utf-8'))
    new = plain(pathlib.Path(new_f).read_text(encoding='utf-8'))
    out = diff_text(old, new)
    assert norm_ws(reconstruct(out, 'new')) == norm_ws(new), 'reconstruction of NEW failed'
    assert norm_ws(reconstruct(out, 'old')) == norm_ws(old), 'reconstruction of OLD failed'
    pathlib.Path(out_md).write_text(out, encoding='utf-8')
    n_add = len(MARK.findall(out)); n_del = len(re.findall(r'~~[^~]*~~', out))
    print(f'marked: {n_add} added/changed spans (yellow), {n_del} deleted spans (struck); reconstruction of both versions OK')
    if len(sys.argv) > 4:
        import pypandoc, subprocess, docx
        from docx.shared import RGBColor
        out_docx = sys.argv[4]
        pypandoc.convert_file(out_md, 'docx', outputfile=out_docx, extra_args=['--resource-path=.:figures', '--dpi=300'])
        subprocess.run([sys.executable, str(pathlib.Path(__file__).parent / 'docx_polish.py'), out_docx, out_docx,
                        '--caption-pt', '10', '--front-black'], check=True)
        d = docx.Document(out_docx); n = 0
        from docx.oxml.ns import qn
        for r in d.element.body.iter(qn('w:r')):
            rpr = r.find(qn('w:rPr'))
            if rpr is not None and rpr.find(qn('w:strike')) is not None:
                from docx.text.run import Run
                Run(r, None).font.color.rgb = RGBColor(0xC0, 0x00, 0x00); n += 1
        d.save(out_docx)
        print('docx ->', out_docx, '| struck runs colored red:', n)
