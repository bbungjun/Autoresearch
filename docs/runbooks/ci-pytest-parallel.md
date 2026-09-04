# CI pytest 병렬 실행 문제 해결 기록

> 이슈: #85  
> 작성일: 2026-09-04  
> 상태: PR 1차 실측 완료, main 최종 실측 대기

## 문제

일반 pytest job은 Python 3.11과 3.12에서 전체 테스트를 각각 직렬 실행합니다.
최근 성공한 `main` CI 10회의 job 시간은 Python 3.11 중앙값 477초
(415~502초), Python 3.12 중앙값 493.5초(363~581초)였습니다. 최신 CI의 pytest
본체는 4,052 passed, 126 skipped를 기록하고 449.74초 걸렸습니다. 두 Python
버전의 회귀 검증은 필요하지만 runner에서 테스트 프로세스 하나만 사용하는 실행
방식이 대기시간의 큰 부분을 차지했습니다.

## 대안과 선택

- 개별 테스트의 `sleep`을 찾아 제거하는 방법은 실제 대기 원인을 줄일 수 있지만,
  전체 suite의 병목을 한 번에 해소하지 못하고 테스트 의미를 바꿀 위험이 있습니다.
  `--durations=25` 결과를 남겨 이후 실제 병목만 별도로 개선하기로 했습니다.
- 테스트 디렉터리를 여러 CI job으로 수동 분할하면 job별 격리가 명확하지만, 파일
  수와 실행시간이 바뀔 때 shard 균형을 계속 관리해야 하고 환경 설치도 반복합니다.
- `pytest-xdist`는 한 job 안에서 여러 worker를 사용하므로 현재 Python 버전 matrix와
  설치 단계를 유지합니다. `--dist loadfile`을 선택해 같은 파일의 테스트를 같은
  worker에서 실행하고, 파일 내부 순서와 module 범위 fixture의 결합을 보존합니다.

깨끗한 WSL Python 3.12 환경에서 직렬 전체 suite는 환경성 실패 2건을 제외하고
4,050건이 통과했으며 pytest 353.87초, 벽시계 370.39초였습니다. 같은 환경의
`-n 2`는 pytest 175.64초, 벽시계 182.78초였고 `-n 4`는 pytest 97.64초,
벽시계 101.31초였습니다. `-n 4`는 직렬 대비 pytest 72.4%, 벽시계 72.7%,
`-n 2` 대비 pytest 44.4% 감소했습니다.

세 실행은 모두 4,050 passed, 126 skipped와 동일한 실패 2건을 냈습니다. 실패는
WSL의 `PATH`에 빈 항목이 있어 존재하지 않는 명령 실행이 `PermissionError`로 바뀐
로컬 환경 문제였으며 병렬 worker 수에 따라 새로 생기지 않았습니다. 이 결과는
worker 수를 고르는 로컬 비교 근거이며 GitHub-hosted runner 성과를 뜻하지 않습니다.

## 구현

dev dependency에 `pytest-xdist>=3.8,<4`를 추가하고 `uv.lock`으로 고정합니다.
일반 Python 3.11/3.12 job과 CI 동일 로컬 명령은 다음을 사용합니다.

```bash
uv run python -m pytest -n 4 --dist loadfile --durations=25
```

[GitHub 공식 사양](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)에
따르면 Public 저장소의 현재 표준 `ubuntu-latest` runner는 4 vCPU/16GB입니다.
worker 수 4는 이 CPU 수와 로컬 `-n 2`/`-n 4` 비교를 함께 근거로 정했습니다.
`auto`처럼 runner 사양 이상으로 프로세스를 만들지 않아 worker별 메모리와 공유
자원 충돌 위험을 제한합니다. Feast 전용 환경과 PostgreSQL 계약 job은 일반 dev
suite와 다른 의존성·서비스를 사용하므로 이번 변경에서 병렬화하지 않습니다.

## 검증과 결과

구현 시 다음을 확인합니다.

- `uv lock --check`: `pyproject.toml`과 lockfile 일치
- 소규모 테스트를 `-n 4 --dist loadfile`로 실행: xdist 설치와 worker 실행 확인
- `git diff --check`: YAML·문서·lockfile 변경의 공백 오류 확인

PR #86의 첫 GitHub-hosted 실행은
[Actions run 33790459899](https://github.com/bbungjun/Autoresearch/actions/runs/33790459899)에서
성공했습니다. 변경 전 최근 성공 `main` 10회 중앙값과 비교하면 Python 3.11 job은
477초에서 214초로 55.1%, Python 3.12 job은 493.5초에서 228초로 53.8%
줄었습니다. 한 번의 PR 실행 결과이므로 `main` 최종 실측과 반복 실행 분포는 아직
확인하지 않았습니다.

| 항목 | 변경 전 근거 | 변경 후 원격 결과 |
| --- | --- | --- |
| Python 3.11 pytest job | 최근 성공 10회 중앙값 477초(415~502초) | 214초(3분 34초), 55.1% 감소 |
| Python 3.12 pytest job | 최근 성공 10회 중앙값 493.5초(363~581초) | 228초(3분 48초), 53.8% 감소 |
| pytest 본체 | 최신 CI 449.74초 | 3.11 195.07초, 3.12 213.82초 |
| 테스트 결과 | 최신 CI 4,052 passed, 126 skipped | 양쪽 모두 4,052 passed, 126 skipped; 3.11 131 warnings, 3.12 190 warnings |
| 느린 테스트 관측 | 별도 상위 25건 출력 없음 | proxy Docker forward 32.33초/25.00초, port-env 11.79초/11.48초, executor report timeout 약 10초 |

## 한계와 후속 과제

병렬 worker는 파일 간 공유하는 외부 자원, 고정 포트, 프로세스 전역 상태가 있으면
직렬 실행에서 보이지 않던 충돌을 드러낼 수 있습니다. `loadfile`은 한 파일 안의
결합을 줄이지만 서로 다른 파일 사이의 결합까지 막지는 않습니다. PR의 Python
3.11/3.12 결과가 기존 pass/skip 기준과 일치하는지 확인하고, 실패가 생기면 먼저
테스트 격리 결함인지 환경성 실패인지 구분합니다.

원격 실행에서는 테스트 시간뿐 아니라 의존성 설치 시간과 worker 시작 비용도 함께
관측해야 합니다. `--durations=25`에서 반복적으로 긴 테스트가 확인되면 개별 대기
제거를 별도 이슈로 다루고, 네 worker로도 충분하지 않은 경우에만 job sharding의
설치 비용과 유지보수 비용을 다시 비교합니다.

첫 원격 job 감소율 55.1%/53.8%는 WSL의 직렬 대비 `-n 4` 로컬 감소율
72.4%/72.7%보다 작았습니다. 원격 durations에서는 proxy Docker forward 테스트가
Python 3.11/3.12에서 각각 32.33초/25.00초, port-env 테스트가 11.79초/11.48초,
executor report timeout 계열이 약 10초를 차지했습니다. 병렬 worker가 같은 runner의
CPU와 Docker daemon을 함께 사용하지만, 이번 측정은 CPU·Docker 경합을 분리하지
않았습니다. 확인된 사실은 proxy Docker와 timeout 계열 테스트의 시간이 길었다는
것뿐입니다. 경합은 원격 개선 폭을 제한했을 수 있는 후보로 남기고 자원 사용량을
별도로 측정한 뒤 판단합니다. 한 번의 PR 결과를 안정적인 절감률로 일반화하지 않고,
`main` 실행과 이후 분포에서도 재확인합니다.
