# 마감(Finalize) 안내 — 형식 변환 & 학교 양식

최종 본문은 **`manuscript/draft.md`** (한국어, APA 7판 기준, 회귀 표 삽입 완료)입니다.
이 환경에는 pandoc·LaTeX(tectonic/xelatex)·한글 폰트가 없어 PDF/DOCX를 여기서 생성하지 못했습니다. 아래 방법으로 로컬에서 변환하세요.

## DOCX 변환 (가장 간단)
```bash
pandoc manuscript/draft.md -o thesis.docx
```

## PDF 변환 (한글 포함, LaTeX 경유 — 권장)
```bash
# 한글 폰트 + xelatex 사용
pandoc manuscript/draft.md -o thesis.pdf \
  --pdf-engine=xelatex \
  -V mainfont="Noto Serif CJK KR" \
  -V geometry:margin=1in
```
- macOS: `brew install pandoc` + MacTeX(또는 `brew install --cask mactex-no-gui`)
- Ubuntu: `sudo apt install pandoc texlive-xetex fonts-noto-cjk`

## 학교 양식(템플릿)이 나오면
양식(.docx/.hwp/LaTeX class)을 주시면, 다음을 그 양식에 맞춰 재배치합니다(내용 불변):
1. 표지·목차·국문초록/Abstract 서식
2. 장·절 번호 체계, 글꼴/줄간격/여백
3. 표/그림 캡션 양식, 참고문헌 스타일(학과 지정 시 APA→해당 양식)
4. 원본 그림/세부 표(①②) 이미지 삽입(부록 목록 참조)

→ 이때 `academic-paper`의 `format-convert`로 재처리하면 됩니다.

## 남은 형식 작업(양식 확정 후)
- [ ] 원본 그림 11개·세부 표 7개 이미지 삽입(부록 목록)
- [ ] 표지/심사 페이지/감사의 글 등 학과 필수 전면부
- [ ] 참고문헌 최종 정렬(가나다→알파벳) 및 학과 지정 스타일 적용
