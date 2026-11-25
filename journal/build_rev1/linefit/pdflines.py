"""Parse pdftotext -bbox-layout output into lines with word boxes. usage: pdflines.py bbox.html out.json"""
import sys, re, json, html
src = open(sys.argv[1], encoding='utf8').read()
pages = []
for pm in re.finditer(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', src, re.S):
    lines = []
    for lm in re.finditer(r'<line xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</line>', pm.group(3), re.S):
        words = [(float(a), float(b), float(c), float(d), html.unescape(t)) for a, b, c, d, t in
                 re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', lm.group(5))]
        lines.append(words)
    pages.append(lines)
json.dump(pages, open(sys.argv[2], 'w', encoding='utf8'), ensure_ascii=False)
print(len(pages), 'pages', sum(len(l) for l in pages), 'lines')
