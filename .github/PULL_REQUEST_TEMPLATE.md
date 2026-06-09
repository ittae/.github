<!-- 작은 PR로 올리기: 한 번에 200줄 이하, 이상적으로 50줄 내외의 변경만 포함시키는 것이 좋아요 -->

## 🌷 Summary
<!-- TL;DR처럼 한눈에 이해할 수 있게 설명 -->


## 📢 Description
<!-- 변경 이유, 주요 변경점, 배경 등 상세 설명 -->


## 🐙 Related Issue
<!-- 연관 이슈 번호 또는 링크 (예: close #123) -->
<!-- 이슈가 있을 때만 아래 예시를 실제 번호로 바꾸세요. -->
<!-- close #123 -->

## Real Behavior Proof (필수)

> CI, unit test, lint 결과만으로는 충분하지 않습니다.
> 실제 실행 로그, 스크린샷, 녹화, 터미널 출력, 또는 실제 환경 관측 결과를 첨부하세요.

- 실제 실행 환경:
- 실행한 명령 / 조작:
- 결과 증거:
- 검증한 시나리오:
- 검증하지 않은 영역:
- 증거가 부족하다면 그 이유:

## AI Review Focus

- 위험한 변경 영역:
  - [ ] auth / permission / privacy
  - [ ] payment / monetization
  - [ ] data migration / storage
  - [ ] dependency / build / CI
  - [ ] broad refactor / behavior change
  - [ ] user-facing copy / brand quality
  - [ ] 없음

- AI 리뷰어가 특히 봐야 할 점:
- 테스트나 증거가 부족할 수 있는 영역:

## Human Decision Needed

- [ ] 없음 — 에이전트 판단으로 진행 가능
- [ ] 제품 / UX 결정 필요
- [ ] 비용 / 수익 영향 결정 필요
- [ ] 보안 / 데이터 위험 승인 필요
- [ ] 릴리즈 타이밍 결정 필요

필요하다면 설명:


## UI 변경 시각 증빙 (변경 성격 기준)

> 적용 기준은 저장소 이름이 아니라 변경 성격입니다. Flutter, React, web/mobile UI, screenshot 생성, design/layout/component 변경처럼 결과를 눈으로 봐야 판단되는 변경이면 저장소와 무관하게 시각 증빙이 필요합니다.

- [ ] 이 PR은 위 성격의 UI/시각 변경을 포함하지 않음
- [ ] UI/시각 변경 포함 → 아래 중 하나를 채움
  - screenshot/preview 첨부: <이미지 또는 링크>
  - 미첨부 사유: <왜 첨부가 불가능/불필요한지>

## Commit / PR Title Convention

- ✅ `type: <한국어 설명>` 사용
  - 예: `feat: 로그인 화면 에러 메시지 다국어 처리`
- Conventional Commits 타입을 따른다: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `ci`, `build`, `style`, `revert`
- Multica 이슈 키(`ITT-123`)는 type 뒤에 둔다. title 맨 앞 prefix나 대괄호로 쓰지 않는다.
  - ✅ `ci: ITT-462 add PR visual preview artifact`
  - ✅ `feat: ITT-123 로그인 에러 메시지 다국어 처리`
  - ✅ `fix: ITT-123 ...`
  - ❌ `ITT-462: ...`
  - ❌ `[ITT-462] ...`
  - ❌ `Add ...` (type 없음)

## PR 마무리 체크리스트

- [ ] PR title이 `type: ITT-123 ...` 형식이다 (type 뒤에 ITT 키, 한국어 설명).
- [ ] PR body가 Korean-first로 작성됐다.
- [ ] UI 변경이면 screenshot/preview를 첨부했거나, 미첨부 사유를 적었다.
- [ ] branch 이름·title·body 중 하나 이상에 `ITT-123` 키가 있어 Multica 이슈에 자동 연결된다 (commit message·PR comment만으로는 연결되지 않음).
