"""실험 산출물 게시의 경로 규칙과 write-once 계약을 고정한다.

실제 GCS에 붙지 않고 client 경계만 대역으로 바꾼다. 버킷 생성과 IAM은
`Autoresearch-infra`의 범위이며, 여기서 지키는 것은 "어디에 무엇을 어떤 조건으로
올리는가"다.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from applications.experiment_platform.executor.results_store import (  # noqa: E402
    ResultsStoreError,
    build_experiment_prefix,
    collect_publishable_files,
    publish_results,
)


ROOT = "gs://autoresearch-505505-autoresearch-dev-experiment-results"


class _AlreadyExists(Exception):
    """`if_generation_match=0` 위반을 흉내 내는 412 예외."""

    code = 412


class _FakeBlob:
    def __init__(self, name: str, recorder: "_FakeClient") -> None:
        self._name = name
        self._recorder = recorder

    def upload_from_filename(self, filename: str, *, if_generation_match: int) -> None:
        self._recorder.uploads.append((self._name, if_generation_match))
        if self._name in self._recorder.existing:
            raise _AlreadyExists(self._name)


class _FakeBucket:
    def __init__(self, name: str, recorder: "_FakeClient") -> None:
        self.name = name
        self._recorder = recorder

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(name, self._recorder)


class _FakeClient:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.uploads: list[tuple[str, int]] = []
        self.buckets: list[str] = []

    def bucket(self, name: str) -> _FakeBucket:
        self.buckets.append(name)
        return _FakeBucket(name, self)


@pytest.fixture
def metrics_file(tmp_path: Path) -> Path:
    path = tmp_path / "metrics.json"
    path.write_text('{"contract_version": "experiment-metrics-v1"}', encoding="utf-8")
    return path


def test_prefix_separates_experiments_within_one_issue() -> None:
    """한 이슈가 재실행으로 여러 실험을 가질 수 있으므로 실험 ID까지 경로에 넣는다.

    이슈 번호까지만 쓰면 재실행 결과가 앞선 실험을 덮어, 무엇을 보고 있는지 알 수
    없게 된다.
    """
    first = build_experiment_prefix(issue_number=619, experiment_id="0134e82c")
    second = build_experiment_prefix(issue_number=619, experiment_id="7900e79f")

    assert first == "experiments/619/0134e82c"
    assert first != second


@pytest.mark.parametrize(
    ("issue_number", "experiment_id"),
    [(0, "abc"), (-1, "abc"), (619, ""), (619, "../escape"), (619, "a/b")],
)
def test_prefix_rejects_coordinates_that_could_escape_the_experiment_path(
    issue_number: int, experiment_id: str
) -> None:
    """경로를 벗어날 수 있는 좌표는 접두부를 만들지 않는다."""
    with pytest.raises(ResultsStoreError):
        build_experiment_prefix(issue_number=issue_number, experiment_id=experiment_id)


def test_publish_uses_write_once_precondition(metrics_file: Path) -> None:
    """모든 업로드가 `if_generation_match=0`으로 나간다.

    GSA 권한이 `objectCreator`라 교체는 IAM이 이미 막지만, 계약을 코드에도 남겨야
    권한이 넓어졌을 때 조용히 덮어쓰기로 바뀌지 않는다.
    """
    client = _FakeClient()

    published = publish_results(
        ROOT,
        {"metrics.json": metrics_file},
        issue_number=619,
        experiment_id="0134e82c",
        client=client,
    )

    assert client.uploads == [("experiments/619/0134e82c/metrics.json", 0)]
    assert published["metrics.json"].created is True
    assert published["metrics.json"].uri == (
        f"{ROOT}/experiments/619/0134e82c/metrics.json"
    )


def test_publish_applies_root_prefix(metrics_file: Path) -> None:
    """루트에 prefix가 있으면 실험 경로 앞에 붙인다."""
    client = _FakeClient()

    publish_results(
        f"{ROOT}/runs",
        {"metrics.json": metrics_file},
        issue_number=619,
        experiment_id="abc",
        client=client,
    )

    assert client.uploads[0][0] == "runs/experiments/619/abc/metrics.json"


def test_publish_marks_existing_objects_instead_of_failing(metrics_file: Path) -> None:
    """이미 있는 이름은 실패가 아니라 `created=False`로 구분해 돌려준다.

    Job 재시도가 같은 실험을 다시 돌릴 수 있는데 거기서 게시가 막히면 두 번째 실행은
    결과를 하나도 남기지 못한다. 그렇다고 조용히 넘기면 재실행 결과가 첫 실행과
    다를 때 그 사실이 사라진다 — 호출부가 로그로 남길 수 있게 구분한다.
    """
    client = _FakeClient(existing={"experiments/619/abc/metrics.json"})

    published = publish_results(
        ROOT,
        {"metrics.json": metrics_file},
        issue_number=619,
        experiment_id="abc",
        client=client,
    )

    assert published["metrics.json"].created is False


def test_publish_rejects_object_names_that_escape_the_prefix(
    metrics_file: Path,
) -> None:
    """상대 경로 탈출을 막는다 — 다른 실험의 결과를 침범할 수 없어야 한다."""
    client = _FakeClient()

    with pytest.raises(ResultsStoreError, match="invalid_object_name"):
        publish_results(
            ROOT,
            {"../other/metrics.json": metrics_file},
            issue_number=619,
            experiment_id="abc",
            client=client,
        )


def test_publish_checks_every_source_before_uploading_any(
    tmp_path: Path, metrics_file: Path
) -> None:
    """하나라도 없으면 아무것도 올리지 않는다.

    중간에 멈추면 실험 경로에 반쪽짜리 결과가 남고, 그 경로는 write-once라 **고쳐
    올릴 수 없다.**
    """
    client = _FakeClient()

    with pytest.raises(ResultsStoreError, match="publish_source_missing"):
        publish_results(
            ROOT,
            {"metrics.json": metrics_file, "report.md": tmp_path / "absent.md"},
            issue_number=619,
            experiment_id="abc",
            client=client,
        )

    assert client.uploads == []


@pytest.mark.parametrize(
    "root", ["", "s3://bucket", "bucket/prefix", "gs://", "gs:///prefix"]
)
def test_publish_rejects_invalid_root(root: str, metrics_file: Path) -> None:
    """루트 형식 오류는 업로드를 시도하기 전에 끊는다."""
    with pytest.raises(ResultsStoreError, match="invalid_results_root"):
        publish_results(
            root,
            {"metrics.json": metrics_file},
            issue_number=619,
            experiment_id="abc",
            client=_FakeClient(),
        )


def test_collect_includes_training_output_tree(
    tmp_path: Path, metrics_file: Path
) -> None:
    """학습 산출물은 조건·seed 구조를 유지한 채 함께 싣는다."""
    output_root = tmp_path / "training-output"
    (output_root / "baseline").mkdir(parents=True)
    (output_root / "baseline" / "model_42.txt").write_text("m", encoding="utf-8")
    (output_root / "candidate").mkdir(parents=True)
    (output_root / "candidate" / "model_42.txt").write_text("m", encoding="utf-8")

    files = collect_publishable_files(
        metrics_path=metrics_file, training_output_root=output_root
    )

    assert files["training-output/baseline/model_42.txt"].name == "model_42.txt"
    assert "training-output/candidate/model_42.txt" in files
    assert "metrics.json" in files


def test_collect_survives_without_training_output(metrics_file: Path) -> None:
    """학습을 켜지 않은 배포에서도 지표 게시 경로는 끊기지 않는다."""
    files = collect_publishable_files(metrics_path=metrics_file)

    assert list(files) == ["metrics.json"]


def test_collect_requires_metrics(tmp_path: Path) -> None:
    """지표가 없으면 게시하지 않는다 — 판정 입력이 빠진 게시는 성공이 아니다."""
    with pytest.raises(ResultsStoreError, match="metrics_missing"):
        collect_publishable_files(metrics_path=tmp_path / "absent.json")
