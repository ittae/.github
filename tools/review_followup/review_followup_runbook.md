# PR Review Follow-up Runbook

## Goal

사용자가 Claude/Codex/Gemini/Copilot 리뷰를 직접 대조하지 않아도, Hermes가 PR 리뷰를 수집하고 가중치 기반으로 종합한 뒤 사용자 승인 게이트를 관리한다.

## Source of Truth

- `tools/review_followup/` is the editable source-of-truth in git.
- `{{RUNTIME_TOOLS_DIR}}` is the installed runtime copy used by Hermes.
- Sync changes with `python3 tools/review_followup/sync_runtime_copy.py`.
- Do not hand-edit the runtime copy; regenerate it from the repo source instead.

## Required Input

- Multica 구현 이슈
- GitHub PR URL
- Claude/Codex high-signal 리뷰 코멘트
- Gemini/Copilot supplementary 리뷰 코멘트
- 로컬 `gh auth login` 또는 `GH_TOKEN`

## Loop

1. 구현 PR을 만들고 원본 Multica 이슈에 `GitHub PR: <url>` 형식으로 남긴다.
2. `python3.11 {{REVIEW_FOLLOWUP_PATH}} <ISSUE> --review-pack --post-comment`로 리뷰 요청 pack을 남긴다.
3. Claude/Codex는 high-signal reviewer, Gemini/Copilot은 supplementary reviewer로 리뷰를 수집한다.
4. 리뷰 결과는 `must-fix`, `should-fix`, `question`, `non-actionable` 섹션으로 받는다.
5. `python3.11 {{REVIEW_FOLLOWUP_PATH}} <ISSUE> --post-comment`로 PR 상태, checks, reviewThreads, 리뷰 triage, approval gate를 수집한다.
6. `autoMergeRequest: null`은 GitHub PR에서 auto-merge가 켜져 있지 않다는 뜻일 뿐이며, 코드 리뷰 blocker나 Hermes 자동 병합 제외 사유로 보지 않는다.
7. Hermes 자동 병합 판단은 GitHub auto-merge 설정값이 아니라 repo/PR risk, CI/checks, high-signal review, unresolved threads, 사용자 정책을 기준으로 한다. 단순·저위험·범위 내 PR은 gate 통과 후 자동 병합 후보가 될 수 있고, 위험/정책/보안/비용/릴리스 판단은 사용자 승인을 요구한다.
8. 상태는 다음 순서로 본다.
   - `collecting_reviews`: Claude/Codex high-signal gate가 아직 비어 있음
   - `approval_needed`: 리뷰/CI/범위 신호가 모였고 사용자 승인 또는 보류 판단이 필요함
   - `needs_agent_fix`: 사용자가 `hermes approve fix` 또는 `hermes approve split`로 수정 진행을 승인함
   - `ready_for_approved_merge`: CI success + 승인 + high-signal blocker 없음 + unresolved must-fix 없음
   - `blocked`: PR 매핑 실패, 인증 실패, invalid PR URL 같은 운영 blocker
9. Hermes가 `Apply Plan`을 기준으로 반영 범위를 제안한다.
   - `apply-now`: 현재 PR 브랜치에 바로 반영한다.
   - `apply-if-low-risk`: 작고 명확하면 반영하고, 범위가 커지면 보류한다.
   - `create-follow-up-issue`: 큰 항목은 별도 Multica 이슈로 분리한다.
   - `needs-decision`: 사용자나 리뷰어 결정 없이는 구현하지 않는다.
   - `no-code-change`: 승인, FYI, 중복, outdated 항목으로 처리한다.
10. 사용자 리뷰가 필요한 조건은 항상 명시한다.
   - 제품/정책/보안/비용/릴리스 결정
   - 위험 변경
   - Claude/Codex 충돌
   - CI failure/pending
   - unresolved must-fix 또는 unresolved review threads
   - PR 범위 확대
11. `approval_needed`일 때는 `[hermes:approval-needed]`와 A/B/C 선택지, Hermes 추천안, 짧은 승인 명령을 남긴다.
12. targeted test/analyze, `gh pr checks`, unresolved reviewThreads를 다시 확인한다.
13. 최종 코멘트에는 `changed`, `deferred`, `rejected with reason`, `verified`, `remaining risk`만 남긴다.

## Commands

```bash
python3.11 {{REVIEW_FOLLOWUP_PATH}} --scan-status in_review --output json
python3.11 {{REVIEW_FOLLOWUP_PATH}} ITT-102 --review-pack --post-comment
python3.11 {{REVIEW_FOLLOWUP_PATH}} ITT-102 --pr-url https://github.com/owner/repo/pull/123 --post-comment
python3.11 {{REVIEW_FOLLOWUP_PATH}} --resolve-pr-url https://github.com/owner/repo/pull/123 --statuses in_review,in_progress,blocked,todo --reviewer-roster-file {{REVIEWER_ROSTER_PATH}} --create-triage-issue-on-miss --output json
```

## Webhook Trigger Loop

1. Enable the Hermes webhook platform.
   - Check the current state with `hermes webhook list`.
   - Minimal config change:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      host: "0.0.0.0"
      port: 8644
      secret: "<global-hmac-secret>"
```

2. Restart the gateway after the config change.

```bash
hermes gateway restart
curl http://localhost:8644/health
```

3. Create a dynamic subscription using the local prompt template.

```bash
PROMPT_FILE={{RUNTIME_TOOLS_DIR}}/review_followup_webhook_prompt.txt
PROMPT="$(cat "$PROMPT_FILE")"

hermes webhook subscribe github-pr-followup \
  --events "pull_request,pull_request_review,pull_request_review_comment,issue_comment" \
  --prompt "$PROMPT" \
  --description "Recheck linked Multica issue when GitHub PR review activity or merge arrives"
```

4. Register the returned URL and secret in GitHub repository settings.
   - Payload URL: `https://<public-host>/webhooks/github-pr-followup`
   - Content type: `application/json`
   - Secret: the secret returned by `hermes webhook subscribe`
   - Events:
     - `Pull requests`
     - `Pull request reviews`
     - `Pull request review comments`
     - `Issue comments`

## GitHub Payload Mapping

- `pull_request`
  - repo: `repository.full_name`
  - PR number: `pull_request.number`
  - action: `action`
  - merged flag: `pull_request.merged`
  - PR URL: `pull_request.html_url`
  - head/base: `pull_request.head.ref`, `pull_request.base.ref`
  - head SHA: `pull_request.head.sha`
  - merge commit: `pull_request.merge_commit_sha`
  - merged at/by: `pull_request.merged_at`, `pull_request.merged_by.login`
- `pull_request_review`
  - repo: `repository.full_name`
  - PR number: `pull_request.number`
  - action: `action`
  - author: `review.user.login`
  - review URL: `review.html_url`
  - PR URL: `pull_request.html_url`
  - body: `review.body`
  - review state: `review.state`
- `pull_request_review_comment`
  - repo: `repository.full_name`
  - PR number: `pull_request.number`
  - action: `action`
  - author: `comment.user.login`
  - comment URL: `comment.html_url`
  - PR URL: `pull_request.html_url`
  - body: `comment.body`
  - location: `comment.path`, `comment.line`
- `issue_comment`
  - Only continue when the payload is for a PR conversation (`issue.pull_request` exists)
  - repo: `repository.full_name`
  - PR number: `issue.number`
  - action: `action`
  - author: `comment.user.login`
  - comment URL: `comment.html_url`
  - PR URL: `issue.html_url`
  - body: `comment.body`

## GitHub Signal Normalization

- `claude` / `claude[bot]` GitHub review or PR issue comment는 `reviewer_key=claude-code`, `signal_source=github-review`로 정규화한다.
- GitHub payload body에 `<!-- ai-review-meta {... "verdict": "PASS" ...} -->`가 있으면 해당 verdict를 우선 사용한다.
- 그 외에는 `review.state`와 본문 패턴(`PASS`, `APPROVED`, `must-fix`, `changes requested` 등)으로 verdict를 추출한다.
- gate용 정규화는 `pass|approved -> ready`, `needs-fix|actionable|unclear|commented -> needs_review`, `blocked -> blocked`로 본다.
- supplementary reviewer(`gemini`, `copilot`)의 GitHub verdict도 수집하지만, high-signal gate를 직접 해제하지는 않고 triage/reference metadata로만 반영한다.

## Multica Issue Link Strategy

1. Primary match: exact GitHub PR URL in the Multica issue description or in a comment line labeled as `PR`, `Pull Request`, or `GitHub PR`.
2. Fallback match: PR body or comment body contains `ITT-123` or `mention://issue/<uuid>`.
3. If the same PR resolves to zero safe matches and the base repo owner is `ittae`, create one blocked `needs-triage` tracking issue that records PR URL, reviewer/state, missing-link reason, and next action instead of silently dropping the event.
4. If the same PR resolves to zero safe matches and the repo owner is not `ittae`, skip fallback tracking. External/third-party repos are out of scope unless they already link to a Multica issue.
5. Safety rule: if the same PR resolves to more than one non-placeholder issue, do not guess. Return `blocked` and list the candidates.
6. Preferred authoring convention:
   - Multica issue comment: `GitHub PR: https://github.com/<owner>/<repo>/pull/<number>`

## Merged PR Aftercare

1. Subscribe to the GitHub `pull_request` event. GitHub cannot route only `closed` sub-actions, so the prompt must explicitly ignore non-merged actions.
2. When `action == closed` and `pull_request.merged == true`, run `{{REVIEW_FOLLOWUP_PATH}} --merged-aftercare-pr-url <url> --apply-aftercare`.
3. Status transition policy:
   - Directly linked issue: auto-close only when it has the exact PR URL, or when it is a leaf issue directly referenced by PR title/body (`ITT-123`, `mention://issue/<uuid>`).
   - Child issues: auto-close only when they are descendants of a linked issue and explicitly carry the same PR URL.
   - Leave `blocked`, `backlog`, `cancelled`, and non-leaf safe-ref-only issues unchanged.
4. The script posts one `[hermes:pr-merged]` record on each top-level linked issue unless the same merge commit/head SHA was already recorded.
5. Merge aftercare must never merge, push, reopen reviews, or change branch protection. It only records merge metadata and normalizes Multica issue statuses.
   - PR body footer: `Related Multica: ITT-123`
   - Optional deep link: `mention://issue/<uuid>`

## Review Gate Strategy

1. Treat the GitHub webhook as the wake-up signal, not as the final decision.
2. Resolve the linked Multica issue first, then compute gate status from recognized reviewer sources.
3. Prefer `--reviewer-roster-file` so reviewer key, role, availability, legacy_names, and signal_source live in one JSON config. Legacy `--required-reviewers` / `--supplementary-reviewers` stay available as backward-compatible overrides.
4. Missing supplementary reviewers should not hard-block the state.
5. Only high-signal reviewers gate progress out of `collecting_reviews`.
6. `approval_needed` is the default state after high-signal collection unless a user approval signal is already present.
7. Only mark `ready_for_approved_merge` when all of these are true:
   - CI success
   - explicit merge approval exists
   - no high-signal conflict
   - no unresolved must-fix
   - no unresolved current review threads
   - no pending checks

## Multica Comment Format

```md
## PR Review Webhook Recheck
- event: pull_request_review / submitted by copilot
- PR: https://github.com/owner/repo/pull/123
- linked issue: ITT-123
- high-signal present: Claude, Codex
- high-signal missing: none
- supplementary present: Gemini
- supplementary missing: Copilot
- state: approval_needed
- next action: wait for `hermes approve fix` or `hermes approve merge`

[hermes:approval-needed]
- reason: CI pending 상태라 자동 병합 판단 전에 사용자가 대기/진행을 결정해야 함
- option A: `hermes approve fix` - 현재 PR 범위 안에서 리뷰 반영 작업을 진행
- option B: `hermes approve split` - 범위가 큰 항목은 child issue로 분리
- option C: `hermes hold` - 추가 판단 전까지 자동 조치를 보류
- recommendation: `hermes approve fix`
```

## Safe Child Issue Pattern

1. Do not auto-create child issues on every webhook event.
2. Only consider a child issue when `follow_up.state == needs_agent_fix` and the apply plan contains `create-follow-up-issue`.
3. Prefer a proposal-first comment, then create the child issue with an explicit parent link if the workflow allows automation.

```bash
multica issue create \
  --title "[follow-up] <short fix summary>" \
  --description-stdin \
  --parent <parent-issue-id> \
  --priority high \
  --assignee-id <worker-agent-id>
```

Suggested description fields:
- source PR URL
- original parent issue
- exact reviewer finding
- why it was split instead of patching the current PR

## Blocked States

- `blocked/missing-pr-url`: 구현 이슈에 실제 PR URL이 없다.
- `blocked/gh-auth`: `gh`가 GitHub에 인증되어 있지 않다.
- `blocked/invalid-pr-url`: GitHub PR URL 형식이 아니다.

## Write Policy

GitHub write action은 기본 비활성이다. GitHub 댓글 작성, review submit, thread resolve, merge, push, branch protection 변경은 별도 명시 플래그와 사용자 승인 없이 하지 않는다.


## Notification Policy

Goal: GitHub webhook events should wake Hermes up, but Hermes should only notify the user when the PR state meaningfully changes.

Notify the user / leave a visible Multica summary only when one of these conditions is true:

1. State becomes `approval_needed` with a new `[hermes:approval-needed]` block.
2. State becomes `needs_agent_fix` and Hermes created or proposed a worker follow-up issue.
3. State becomes `ready_for_approved_merge`.
4. State becomes `blocked` because webhook handling failed, PR mapping failed, auth failed, or the linked Multica issue cannot be resolved safely.
5. An unlinked PR created a new blocked `needs-triage` tracking issue. The issue itself is the visible record, so a second summary comment is unnecessary unless triage creation failed.

Stay quiet / internal-only when:

- The event is just a single new comment and high-signal reviewer gate is still incomplete.
- The new feedback is non-blocking suggestion/FYI only and no worker action is needed.
- The event is a duplicate delivery.
- The computed state is unchanged from the last recorded `[hermes:pr-review-gate]` state and there is no new actionable item.

## Debounce and Idempotency

- Use GitHub `X-GitHub-Delivery` as the first idempotency key when available.
- Also compute a semantic key from `repo`, `pr_number`, `event`, `action`, `comment/review id`, and current `headRefOid`.
- For unlinked PRs, store the first meaningful event in a single blocked `needs-triage` tracking issue and reuse that issue on later deliveries.
- Store the last processed state in the linked Multica issue comment stream using `[hermes:pr-review-gate]` metadata.
- Before posting a user-visible update, compare new state/actionable counts with the last metadata block.
- If multiple comments arrive close together, prefer one consolidated notification after the reviewer gate is satisfied rather than one notification per webhook event.
