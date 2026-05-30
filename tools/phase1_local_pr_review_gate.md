# Local Hermes PR Review Gate — Phase 1 아키텍처 / 운영 가이드

이 문서는 [ITT-294](https://github.com/ittae/ittae) (Phase 1: 로컬 Hermes PR Review Gate live wiring 준비)의 산출물입니다. [ITT-276](https://github.com/ittae/ittae) "AI 리뷰 GitHub Action을 로컬 Hermes PR Review Gate로 대체"의 Phase 1 단계로, Phase 0 dispatcher([`tools/local_pr_review_dispatcher.py`](./local_pr_review_dispatcher.py))를 기반으로 live wiring을 **안전하게(non-mutating / disabled-by-default)** 준비합니다.

핵심 원칙: **이 단계에서는 실제 GitHub PR comment/label/check status를 게시하지 않습니다.** 모든 mutation은 adapter 뒤에서 기본 비활성이며, 기존 AI 리뷰 GitHub Action 제거·branch protection·required checks·repo settings 변경은 Phase 2에서 dual-run 검증 후 별도 승인으로 진행합니다.

## 컴포넌트와 책임 경계

```
GitHub PR event (opened / synchronize / ready_for_review)
        │  webhook
        ▼
[1] Webhook ingress (Hermes runtime)
        │  PR URL + event type
        ▼
[2] Dispatcher  (local_pr_review_dispatcher.py)  ── 순수 분류, mutation 없음
        │  classify(size/high-risk) + agent dispatch payload (dry-run JSON)
        ├──────────────► [3] Agent dispatch (Claude / Codex / Gemini / Copilot)
        │                        │  각 agent가 리뷰 → ai-review-meta 신호 게시
        ▼                        ▼
[4] Gate actions adapter            [5] review_followup gate (review_followup.py)
   (pr_review_gate_actions.py)         │  reviewer roster 기준 state machine
   라벨/코멘트/check status            │  collecting_reviews / approval_needed /
   = DryRunExecutor (기본)            │  needs_agent_fix / ready_for_approved_merge / blocked
   = GhCliExecutor (승인 후에만)       ▼
                                  최종 verdict + 사람 승인 게이트
```

| 컴포넌트 | 책임 | mutation |
|---|---|---|
| **[1] Webhook ingress** | PR event 수신, PR URL/event type 정규화, dispatcher 호출 | 없음 |
| **[2] Dispatcher** | size limit + high-risk path 분류, agent dispatch payload 생성 | **없음 (dry-run only, Phase 0에서 고정)** |
| **[3] Agent dispatch** | reviewer roster의 active agent에게 PR 리뷰 작업 분배, 각 agent가 `ai-review-meta` 신호 게시 | agent별 리뷰 코멘트 (별도 경계) |
| **[4] Gate actions adapter** | classify 결과 → 라벨/코멘트/check status action plan 변환·실행 | **기본 DryRunExecutor (mutation 없음)** |
| **[5] review_followup gate** | reviewer 신호 종합, state machine으로 최종 verdict 산출, 사람 승인 게이트 | gate 코멘트 / Check (`hermes/pr-review-gate`) |

**경계 규칙**:
- Dispatcher는 절대 mutation하지 않는다. 분류와 payload 생성만 한다.
- 모든 GitHub mutation은 [4] adapter의 executor를 통해서만 일어난다. 직접 `gh pr edit` / `gh pr comment` 호출을 다른 컴포넌트에 흩뿌리지 않는다.
- live mutation은 두 단계 안전장치를 모두 통과해야 한다: (a) `GhCliExecutor`를 선택하고, (b) `enable_mutations=True`를 명시한다. 둘 중 하나라도 빠지면 mutation은 일어나지 않는다.
- agent 리뷰 신호 종합과 사람 승인 판단은 [5] review_followup이 단독 소유한다. adapter는 "무엇을 게시할지"만 알고 "게시해도 되는지"는 모른다.

## Gate actions adapter ([`tools/pr_review_gate_actions.py`](./pr_review_gate_actions.py))

Phase 0 dispatcher가 내보내는 `classify.would_apply_labels` / `classify.would_post_comments`를 실행 가능한 action plan으로 바꾸고, executor 뒤에서 실행한다.

```python
import json, subprocess
import local_pr_review_dispatcher as dispatcher
import pr_review_gate_actions as gate

# 1) Phase 0 dispatcher로 분류 (mutation 없음)
result = dispatcher.run_dispatch(
    pr_url="https://github.com/ittae/ittae/pull/425",
    pr_size_limit=dispatcher.DEFAULT_PR_SIZE_LIMIT,
    high_risk_paths_regex=dispatcher.DEFAULT_HIGH_RISK_PATHS,
    model=dispatcher.DEFAULT_MODEL,
    roster_path="...",
    fixture_path=None,  # live는 None → gh CLI
)

# 2) classify → action plan (순수 변환)
target = gate.PRTarget(repo=result["pr"]["repo"], number=result["pr"]["number"])
plan = gate.plan_actions_from_classify(result["classify"], target)

# 3a) 기본: dry-run. 실행될 명령만 기록, mutation 없음.
results = gate.apply_actions(plan)  # DryRunExecutor
print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))

# 3b) live (Phase 2, 승인 후에만):
# executor = gate.GhCliExecutor(enable_mutations=True)
# results = gate.apply_actions(plan, executor)
```

- `DryRunExecutor` (기본): `build_gh_command()`로 만든 **실제 실행될 argv**를 그대로 기록한다. 실행은 하지 않는다. dual-run 비교의 기준이 된다.
- `GhCliExecutor(enable_mutations=False)` (기본 생성): mutating 호출 시 `GateActionError`를 던지며, 던지기 전에도 runner를 호출하지 않는다.
- `GhCliExecutor(enable_mutations=True)`: 명시적 opt-in. Phase 2 dual-run 검증 + 사람 승인 후에만 사용한다.

action 종류: `ADD_LABELS`, `REMOVE_LABELS`, `POST_COMMENT`, `SET_CHECK_STATUS`. check status는 review_followup의 `hermes/pr-review-gate` Check 게시를 위한 forward-compatible 슬롯이며, 현재 기존 `claude-code-review.yml`은 Check가 아니라 label+comment로 게이트하므로 기본 경로에서는 쓰이지 않는다.

## 1주 dual-run 운영 방법

목표: 기존 AI 리뷰 GitHub Action을 **켜둔 채로**, 로컬 게이트를 **dry-run**으로 같은 PR에 병행 실행해 결과가 일치하는지 1주간 비교한다. 이 기간 동안 어떤 것도 제거하거나 비활성화하지 않는다.

1. **기존 Action 유지**: `.github/workflows/claude-code-review.yml` 및 관련 워크플로는 그대로 두고 평소처럼 라벨/코멘트를 단다 (= ground truth).
2. **로컬 게이트 dry-run 병행**: 새 PR event마다 로컬 Hermes 런타임에서
   ```bash
   python3 tools/local_pr_review_dispatcher.py --pr-url <URL> --dry-run > /tmp/gate.json
   # classify 블록을 adapter로 dry-run 적용 (mutation 없음)
   ```
   를 돌려 `classify.verdict` / `would_apply_labels` / `would_post_comments`를 기록한다.
3. **비교 로그**: PR별로 (a) 기존 Action이 실제로 단 라벨/코멘트와 (b) 로컬 게이트 dry-run이 산출한 라벨/코멘트를 나란히 남긴다. 불일치는 모두 분류한다.
   - verdict 불일치 (proceed vs too-large vs high-risk)
   - 라벨 집합 차이
   - 코멘트 본문 차이 (템플릿은 verbatim 포팅이므로 일치해야 정상)
4. **합격 기준 (Phase 2 진입 전제)**: 1주(또는 최소 N건의 PR) 동안 verdict·라벨 불일치 0건, 코멘트 본문 차이는 의도된 것만. 미해결 불일치가 있으면 Phase 2(Action 제거)로 넘어가지 않는다.
5. **승격**: 합격 후 별도 PR/승인으로 `GhCliExecutor(enable_mutations=True)`를 켜고, 그 다음 별도 단계에서 기존 Action을 제거/비활성화한다 (한 번에 둘 다 하지 않는다).

## Rollback 방법

이 Phase 1 PR 자체는 mutation을 하지 않으므로 rollback 위험이 낮다. 단계별 rollback:

| 상태 | rollback 동작 | 영향 |
|---|---|---|
| Phase 1 (현재): adapter 추가, dry-run only | PR revert 또는 adapter 미사용 | 없음 — 기존 Action이 계속 단독 동작 |
| Phase 2-a: `enable_mutations=True` 활성화 후 문제 발생 | `enable_mutations=False`로 되돌림 (코드 한 줄 / 설정 플래그) | 즉시 mutation 중단, 기존 Action은 여전히 켜져 있어 공백 없음 |
| Phase 2-b: 기존 Action 제거 후 문제 발생 | 제거 PR revert로 `claude-code-review.yml` 복구 | 워크플로 복구 후 즉시 기존 게이트 재가동 |

**핵심 안전장치**: dual-run 기간 동안 기존 Action을 끄지 않으므로, Phase 2-a에서 로컬 게이트를 꺼도 PR 리뷰 게이트에 공백이 생기지 않는다. 기존 Action 제거(Phase 2-b)는 로컬 게이트 live 동작이 충분히 검증된 뒤 마지막에만 한다.

## 검증

```bash
python3 tools/tests/test_pr_review_gate_actions.py        # adapter (21개)
python3 tools/tests/test_local_pr_review_dispatcher.py    # 기존 dispatcher 회귀 (27개)
python3 -m py_compile tools/*.py
```

## Phase 1 범위 밖 (별도 승인 필요)

- `GhCliExecutor(enable_mutations=True)` live 활성화.
- 기존 `claude-code-review.yml` / `claude-review-light.yml` 제거·비활성화.
- review_followup gate ↔ adapter 실제 연동 및 `hermes/pr-review-gate` Check 게시.
- webhook subscription 등록, `sync_runtime_copy.py` 패턴으로 `~/.hermes/workspace/tools/` 자동 동기화.
- branch protection / required checks / repo settings 변경.
