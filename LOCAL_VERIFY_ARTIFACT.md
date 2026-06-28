# local_verify artifact v1

`local_verify`는 로컬에서 전체 검증을 끝낸 결과를 PR에 정적 JSON으로 남기고,
CI가 그 JSON이 현재 source와 맞는지 빠르게 확인하기 위한 attestation 포맷이다.

이 포맷은 full CI를 대체하지 않는다. 기본 `flutter-ci.yml`에서는 artifact가 없으면
기존 `build_runner` / `flutter analyze` / `flutter test --coverage` / coverage gate를
그대로 실행한다. artifact가 있으면 무결성 검증에 실패할 때만 CI를 fail한다.

## 파일 위치

기본 경로는 caller repository의 `.github/local_verify.json`이다.

## Source Hash

`source.hash`는 `git-tracked-sha256-v1` 알고리즘으로 계산한다.

계산 규칙:
- `git ls-files`에 포함된 tracked file만 사용한다.
- 기본적으로 `.github/local_verify.json` 자체는 제외한다.
- caller가 `local_verify_source_exclude` input을 넘기면 쉼표 또는 줄바꿈으로 추가 제외할 수 있다.
- 각 file은 path bytes, NUL byte, file bytes, NUL byte 순서로 sha256에 누적한다.
- file 순서는 path 기준 오름차순이다.

## JSON Schema

필수 필드:

```json
{
  "schema_version": 1,
  "repository": "ittae/flutter_boilerplate",
  "source": {
    "algorithm": "git-tracked-sha256-v1",
    "hash": "64-character-sha256-hex",
    "base_ref": "main",
    "excluded_paths": [".github/local_verify.json"]
  },
  "verification": {
    "generated_at": "2026-06-28T00:00:00Z",
    "tool_versions": {
      "flutter": "Flutter 3.41.9",
      "dart": "Dart 3.x"
    },
    "commands": [
      {
        "name": "pub get",
        "command": "flutter pub get",
        "exit_code": 0
      },
      {
        "name": "codegen",
        "command": "dart run build_runner build --delete-conflicting-outputs",
        "exit_code": 0
      },
      {
        "name": "analyze",
        "command": "flutter analyze --fatal-infos --fatal-warnings",
        "exit_code": 0
      },
      {
        "name": "test",
        "command": "flutter test --coverage test/",
        "exit_code": 0
      }
    ],
    "test_manifest": {
      "files": ["test/example_test.dart"],
      "test_count": 205,
      "coverage_lcov": "coverage/lcov.info"
    },
    "result_summary": {
      "status": "passed",
      "coverage": 80,
      "notes": "Full local verify passed before PR."
    }
  }
}
```

CI가 강제하는 최소 조건:
- `schema_version`은 `1`이어야 한다.
- `repository`는 현재 `github.repository`와 일치해야 한다.
- `source.algorithm`은 `git-tracked-sha256-v1`이어야 한다.
- 재계산한 source hash가 `source.hash`와 일치해야 한다.
- `verification.commands`는 비어 있으면 안 되고 모든 `exit_code`가 `0`이어야 한다.
- `verification.tool_versions`는 비어 있으면 안 된다.
- `verification.test_manifest.test_count`는 0 이상의 정수여야 한다.
- `verification.result_summary.status`는 `passed`여야 한다.

## Local Generation Checklist

artifact를 커밋하기 전 최소 검증:

```bash
flutter pub get
dart run build_runner build --delete-conflicting-outputs
flutter analyze --fatal-infos --fatal-warnings
flutter test --coverage test/
```

release/tag 직전에는 이 artifact만으로 충분하지 않다. native build, e2e/smoke,
Fastlane dry-run 또는 store-upload 직전 검증은 별도 release gate에서 다시 수행해야 한다.
