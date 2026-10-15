"""Layout of the contents, table and figure lists in the user's Word file (thesis track, v83+).

Usage: python3 tools/list_layout.py IN.docx OUT.docx

When the user updated the fields in Word (v79), Word rebuilt the three lists with its own built-in TOC styles: the
table and figure entries (TC fields at level 3, so style "toc 3") got a left indent of four characters (880 twips), and
all entries inherit the document default alignment "both" (justified). The indent narrows the line so far that a long
entry such as "그림 1. 하이브리드 본딩의 기반 공정, 접합 형태, 소자 구조, 적용 제품" leaves no room for the dot leader
and the page number; when such an entry wraps, the justified first line is spread over its spaces.
This puts the lists back to the layout the thesis was built with (v78): table and figure entries flush left like the
chapter entries, and every list entry left-aligned. The change is made in the styles (so it survives the next field
update in Word) and the four-character indent Word wrote on each table/figure entry is removed. The text is not
touched; every other part of the file is copied byte for byte.
Audit (exit 1 on failure): each edit applies exactly as expected, and the visible text of every paragraph is unchanged.
"""
import re, sys, zipfile

TOC_NAMES = ('toc 1', 'toc 2', 'toc 3')


def fix_styles(x):
    n = 0
    for name in TOC_NAMES:
        m = re.search(r'<w:style [^>]*>(?:(?!</w:style>).)*?<w:name w:val="%s"/>.*?</w:style>' % name, x, re.S)
        assert m, name
        s = m.group(0)
        assert '<w:jc ' not in s, name
        if name == 'toc 3':
            assert s.count('<w:ind w:leftChars="400" w:left="850"/>') == 1, s
            s2 = s.replace('<w:ind w:leftChars="400" w:left="850"/>', '<w:ind w:leftChars="0" w:left="0"/><w:jc w:val="left"/>')
        elif '<w:pPr>' in s:
            s2 = s.replace('</w:pPr>', '<w:jc w:val="left"/></w:pPr>', 1)
        else:
            s2 = s.replace('</w:style>', '<w:pPr><w:jc w:val="left"/></w:pPr></w:style>')
        x = x.replace(s, s2, 1); n += 1
    return x, n


def toc3_id(styles):
    m = re.search(r'<w:style [^>]*w:styleId="([^"]+)"[^>]*>(?:(?!</w:style>).)*?<w:name w:val="toc 3"/>', styles, re.S)
    return m.group(1)


def fix_document(x, sid):
    # paragraphs of style toc 3 carry <w:ind w:left="880"/> written by Word; drop it so the style's indent (0) applies
    pat = re.compile(r'(<w:pPr><w:pStyle w:val="%s"/><w:tabs>(?:(?!</w:tabs>).)*</w:tabs>)<w:ind w:left="880"/>' % re.escape(sid))
    x2, n = pat.subn(r'\1', x)
    return x2, n


def texts(xml):
    out = []
    for p in re.findall(r'<w:p[ >].*?</w:p>', xml, re.S):
        out.append(''.join(re.findall(r'<w:t(?: [^>]*)?>([^<]*)</w:t>', p)))
    return out


def main(src, out):
    zin = zipfile.ZipFile(src)
    styles = zin.read('word/styles.xml').decode('utf8')
    doc = zin.read('word/document.xml').decode('utf8')
    sid = toc3_id(styles)
    styles2, ns = fix_styles(styles)
    doc2, nd = fix_document(doc, sid)
    n_toc3 = len(re.findall(r'<w:pStyle w:val="%s"/>' % re.escape(sid), doc))
    zout = zipfile.ZipFile(out, 'w')
    for info in zin.infolist():
        data = zin.read(info.filename)
        if info.filename == 'word/styles.xml':
            data = styles2.encode('utf8')
        elif info.filename == 'word/document.xml':
            data = doc2.encode('utf8')
        zout.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    zout.close()
    same = texts(doc) == texts(doc2)
    print(f'styles changed {ns}/3 (toc 3 = "{sid}"); list entries un-indented {nd} of {n_toc3}; visible text unchanged: {same}')
    sys.exit(0 if ns == 3 and nd == n_toc3 and nd > 0 and same else 1)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
