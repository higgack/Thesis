# Kami 검토 — 문서 디자인 시스템 (optional, 부차 산출물용)

> 대상: [tw93/Kami](https://github.com/tw93/Kami) (MIT; 일부 폰트 별도 라이선스) —
> AI 에이전트가 **일관된 스타일의 전문 문서**를 생성하게 하는 제약 기반 디자인 시스템.
> 결론: **논문 본문엔 부적합, 부차 산출물엔 조건부 유용.**

## 무엇인가
- 9개 템플릿(One-Pager, Resume, **Slides**, Letter, Portfolio, **Equity Report**, **Changelog**, Landing Page, **Long Doc**).
- 통일된 디자인 언어(파치먼트 캔버스·잉크블루·세리프), **한/영/중/일** 지원, 인라인 SVG 다이어그램(17종, Mermaid 포함).
- 출력: **WeasyPrint PDF**, 편집가능 **PPTX**, **Marp 슬라이드**, 배포형 웹사이트. 스택: HTML/CSS/Python.
- Claude Code·Codex·Claude Desktop 등에서 동작하는 *문서 생성 디자인 시스템*(마크다운 에디터·지식관리 아님).

## 우리 프로젝트 적용성
| 용도 | 적합도 | 비고 |
|---|---|---|
| 논문 **본문/제출본** | ❌ | 학교 양식(템플릿)이 우선이고, academic-pipeline IRON RULE = "**PDF는 LaTeX에서 컴파일**(HTML→PDF 금지)". Kami는 WeasyPrint(HTML→PDF) 경로라 충돌. |
| **부차 산출물** | ★★☆ | 연구 **요약 one-pager**, **발표 슬라이드**, **Changelog**, 예쁜 비공식 PDF 등엔 유용. 한국어 지원 플러스. |
| 슬라이드 | △ | 이미 검토한 **ppt-master**(편집형 .pptx)와 역할 일부 겹침. Kami는 Marp/디자인 통일성 강점. |

## 결론 / 사용 시점
- 학위논문 **공식 제출본엔 미사용**(학교 양식 + LaTeX 규칙).
- 필요 시 **요약본·슬라이드·홍보용 one-pager**를 빠르게 보기 좋게 뽑을 때 opt-in. 폰트 상용 라이선스(TsangerJinKai02 등) 주의.

> 설치/사용은 업스트림 README 참조(템플릿 + Python 렌더러). 도입 전 폰트 라이선스 확인.
