from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "pr-report-archive.yml"
)
pytestmark = pytest.mark.skipif(
    not WORKFLOW.is_file(),
    reason="조직 자원 부재로 비활성화된 pr-report-archive.yml 워크플로우 계약 테스트",
)


def _load_workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_archive_workflow_has_all_rebuild_triggers():
    workflow = _load_workflow()
    triggers = workflow["on"]

    assert triggers["pull_request"]["types"] == ["closed"]
    assert triggers["workflow_run"]["workflows"] == ["PR Comprehension Report"]
    assert triggers["workflow_run"]["types"] == ["completed"]
    assert "workflow_dispatch" in triggers


def test_archive_workflow_uses_main_code_and_static_pages_only():
    workflow = _load_workflow()
    job = workflow["jobs"]["publish-archive"]
    checkouts = [
        step
        for step in job["steps"]
        if step.get("uses") == "actions/checkout@v6"
    ]

    assert job["if"] == (
        "github.event_name != 'pull_request' || "
        "github.event.pull_request.merged == true"
    )
    assert checkouts[0]["with"] == {"ref": "main", "fetch-depth": "1"}
    assert checkouts[1]["with"] == {
        "ref": "gh-pages",
        "path": "pages",
        "fetch-depth": "1",
    }
    assert not any(
        "download-artifact" in step.get("uses", "") for step in job["steps"]
    )


def test_archive_workflow_serializes_and_preserves_pages_push():
    workflow = _load_workflow()
    job = workflow["jobs"]["publish-archive"]
    deploy = next(
        step
        for step in job["steps"]
        if step.get("uses") == "peaceiris/actions-gh-pages@v4"
    )

    assert job["concurrency"] == {
        "group": "pr-report-publish",
        "cancel-in-progress": "false",
    }
    assert deploy["with"]["publish_dir"] == "./site"
    assert deploy["with"]["keep_files"] == "true"
    assert workflow["permissions"] == {
        "contents": "write",
        "pull-requests": "read",
    }
