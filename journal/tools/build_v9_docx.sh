#!/bin/sh
# Rebuild every v9 DOCX from its markdown and apply the table polish.
set -e
cd "$(dirname "$0")/.."
python3 - <<'PY'
import pypandoc
for f in ('manuscript_v9_ko','supplement_S1_v9_ko','supplement_S2_v9_ko',
          'manuscript_v9_ko_JTI','manuscript_v9_en_TASM','supplement_v9_en'):
    pypandoc.convert_file(f+'.md','docx',outputfile=f+'.docx',extra_args=['--resource-path=.:figures'])
    print('pandoc', f)
PY
python3 tools/docx_polish.py manuscript_v9_ko.docx manuscript_v9_ko.docx
python3 tools/docx_polish.py supplement_S1_v9_ko.docx supplement_S1_v9_ko.docx
python3 tools/docx_polish.py supplement_S2_v9_ko.docx supplement_S2_v9_ko.docx
python3 tools/docx_polish.py manuscript_v9_ko_JTI.docx manuscript_v9_ko_JTI.docx --ea-font "바탕"
python3 tools/docx_polish.py manuscript_v9_en_TASM.docx manuscript_v9_en_TASM.docx --body-pt 11
python3 tools/docx_polish.py supplement_v9_en.docx supplement_v9_en.docx
