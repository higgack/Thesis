#!/bin/bash
# usage: render.sh deck.pptx outdir  -> outdir/<name>.pdf + outdir/<name>-NN.png (preview; 맑은 고딕 is replaced by Noto Sans CJK)
set -e
IN=$1; OUT=$2; N=$(basename "$IN" .pptx); T=$(mktemp -d)
cp "$IN" $T/$N.pptx
(cd $T && timeout 300 soffice --headless --convert-to odp $N.pptx >/dev/null 2>&1)
mkdir $T/x && (cd $T/x && unzip -q ../$N.odp)
python3 - "$T/x" <<'PY'
import sys, re, os
d = sys.argv[1]
for f in ('content.xml', 'styles.xml'):
    p = os.path.join(d, f); t = open(p, encoding='utf8').read()
    t = re.sub(r'style:text-autospace="[^"]*"', '', t)
    t = t.replace('<style:paragraph-properties', '<style:paragraph-properties style:text-autospace="none"')
    t = re.sub(r'(<style:(?:style|default-style)\b[^>]*>)(?!\s*<style:paragraph-properties)', r'\1<style:paragraph-properties style:text-autospace="none"/>', t)
    open(p, 'w', encoding='utf8').write(t)
PY
(cd $T/x && rm -f ../$N.odp && zip -q -X -0 ../$N.odp mimetype && zip -q -X -r ../$N.odp . -x mimetype)
(cd $T && timeout 300 soffice --headless --convert-to pdf $N.odp >/dev/null 2>&1)
mkdir -p "$OUT"; cp $T/$N.pdf "$OUT/$N.pdf"; rm -rf $T
python3 -c "
import pymupdf; d=pymupdf.open('$OUT/$N.pdf')
for i,p in enumerate(d): p.get_pixmap(dpi=${DPI:-80}).save('$OUT/${N}-%02d.png'%(i+1))
print('pages', d.page_count)"
