# ittae Codex 지침

이 파일은 Codex 및 `AGENTS.md` 호환 도구가 ittae 조직의 코딩/PR 리뷰에서 따라야 할 언어/리뷰 정책입니다. 기준 정책은 `.github/instructions/ai-pr-review-language.instructions.md`와 동일하게 유지합니다.

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

## AI Review guidelines

- 모든 PR은 기본적으로 AI 리뷰 대상입니다.
- auth, permissions, secrets, privacy, payments, user data 주변의 security regression을 높은 우선순위로 봅니다.
- 동작이 바뀌는 변경에서 test가 없거나 약하면 지적합니다.
- PR 설명에 드러나지 않은 risky behavior change를 지적합니다.
- 런타임 동작에 영향을 주는 변경인데 `Real Behavior Proof`가 부족하면 지적합니다.
- PR의 `AI Review Focus`가 위험 영역과 검증 부족 영역을 명확히 설명하는지 확인합니다.
- user-facing copy, trust, brand quality에 영향을 주지 않는 단순 typo는 과도하게 우선순위를 높이지 않습니다.
- 리뷰 코멘트는 가능한 한 file/line, risk level, suggested fix를 포함해 실행 가능하게 작성합니다.
- PR title이 `type: ITT-123 ...` 형식인지 확인합니다. ITT 키가 type 앞 prefix(`ITT-462: ...`)나 대괄호(`[ITT-462] ...`)로 들어가 있거나 Conventional Commits 타입이 없으면 지적합니다.
- 변경 성격이 UI/시각(Flutter, React, web/mobile UI, screenshot 생성, design/layout/component)인데 screenshot/preview도 미첨부 사유도 없으면 지적합니다. 적용 기준은 저장소 이름이 아니라 변경 성격입니다.
- branch·title·body 중 어디에도 `ITT-123` 키가 없어 Multica 자동 연결이 안 되는 PR이면 지적합니다.
