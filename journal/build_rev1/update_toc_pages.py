"""목차·표 목차·그림 목차에 저장된 쪽번호를 렌더링 결과에 맞춰 고친다.

usage: python3 update_toc_pages.py DOCX PAGES.json
PAGES.json: 최종본을 PDF로 바꾼 뒤 쪽마다 인쇄 쪽번호(label)와 줄(lines)을 담은 목록.
Word에서 필드를 갱신하면 Word가 다시 계산하므로, 이 값은 그 전까지 보이는 저장값이다.
"""
import sys, json, re
import docx
from docx.oxml.ns import qn

W = lambda t: qn('w:' + t)
path, pj = sys.argv[1], sys.argv[2]
pages = json.load(open(pj, encoding='utf8'))
norm = lambda s: re.sub(r'\s+', '', s)
body_pages = [pg for pg in pages if pg['label'] and pg['label'].isdigit()]
front_pages = [pg for pg in pages if pg['label'] and not pg['label'].isdigit()]

def find_page(title):
    key = norm(title)
    pool = front_pages if key == '국문요지' else body_pages
    for pg in pool:                       # 1) 제목과 똑같은 줄
        if any(norm(l) == key for l in pg['lines']):
            return pg['label']
    m = re.match(r'^(표\d+\.|그림\d+\.)', key)
    if m:                                 # 2) 표·그림: 같은 번호로 시작하는 제목 줄(목차용 제목은 짧게 줄인 것)
        for pg in pool:
            if any(norm(l).startswith(m.group(1)) for l in pg['lines']):
                return pg['label']
    for pg in pool:                       # 3) 제목이 두 줄로 나뉜 경우
        if any(len(norm(l)) > 8 and key.startswith(norm(l)) for l in pg['lines']):
            return pg['label']
    return None

d = docx.Document(path)
changed, missing = 0, []
for p in d.paragraphs:
    if p.style.name not in ('toc 1', 'toc 2', 'toc 3'):
        continue
    # 제목: 첫 탭 앞까지의 텍스트
    title = ''
    for r in p._p.iter(W('r')):            # 문단 속성의 탭 정의(w:tabs)는 건너뛰고 글자 런만 본다
        if r.find(W('tab')) is not None:
            break
        title += ''.join(t.text or '' for t in r.findall(W('t')))
    if not title.strip():
        continue
    label = find_page(title)
    if label is None:
        missing.append(title); continue
    # PAGEREF 필드의 결과 텍스트 교체
    state, done = None, False
    for r in p._p.iter(W('r')):
        fc = r.find(W('fldChar'))
        it = r.find(W('instrText'))
        if it is not None and 'PAGEREF' in (it.text or ''):
            state = 'instr'
        elif fc is not None and fc.get(W('fldCharType')) == 'separate' and state == 'instr':
            state = 'result'
        elif fc is not None and fc.get(W('fldCharType')) == 'end' and state == 'result':
            break
        elif state == 'result':
            t = r.find(W('t'))
            if t is not None:
                if not done:
                    if t.text != label: changed += 1
                    t.text = label; done = True
                else:
                    t.text = ''
    if not done:
        missing.append(title + ' (PAGEREF 없음)')
d.save(path)
print('toc pages updated:', changed, 'missing:', missing)
