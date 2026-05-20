# ittae Gemini 지침

이 파일은 Gemini 계열 도구가 ittae 조직의 PR을 리뷰할 때 따라야 할 언어/리뷰 정책입니다. 기준 정책은 `.github/instructions/ai-pr-review-language.instructions.md`와 동일하게 유지합니다.

## 언어 정책

- 기본 응답 언어는 한국어입니다.
- PR 리뷰 요약, finding, inline comment, suggestion, recommendation, 최종 판단은 한국어로 작성합니다.
- code, command, file path, API name, identifier, package name, model/provider name, status enum, 인용한 error message는 원문을 유지합니다.
- diff, GitHub UI, CI log, 외부 문서가 영어여도 설명과 판단은 한국어로 작성합니다.
- 영어 원문을 인용해야 하면 그대로 인용하고, 해설은 한국어로 작성합니다.

## 리뷰 초점

- 간결하고 신호가 높은(high-signal) 리뷰 코멘트를 우선합니다.
- correctness, security, maintainability, tests, release risk를 우선 검토합니다.
- 실제 영향이 작은 nitpick은 피합니다.
- 작고 안전한 수정은 가능한 한 구체적인 suggestion block으로 제안합니다.
- 사람 승인이 필요한 경우, 필요한 이유를 한국어로 명확히 씁니다.
