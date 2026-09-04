"""폐루프 배포 전 CI의 이미지 선택·캐시·실행 연결 계약을 검증한다.

캐시 scope 충돌, 런타임 이미지 미적재, 커밋 메타데이터로 인한 설치 캐시
무효화 회귀를 잡는다. 실제 Actions 캐시 전송과 배포는 이 모듈의 책임이 아니다.
"""

from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("changed_path", "expected"),
    [
        ("docs/runbooks/example.md", set()),
        ("deployment/Dockerfile.train", {"train"}),
        ("tools/__init__.py", {"experiment_platform"}),
        ("tools/auto_research_issue_branch.py", {"experiment_platform"}),
        (".streamlit/config.toml", {"experiment_platform"}),
        ("applications/__init__.py", {"serving", "experiment_platform"}),
        ("scripts/gcs_code_bootstrap.sh", {"app", "train", "feast"}),
        (
            ".github/workflows/ci.yml",
            {"app", "train", "feast", "serving", "mlflow", "experiment_platform"},
        ),
    ],
)
def test_shared_image_inputs_are_selected(
    changed_path: str, expected: set[str]
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text("utf-8"))
    filter_step = next(
        step
        for step in workflow["jobs"]["changes"]["steps"]
        if step.get("id") == "filter"
    )
    filters = yaml.safe_load(filter_step["with"]["filters"])
    selected = {
        image
        for image, patterns in filters.items()
        if any(fnmatchcase(changed_path, pattern) for pattern in patterns)
    }
    assert selected == expected


def test_push_filter_preserves_initial_and_multi_commit_push_boundaries() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text("utf-8"))
    changes = workflow["jobs"]["changes"]
    filter_step = next(step for step in changes["steps"] if step.get("id") == "filter")
    assert filter_step["if"] == "github.event_name != 'workflow_dispatch'"
    for image, output in changes["outputs"].items():
        assert output == (
            "${{ github.event_name == 'workflow_dispatch' || "
            f"steps.filter.outputs.{image} }}}}"
        )
    # dorny v3의 NULL_SHA 분기는 base와 ref가 같을 때만 실행된다.
    for key, tip in (("base", "github.event.before"), ("ref", "github.sha")):
        assert filter_step["with"][key] == (
            "${{ github.event_name == 'push' && "
            "(github.event.before == '0000000000000000000000000000000000000000' "
            f"&& github.ref || {tip}) || '' }}}}"
        )
    concurrency = workflow["concurrency"]
    assert (
        concurrency["cancel-in-progress"]
        == "${{ github.event_name == 'pull_request' }}"
    )
    assert "format('pr-{0}', github.event.pull_request.number)" in concurrency["group"]
    assert "format('run-{0}', github.run_id)" in concurrency["group"]


def test_cached_builds_load_distinct_images_for_existing_smoke_checks() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text("utf-8"))
    scopes: set[str] = set()
    tags: set[str] = set()
    for job in workflow["jobs"].values():
        steps = job.get("steps", [])
        for index, step in enumerate(steps):
            if not step.get("uses", "").startswith("docker/build-push-action@"):
                continue
            inputs = step["with"]
            assert inputs["context"] == "."
            assert inputs["load"] is True
            assert inputs.get("push", False) is False
            assert any(
                previous.get("uses", "").startswith("docker/setup-buildx-action@")
                for previous in steps[:index]
            )
            cache_from = dict(
                item.split("=", 1) for item in inputs["cache-from"].split(",")
            )
            cache_to = dict(
                item.split("=", 1) for item in inputs["cache-to"].split(",")
            )
            assert cache_from["type"] == cache_to["type"] == "gha"
            assert cache_from["scope"] == cache_to["scope"]
            assert cache_to["mode"] == "max"
            assert cache_to["scope"] not in scopes
            assert "${{" not in cache_to["scope"], (
                "커밋별 scope는 실행 간 재사용을 막는다"
            )
            scopes.add(cache_to["scope"])
            assert inputs["tags"] not in tags
            tags.add(inputs["tags"])
            smoke = "\n".join(
                following.get("run", "") for following in steps[index + 1 :]
            )
            assert inputs["tags"] in smoke, (
                "캐시 적중 후에도 적재한 이미지를 실행해야 한다"
            )
    assert len(tags) == 9


@pytest.mark.parametrize("image", ["app", "train", "feast"])
def test_commit_metadata_follows_all_expensive_image_instructions(image: str) -> None:
    dockerfile = (ROOT / f"deployment/Dockerfile.{image}").read_text("utf-8")
    instructions = [
        line for line in dockerfile.splitlines() if line and not line.startswith("#")
    ]
    metadata = instructions.index("ARG VCS_REF=unknown")
    assert all(
        index < metadata
        for index, line in enumerate(instructions)
        if line.startswith(("RUN ", "COPY "))
    ), "VCS_REF 변경이 설치·복사 레이어를 무효화해서는 안 된다"
    assert "AUTORESEARCH_REVISION=${VCS_REF}" in dockerfile
    assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/gcs_code_bootstrap.sh"]' in dockerfile
