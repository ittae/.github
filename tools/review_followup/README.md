# Review Follow-up Tooling

`tools/review_followup/` is the public source-of-truth for the Hermes PR review gate.

## Boundary

- Edit and review changes in this directory only.
- `~/.hermes/workspace/tools/` is a generated runtime copy, not the origin.
- Do not commit secrets, private issue/comment dumps, personal workspace paths, or operational logs here.

## Contents

- `review_followup.py`: Multica/GitHub review gate CLI.
- `reviewer_roster.example.json`: public example roster for runtime bootstrap.
- `reviewer_roster.schema.json`: JSON schema for roster validation.
- `review_followup_runbook.md`: operator runbook template.
- `review_followup_webhook_prompt.txt`: webhook prompt template.
- `sync_runtime_copy.py`: installs or refreshes the runtime copy.
- `tests/`: repo-local regression tests.

## Sync Runtime Copy

```bash
python3 tools/review_followup/sync_runtime_copy.py
```

Useful flags:

- `--runtime-dir /path/to/tools`
- `--fallback-project-title ittae`
- `--overwrite-roster`
- `--dry-run`
- `--output json`

The sync step renders template placeholders into runtime paths and initializes `reviewer_roster.json` only when it does not already exist, unless `--overwrite-roster` is passed.

## Test

```bash
python3 -m unittest \
  tools/review_followup/tests/test_review_followup.py \
  tools/review_followup/tests/test_review_followup_runtime.py
```
