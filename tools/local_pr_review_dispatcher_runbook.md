# Local Hermes PR Review Gate — dispatcher 운영 가이드

이 문서는 [ITT-276](https://github.com/ittae/ittae) "AI 리뷰 GitHub Action을 로컬 Hermes PR Review Gate로 대체" 의 Phase 0 산출물인 `tools/local_pr_review_dispatcher.py` 사용법을 정리합니다.

## 위치 / 패키징

- Source-of-truth: `ittae/.github` repo `tools/local_pr_review_dispatcher.py`.
- 로컬 Hermes 런타임에는 [`tools/sync_runtime_copy.py`](https://github.com/ittae/.github) 와 동일한 패턴 (ITT-196) 으로 `~/.hermes/workspace/tools/local_pr_review_dispatcher.py` 에 복사해 사용할 예정입니다 (실제 sync는 Phase 1에서 wire-up).

## Phase 0 범위 (현재)

이 스크립트는 의도적으로 **dry-run only** 입니다.

- 입력: GitHub PR URL
- 동작: PR 메타데이터 수집 → `gate-classify` 포팅 (size limit + high-risk path 매칭) → agent dispatch 페이로드 빌드 → stdout JSON 출력
- **GitHub mutation 없음**: 라벨/코멘트는 `would_apply_labels`, `would_post_comments` 로 페이로드에 기술만 합니다.
- live wire (실제 라벨/코멘트 게시, 실제 agent 실행) 는 Phase 1+ 작업입니다. `--dry-run` 누락 시 argparse 단계에서 hard fail 합니다.

## 실행

```bash
# 1) 실제 PR 메타데이터를 gh CLI로 수집 (gh auth login 필요)
python3 tools/local_pr_review_dispatcher.py \
  --pr-url https://github.com/ittae/ittae/pull/425 \
  --dry-run

# 2) 캡처된 fixture 로 replay (gh 호출 없음, 테스트/디버그용)
python3 tools/local_pr_review_dispatcher.py \
  --pr-url https://github.com/ittae/ittae/pull/425 \
  --dry-run \
  --fixture tools/fixtures/ittae_ittae_pr_425.json \
  --roster tools/fixtures/roster_phase0.json
```

기본값:

| 옵션 | 기본 | 비고 |
|---|---|---|
| `--pr-size-limit` | 1500 | `claude-code-review.yml::pr_size_limit` 와 동일 |
| `--high-risk-paths` | (regex) | `claude-code-review.yml::high_risk_paths` 와 동일 |
| `--model` | `claude-opus-4-7` | `claude-code` agent 만 사용 |
| `--roster` | `~/.hermes/workspace/tools/reviewer_roster.json` | 누락 시 `--roster` 로 명시적 경로 지정 |
| `--fixture` | (없음) | 지정 시 gh 호출 생략 |

## 출력 스키마 (요약)

```jsonc
{
  "version": "0.1.0",
  "mode": "dry-run",
  "pr": {
    "repo": "ittae/ittae",
    "number": 425,
    "url": "...",
    "title": "...",
    "head_sha": "...",
    "head_ref": "...",
    "base_ref": "...",
    "additions": 4,
    "deletions": 96,
    "total": 100,
    "files": ["..."],
    "author": "get6",
    "state": "MERGED"
  },
  "classify": {
    "size_limit": 1500,
    "pr_size": 100,
    "high_risk_paths_regex": "...",
    "matched_high_risk_files": [],
    "verdict": "proceed",           // "proceed" | "too-large"
    "is_high_risk": false,
    "would_apply_labels": [],       // Phase 1 라이브 시 부여될 라벨
    "would_post_comments": []       // Phase 1 라이브 시 게시될 코멘트
  },
  "dispatch": {
    "decision": "dispatch",         // "dispatch" | "skip"
    "reason": "standard",           // "standard" | "high-risk-needs-human-merge" | "pr-too-large"
    "agents": [
      {"name": "claude-code", "role": "required", "model": "claude-opus-4-7", "perspectives": ["Security", "..."]},
      {"name": "codex", "role": "required"},
      {"name": "gemini", "role": "supplementary"},
      {"name": "copilot", "role": "supplementary"}
    ],
    "agent_input": {
      "repo": "ittae/ittae",
      "pr_number": 425,
      "head_sha": "...",
      "is_high_risk": false,
      "high_risk_files": []
    },
    "ai_review_meta_template": {
      "agent": "<filled-by-agent>",
      "iteration": 0,
      "score": null,
      "verdict": "PASS",
      "categories": [],
      "head_sha": "..."
    }
  }
}
```

## gate-classify 의 분기

기존 [`.github/workflows/claude-code-review.yml`](https://github.com/ittae/.github) `gate-classify` 잡과 동일한 분기:

1. `additions + deletions > pr_size_limit` → `verdict=too-large`, label `too-large`, "PR 크기 X줄 — Y줄 초과" 코멘트. agent dispatch 는 `skip`.
2. 위 단계를 통과하고 high-risk regex 에 매치되는 파일이 있으면 → `verdict=proceed`, label `high-risk` + `needs-human-review`, "민감 영역 변경 감지" 코멘트. agent dispatch 는 진행하되 `reason=high-risk-needs-human-merge` 로 표시 (Phase 1 에서 `ai-approved` 라벨 부여 차단).
3. 그 외 → `verdict=proceed`, `reason=standard`.

코멘트 본문은 워크플로 원문을 거의 그대로 가져와서 dual-run 비교가 쉽도록 했습니다.

## 테스트

```bash
python3 tools/tests/test_local_pr_review_dispatcher.py
```

22개 테스트 통과 확인:

- URL 파싱 (canonical / trailing slash / non-GitHub / issue URL / relative path)
- fixture 로딩 (ittae/ittae#425 캡처본)
- gate-classify 분기 (proceed / too-large / high-risk / 잘못된 regex / boundary)
- dispatch 페이로드 (active agent 포함, 비활성 reviewer 제외, too-large 시 `skip`, high-risk 시 reason)
- end-to-end replay (PR 425, synthetic high-risk, synthetic too-large)
- CLI (정상 실행 / `--dry-run` 누락 시 nonzero / 잘못된 URL 시 1)

## Fixture

- `tools/fixtures/ittae_ittae_pr_425.json` — 실제 닫힌 PR `ittae/ittae#425` (chore: ITT-180 org default PR 템플릿 사용) 의 `gh pr view --json` 캡처. proceed / not-high-risk 케이스.
- `tools/fixtures/synthetic_high_risk.json` — `lib/features/auth/...` + `pubspec.yaml` 변경. proceed / high-risk 케이스.
- `tools/fixtures/synthetic_too_large.json` — 합산 2000줄 변경. too-large 케이스.
- `tools/fixtures/roster_phase0.json` — `~/.hermes/workspace/tools/reviewer_roster.json` 의 Phase 0 스냅샷.

추후 실제 PR 캡처를 추가하려면 다음과 같이 갱신합니다:

```bash
gh pr view <NUM> --repo <OWNER>/<REPO> \
  --json number,title,additions,deletions,files,headRefOid,headRefName,baseRefName,state,url,author \
  > tools/fixtures/<owner>_<repo>_pr_<num>.json
```

## Phase 1+ 에서 추가될 항목 (현 PR 범위 밖)

- live wire: `would_apply_labels` / `would_post_comments` 실제 적용 (`gh pr edit`, `gh pr comment`).
- 실제 agent dispatch: Multica issue 생성 또는 직접 CLI 호출.
- `review_followup.py` 와 연동: 라벨 부여 + GitHub Check (`hermes/pr-review-gate`) 게시.
- `claude.yml` mention responder 통합 — **별도 후속 이슈** (자동 PR 리뷰 마이그레이션 안정화 후, Phase 2 + 1주 dual-run 후).
- `sync_runtime_copy.py` 패턴으로 `~/.hermes/workspace/tools/` 자동 동기화.

## 참고

- ITT-276 plan 코멘트: GitHub 에는 게시되지 않음 (Multica issue 내부).
- ITT-276 결정 코멘트 (`8817fcaa-f8d1-4c1d-9367-b6d471d0cd8b`): Phase 0 시작 승인 + dispatcher 위치 (b) 결정.
- 관련 워크플로: [`.github/workflows/claude-code-review.yml`](https://github.com/ittae/.github), [`.github/workflows/claude-review-light.yml`](https://github.com/ittae/.github).
