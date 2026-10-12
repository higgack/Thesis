# 2026 한양MOT 리서치데이 포스터 (A1 세로)

- 행사: 2026-10-31(토) 12:00~, 융합교육관 602·608호 및 6층 복도. 포스터 제출 마감 10-27(화) 17:00 (곽상숙, kwagss72@hanyang.ac.kr) — 기한 내 제출 시 행정팀 일괄 출력.
- **v1** = `poster_v1.pptx`(학과 A1 템플릿 기반, 글꼴 맑은 고딕, 차트는 PowerPoint 기본 차트라 직접 수정 가능) · `poster_v1.pdf`(미리보기; 이 환경에 맑은 고딕이 없어 Noto Sans CJK로 렌더링).
- 내용 출처: 학위논문 최종본 v59(수치·용어 동일).
- 다시 만들기: `cd poster/build && python3 build_poster.py template_A1.pptx assets icons ../poster_vN.pptx && ./render.sh ../poster_vN.pptx ..`
  (python-pptx 필요; 벤 다이어그램은 `make_venn.py`, 네트워크 그림은 `journal/figures/fig_core_network_v58_ko.png`에서 여백·범례를 잘라 냄.)
- 규칙: 포스터 문구를 고칠 때마다 새 버전 번호(v2, v3 …).
