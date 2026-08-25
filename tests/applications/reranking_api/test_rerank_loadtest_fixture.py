"""리랭킹 부하테스트 fixture의 결정성 및 BigQuery DML 안전성 계약을 검증한다."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import provision_rerank_loadtest_fixture as provisioner
from applications.reranking_api.loadtest.rerank_fixture import (
    FIXTURE_USER_ID,
    FIXTURE_VIDEO_IDS,
    build_fixture,
    targeted_delete_sql,
    targeted_insert_sql,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RERANK_LOADTEST_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "rerank-loadtest.yml"
)
_requires_active_rerank_loadtest_workflow = pytest.mark.skipif(
    not RERANK_LOADTEST_WORKFLOW.is_file(),
    reason="조직 GKE 자원 부재로 비활성화된 rerank-loadtest.yml 워크플로우 계약 테스트",
)


class _FakeQueryJob:
    def __init__(self, sql: str) -> None:
        self._sql = sql
        self.num_dml_affected_rows = 1

    def result(self) -> list[SimpleNamespace] | _FakeQueryJob:
        if self._sql.startswith("SELECT"):
            return [SimpleNamespace(row_count=3)]
        return self


class _FakeBigQueryClient:
    def __init__(self, project: str) -> None:
        self.project = project
        self.calls: list[tuple[str, object]] = []

    def query(self, sql: str, job_config: object = None) -> _FakeQueryJob:
        self.calls.append((sql, job_config))
        return _FakeQueryJob(sql)


def test_fixture_has_exact_row_counts() -> None:
    """고정 fixture는 네 FeatureView에 필요한 정확한 행 수를 생성한다."""
    tables = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))
    rows = {table.name: table.rows for table in tables}

    assert FIXTURE_VIDEO_IDS[0] == "loadtest-video-001"
    assert FIXTURE_VIDEO_IDS[-1] == "loadtest-video-200"
    assert {name: len(value) for name, value in rows.items()} == {
        "user_static_feature": 1,
        "user_dynamic_feature": 1,
        "video_feature": 200,
        "user_category_similarity": 5,
    }


def test_fixture_rows_use_the_requested_timestamp_and_feature_contract() -> None:
    """생성값은 한 UTC 초 단위 시각과 FeatureView가 읽는 모든 원본 컬럼을 쓴다."""
    timestamp = datetime(2026, 8, 1, 12, 34, 56, tzinfo=UTC)
    tables = {table.name: table for table in build_fixture(timestamp)}

    assert tables["user_static_feature"].rows[0] == {
        "user_id": FIXTURE_USER_ID,
        "event_timestamp": timestamp,
        "age_group": "30s",
        "occupation": "engineer",
        "preferred_category": ["10", "20", "22"],
        "preferred_topics": ["technology", "engineering", "science"],
        "watch_time_band": "medium",
    }
    assert tables["user_dynamic_feature"].rows[0] == {
        "user_id": FIXTURE_USER_ID,
        "event_timestamp": timestamp,
        "recent_click_count_7d": 50,
        "recent_view_count_7d": 500,
        "recent_watch_time_7d": 36000,
        "recent_like_count_7d": 25,
        "historical_category_affinity": "10",
        "total_event_count_7d": 575,
    }
    assert tables["video_feature"].rows[0] == {
        "video_id": "loadtest-video-001",
        "event_timestamp": timestamp,
        "category_id": "10",
        "duration_sec": 61,
        "view_count": 100100,
        "like_ratio": 0.05,
        "comment_ratio": 0.005,
        "days_since_upload": 1,
        "channel_subscriber_count": 1000000,
        "channel_view_count": 50000000,
        "channel_video_count": 1000,
    }
    assert tables["user_category_similarity"].rows[-1]["topic_similarity"] == 0.50


def test_video_dml_is_exact_and_non_destructive() -> None:
    """영상 fixture DML은 200개 고정 video ID에만 한정된다."""
    table = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))[2]
    delete_sql, config = targeted_delete_sql("project-1", "feast_offline_store", table)
    insert_sql = targeted_insert_sql("project-1", "feast_offline_store", table)

    assert "video_id IN UNNEST(@video_ids)" in delete_sql
    assert config.query_parameters[0].name == "video_ids"
    assert "loadtest-video-001" in insert_sql
    assert "WRITE_TRUNCATE" not in delete_sql + insert_sql
    assert "CREATE OR REPLACE" not in delete_sql + insert_sql


@pytest.mark.parametrize("index", [0, 1, 3])
def test_user_keyed_dml_deletes_only_the_fixed_user(index: int) -> None:
    """사용자 entity 테이블은 고정 loadtest user parameter로만 삭제한다."""
    table = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))[index]
    delete_sql, config = targeted_delete_sql("project-1", "feast_offline_store", table)

    assert "user_id = @user_id" in delete_sql
    assert config.query_parameters[0].name == "user_id"
    assert config.query_parameters[0].value == FIXTURE_USER_ID


@pytest.mark.parametrize(
    ("project", "dataset"),
    [
        ("project; DROP SCHEMA x", "feast_offline_store"),
        ("project-1", "feast_offline_store; DROP TABLE x"),
        ("", "feast_offline_store"),
        ("project-1", "invalid-dataset"),
    ],
)
def test_dml_rejects_invalid_identifier(project: str, dataset: str) -> None:
    """BigQuery 식별자에 임의 SQL 단편을 허용하지 않는다."""
    table = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))[0]

    with pytest.raises(ValueError):
        targeted_delete_sql(project, dataset, table)
    with pytest.raises(ValueError):
        targeted_insert_sql(project, dataset, table)


@pytest.mark.parametrize("project", ["Project-1", "short", "-project-1", "project-"])
def test_dml_rejects_non_gcp_project_identifier(project: str) -> None:
    """GCP project ID가 아닌 식별자는 BigQuery DML에 쓸 수 없다."""
    table = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))[0]

    with pytest.raises(ValueError):
        targeted_delete_sql(project, "feast_offline_store", table)


def test_dml_accepts_bigquery_dataset_identifier_starting_with_number() -> None:
    """BigQuery dataset ID는 숫자로 시작해도 문자·숫자·밑줄만 쓰면 유효하다."""
    table = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))[0]

    sql, _ = targeted_delete_sql("project-1", "1_loadtest", table)

    assert "`project-1.1_loadtest.user_static_feature`" in sql


def test_k6_script_has_warmup_and_measurement_contract() -> None:
    """k6는 warmup을 분리하고 측정 전용 오류율을 노출해야 한다."""
    script = Path("applications/reranking_api/loadtest/rerank.js").read_text(encoding="utf-8")

    assert 'exec: "warmup"' in script
    assert 'exec: "measure"' in script
    assert "rerank_measure_duration_seconds" in script
    assert "rerank_measure_failure" in script
    assert "rate<0.01" in script
    assert "loadtest-user-001" in script
    assert "loadtest-video-200" in script
    assert 'new Trend("rerank_measure_duration_seconds")' in script
    assert 'new Trend("rerank_measure_duration_seconds", true)' not in script
    assert "response.timings.duration / 1000" in script
    for status_code in ("200", "422", "500", "503", "other"):
        assert f"rerank_measure_status_code_{status_code}" in script


def test_k6_summary_includes_p99_for_exact_latency_reporting() -> None:
    """k6 summary 설정은 측정 latency의 p99를 포함해야 한다."""
    script = Path("applications/reranking_api/loadtest/rerank.js").read_text(encoding="utf-8")

    trend_stats_match = re.search(
        r"summaryTrendStats\s*:\s*(\[[^\]]*\])",
        script,
    )
    assert trend_stats_match is not None

    trend_stats = json.loads(trend_stats_match.group(1))
    assert "p(99)" in trend_stats


def test_k6_script_offers_open_loop_mode_for_saturation_measurement() -> None:
    """개루프 모드가 있어야 도착률이 처리 용량을 넘는 상태를 만들 수 있다.

    폐루프(`constant-vus`)는 VU가 응답을 받아야 다음 요청을 보내므로 동시 요청 수가
    VU 수를 넘지 못한다. 그래서 대기열 무한 증가·부하 차단 부재처럼 과부하에서만
    드러나는 결함을 관측할 수 없고, 측정이 "정상"이라는 잘못된 합격을 낸다.
    """
    script = Path("applications/reranking_api/loadtest/rerank.js").read_text(encoding="utf-8")

    assert 'executor: "constant-arrival-rate"' in script
    assert "ARRIVAL_RATE" in script
    assert "preAllocatedVUs" in script and "maxVUs" in script
    # 폐루프 경로는 개선 전후 A/B 비교용으로 계속 유효하므로 제거하지 않는다.
    assert 'executor: "constant-vus"' in script


def test_k6_script_exposes_measure_scoped_dropped_iterations() -> None:
    """생성기 한계는 측정 구간에서만, 서버 실패와 구분되어 드러나야 한다.

    warmup까지 합산하면 측정 구간의 drop 여부를 읽을 수 없다. 또 drop을 k6 threshold
    실패로 만들면 서버가 무너진 것과 구분되지 않으므로, 노출만 하고 판정은 workflow가
    한다.
    """
    script = Path("applications/reranking_api/loadtest/rerank.js").read_text(encoding="utf-8")

    assert '"dropped_iterations{scenario:measure}"' in script
    assert 'thresholds["dropped_iterations{scenario:measure}"] = ["count>=0"]' in script


def test_k6_summary_metadata_identifies_the_load_mode() -> None:
    """개루프와 폐루프 결과를 사후에 혼동하지 않도록 모드와 도착률을 남긴다."""
    script = Path("applications/reranking_api/loadtest/rerank.js").read_text(encoding="utf-8")

    for key in ("load_mode", "arrival_rate", "pre_allocated_vus", "max_vus"):
        assert f"{key}:" in script


def test_k6_job_has_no_identity_or_token_mount() -> None:
    """k6 Job은 전용 KSA만 쓰고 토큰·Secret·권한 상승을 허용하지 않는다."""
    text = Path("deployment/loadtest/rerank-k6-job.yaml").read_text(encoding="utf-8")

    assert "serviceAccountName: rerank-loadtest" in text
    assert "automountServiceAccountToken: false" in text
    assert "restartPolicy: Never" in text
    assert "allowPrivilegeEscalation: false" in text
    assert "readOnlyRootFilesystem: true" in text
    assert "REDIS_" not in text and "secretKeyRef:" not in text


def test_k6_job_is_immutable_hardened_and_configmap_only() -> None:
    """k6 Job은 고정 digest와 one-shot 제한을 쓰며 두 ConfigMap만 mount한다."""
    text = Path("deployment/loadtest/rerank-k6-job.yaml").read_text(encoding="utf-8")

    assert "generateName: rerank-k6-" in text
    assert "namespace: loadtest" in text
    assert "app.kubernetes.io/part-of: rerank-loadtest" in text
    assert "backoffLimit: 0" in text
    assert "activeDeadlineSeconds: 600" in text
    assert "ttlSecondsAfterFinished: 86400" in text
    assert (
        "grafana/k6@sha256:1f40432b1cbe7234e977f96c362c9bc5"
        "50a2d2b583d014dd8669fe40d3e9e755" in text
    )
    assert "- k6" in text and "- run" in text and "/scripts/rerank.js" in text
    assert "runAsNonRoot: true" in text
    assert "drop:" in text and "- ALL" in text
    assert "type: RuntimeDefault" in text
    assert text.count("configMap:") == 2
    for forbidden in (
        "hostPath:",
        "persistentVolumeClaim:",
        "projected:",
        "serviceAccountToken:",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "DB_",
    ):
        assert forbidden not in text


@_requires_active_rerank_loadtest_workflow
def test_manual_workflow_keeps_load_and_snapshot_identities_separate() -> None:
    """수동 workflow는 VU gate와 Prometheus 조회를 서로 다른 identity로 실행한다."""
    text = Path(".github/workflows/rerank-loadtest.yml").read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert "workflow_dispatch:" in text
    assert "candidate_count:" in text and "- 24" in text and "- 200" in text
    assert "benchmark_label:" in text
    assert "- baseline" in text and "- optimized" in text
    assert "fixture_version:" in text and "default: rerank-v1" in text
    assert "serving_image_ref:" in text and "serving_git_sha:" in text
    assert "sweep_steps=(1 2 4 8)" in text
    assert 'read -r -a sweep_steps <<< "$ARRIVAL_RATES"' in text
    assert ".data.metrics.rerank_measure_failure.values.rate" in text
    assert "< 0.01" in text
    assert "RERANK_LOADTEST_RUNNER_SA" in text
    assert "RERANK_PROMETHEUS_SNAPSHOT_READER_SA" in text
    assert text.count("google-github-actions/auth@v2") == 2
    assert text.count("google-github-actions/get-gke-credentials@v2") == 2
    credential_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("uses") == "google-github-actions/get-gke-credentials@v2"
    ]
    assert len(credential_steps) == 2
    assert all(
        step["with"]["project_id"] == "${{ vars.GCP_PROJECT_ID }}"
        for step in credential_steps
    )
    assert "k6-summary-" in text and "metadata-" in text
    assert "creation_timestamp" in text and "completion_timestamp" in text
    assert "PROMETHEUS_SERVICE_PROXY:" in text
    assert (
        "/api/v1/namespaces/monitoring/services/"
        "http:kube-prometheus-stack-prometheus:9090/proxy" in text
    )
    assert "/api/v1/query_range" in text
    assert "step=30" in text
    for query_name in (
        "phase_p50",
        "phase_p95",
        "outcome_rate",
        "max_in_flight",
        "cpu_seconds",
        "rss",
        "cfs_throttling_ratio",
    ):
        assert query_name in text
    assert ".status == \"success\"" in text
    assert "actions/upload-artifact@v4" in text


def test_runbook_requires_materialize_and_raw_artifacts() -> None:
    """운영 절차에는 materialize 완료와 원시 증거 보존이 필수다."""
    text = Path("docs/runbooks/rerank-loadtest.md").read_text(encoding="utf-8")

    assert "feast_online_store_materialize" in text
    assert "job_summary.status=succeeded" in text
    assert "rerank-v1" in text
    assert "k6-summary-" in text
    assert "prometheus-range-" in text
    assert "CPU-seconds/request" in text


@_requires_active_rerank_loadtest_workflow
def test_manual_workflow_serializes_shared_configmaps_and_waits_for_padding() -> None:
    """공유 ConfigMap 실행은 직렬화하고 Prometheus 종료 패딩은 미래를 조회하지 않는다."""
    text = Path(".github/workflows/rerank-loadtest.yml").read_text(encoding="utf-8")

    assert "group: rerank-loadtest\n" in text
    assert "padded_end=" in text
    assert "current_time=" in text
    assert 'sleep "$((padded_end - current_time))"' in text


@_requires_active_rerank_loadtest_workflow
def test_snapshot_reader_rejects_empty_series_and_uses_gke_cfs_periods() -> None:
    """필수 Prometheus series 누락과 GKE CFS metric 이름 불일치를 통과시키지 않는다."""
    text = Path(".github/workflows/rerank-loadtest.yml").read_text(encoding="utf-8")
    cfs_line = next(
        line for line in text.splitlines() if "[cfs_throttling_ratio]=" in line
    )

    assert "data.result | length" in text
    assert "Prometheus query returned no series" in text
    assert "container_cpu_cfs_throttled_periods_total" in cfs_line
    assert "container_cpu_cfs_periods_total" in cfs_line
    assert cfs_line.count("sum by (pod, container)") == 2
    assert "container_cpu_cfs_throttled_seconds_total" not in cfs_line
    assert "clamp_min(" not in cfs_line
    assert "all(.data.result[].values[];" in text
    assert "(.value | type)" not in text
    assert "expected_sample_count=" in text
    assert "minimum_cfs_samples=" in text
    assert '(.values | length) >= $minimum_samples' in text
    assert 'queries(${#queries[@]})' in text
    assert 'jq -e \'type == "object"\'' in text
    assert "Prometheus response is not valid JSON" in text
    assert 'validate_prometheus_result "$query_name" "$response_path"' in text
    assert "snapshot_failed=0" in text
    assert "prometheus-validation-failures.txt" in text
    assert ".request-status" in text
    assert ".request-stderr" in text
    assert "if (( snapshot_failed )); then" in text


@_requires_active_rerank_loadtest_workflow
def test_snapshot_reader_cfs_validation_accepts_query_range_matrix() -> None:
    """CFS 검증식은 query_range matrix의 values를 실제 jq로 판정한다."""
    if shutil.which("jq") is None:
        pytest.skip("jq is required to execute the workflow validation expression")

    text = Path(".github/workflows/rerank-loadtest.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    query_step = next(
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("name") == "Query padded Prometheus ranges"
    )
    script = query_step["run"]
    match = re.search(
        r'''if \[\[ "\$query_name" == "cfs_throttling_ratio" \]\] && ! jq -e --argjson minimum_samples "\$minimum_cfs_samples" '\n(?P<program>.*?)\n\s*' "\$response_path"''',
        script,
        re.DOTALL,
    )
    assert match is not None, "workflow CFS jq validation block format changed; update this test"
    jq_program = match.group("program")
    generic_match = re.search(
        r'''if ! jq -e '(?P<program>\s*\.status == "success".*?)\s*'\s+"\$response_path"''',
        script,
        re.DOTALL,
    )
    assert generic_match is not None, "workflow generic Prometheus jq validation block not found"
    generic_jq_program = generic_match.group("program")

    def run_jq(response: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["jq", "-e", "--argjson", "minimum_samples", "2", jq_program],
            input=json.dumps(response),
            capture_output=True,
            check=False,
            text=True,
        )

    def run_generic_jq(response: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["jq", "-e", generic_jq_program],
            input=json.dumps(response),
            capture_output=True,
            check=False,
            text=True,
        )

    valid = run_jq(
        {
            "data": {
                "result": [
                    {"metric": {}, "values": [[1, "0"], [2, "0.25"]]},
                ]
            }
        }
    )
    out_of_range = run_jq(
        {
            "data": {
                "result": [
                    {"metric": {}, "values": [[1, "1.01"]]},
                ]
            }
        }
    )
    empty_samples = run_jq(
        {"data": {"result": [{"metric": {}, "values": []}]}}
    )
    non_finite = [
        run_jq({"data": {"result": [{"metric": {}, "values": [[1, value]]}]}})
        for value in ("NaN", "+Inf", "-Inf")
    ]
    empty_series = run_generic_jq(
        {"status": "success", "data": {"result": []}}
    )

    assert valid.returncode == 0, valid.stderr
    assert out_of_range.returncode != 0
    assert empty_samples.returncode != 0
    assert empty_series.returncode != 0
    for result in non_finite:
        assert result.returncode != 0


@_requires_active_rerank_loadtest_workflow
def test_workflow_classifies_generator_limited_open_loop_runs_as_invalid() -> None:
    """개루프에서 생성기가 도착률을 못 지킨 측정은 서버 결과로 보고되면 안 된다.

    dropped_iterations가 있으면 그 구간은 서버가 아니라 부하 생성기의 한계를 잰
    것이다. 이를 서버 결과로 받아들이면 용량을 실제보다 낮게 단정하게 되므로,
    무효로 표시하고 더 높은 도착률로 진행하지 않는다.
    """
    workflow = Path(".github/workflows/rerank-loadtest.yml").read_text(encoding="utf-8")

    assert '.data.metrics["dropped_iterations{scenario:measure}"].values.count' in workflow
    assert "invalid_generator_limited" in workflow
    # 값이 아예 없으면 0으로 읽어 통과시키지 않는다.
    assert "dropped_iterations_missing" in workflow
    assert "invalid; rerun required" in workflow


@_requires_active_rerank_loadtest_workflow
def test_manual_workflow_preserves_runner_artifact_layout_for_reader() -> None:
    """runner upload, reader download·glob, 최종 upload는 같은 raw 경로를 사용한다."""
    text = Path(".github/workflows/rerank-loadtest.yml").read_text(encoding="utf-8")

    assert text.count("path: runner/raw") == 3
    assert "metadata_files=(runner/raw/metadata-step-*.json)" in text
    assert 'response_path="runner/raw/prometheus-' in text


@_requires_active_rerank_loadtest_workflow
def test_workflow_validates_fixture_and_avoids_sourced_settings() -> None:
    """자유 입력은 allowlist를 통과하고 settings 값은 shell source되지 않는다."""
    workflow = Path(".github/workflows/rerank-loadtest.yml").read_text(encoding="utf-8")
    manifest = Path("deployment/loadtest/rerank-k6-job.yaml").read_text(encoding="utf-8")

    assert "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$" in workflow
    assert "--from-literal=rerank.env" not in workflow
    for key in (
        "BASE_URL",
        "CANDIDATE_COUNT",
        "VUS",
        "WARMUP_SECONDS",
        "MEASURE_SECONDS",
        "FIXTURE_VERSION",
        "BENCHMARK_LABEL",
        "SERVING_IMAGE_REF",
        "SERVING_GIT_SHA",
    ):
        assert f"--from-literal={key}=" in workflow
    assert ". /settings/" not in manifest
    assert "envFrom:" in manifest and "configMapRef:" in manifest


@_requires_active_rerank_loadtest_workflow
def test_each_vu_binds_one_versioned_immutable_settings_configmap() -> None:
    """각 VU Job은 생성 시점의 고유 settings ConfigMap을 env와 volume에 함께 bind한다."""
    workflow = Path(".github/workflows/rerank-loadtest.yml").read_text(encoding="utf-8")
    manifest = Path("deployment/loadtest/rerank-k6-job.yaml").read_text(encoding="utf-8")

    assert (
        'settings_config_map="rerank-loadtest-settings-'
        '${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${step}"' in workflow
    )
    assert "immutable = true" in workflow
    assert "job_manifest=" in workflow
    assert "rerank-loadtest-settings-placeholder" in workflow
    assert manifest.count("name: rerank-loadtest-settings-placeholder") == 2
    assert 'settings_config_map: $settings_config_map' in workflow
    assert ".metadata.load_mode == $expected_load_mode" in workflow
    assert ".metadata.arrival_rate == $expected_step" in workflow
    assert ".metadata.vus == $expected_step" in workflow


@_requires_active_rerank_loadtest_workflow
def test_failure_metadata_precedes_summary_and_records_status() -> None:
    """Job 종료 metadata는 summary보다 먼저 남고 후속 실패 상태도 갱신된다."""
    text = Path(".github/workflows/rerank-loadtest.yml").read_text(encoding="utf-8")

    first_metadata_write = text.index('write_metadata "$result"')
    summary_retrieval = text.index('summary_path="runner/raw/k6-summary-')
    assert first_metadata_write < summary_retrieval
    assert 'completion_timestamp="$(date --utc' in text
    assert "result=timeout" in text
    assert "job_result: $job_result" in text
    for status in (
        "pod_not_found",
        "summary_retrieval_failed",
        "measurement_gate_failed",
        "succeeded",
    ):
        assert f'write_metadata "{status}"' in text


@_requires_active_rerank_loadtest_workflow
def test_snapshot_reader_exports_partial_completed_jobs_after_runner_failure() -> None:
    """runner 실패 후에도 reader는 완료된 1~4개 Job의 raw range만 보존한다."""
    text = Path(".github/workflows/rerank-loadtest.yml").read_text(encoding="utf-8")

    reader_start = text.index("  prometheus-snapshot-reader:")
    reader = text[reader_start:]
    assert "needs: loadtest-runner" in reader
    assert "if: ${{ always() }}" in reader
    assert "continue-on-error: true" in reader
    assert "metadata_count" in reader
    # 상한은 고정 4가 아니라 이 실행이 선언한 sweep 길이여야 한다. 개루프는
    # 도착률 개수가 실행마다 다르므로 4로 고정하면 정상 증거를 실패로 만든다.
    assert "metadata_count > expected_step_count" in reader
    assert "expected_step_count=4" in reader
    assert "metadata_count -ne 4" not in reader
    assert 'select(.job_result == "complete")' in reader
    assert "No completed Job metadata" in reader
    assert "steps.evidence.outputs.completed_count != '0'" in reader
    assert "path: runner/raw" in reader


@_requires_active_rerank_loadtest_workflow
def test_settings_configmap_is_owned_by_ttl_job() -> None:
    """VU별 settings ConfigMap은 Job UID ownerReference로 TTL GC에 연결된다."""
    workflow = Path(".github/workflows/rerank-loadtest.yml").read_text(encoding="utf-8")
    manifest = Path("deployment/loadtest/rerank-k6-job.yaml").read_text(encoding="utf-8")

    assert 'job_uid="$(kubectl get job "$job_name"' in workflow
    assert 'kubectl patch configmap "$settings_config_map"' in workflow
    assert "ownerReferences" in workflow
    assert 'apiVersion: "batch/v1"' in workflow
    assert 'kind: "Job"' in workflow
    assert "controller: false" in workflow
    assert "blockOwnerDeletion: false" in workflow
    assert "Could not attach Job ownerReference" in workflow
    assert "ttlSecondsAfterFinished: 86400" in manifest


def test_provisioner_default_dry_run_executes_only_count_selects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """기본 CLI는 fixture 행 수만 읽고 어떠한 DML도 실행하지 않는다."""
    client = _FakeBigQueryClient("project-1")
    monkeypatch.setattr(provisioner.bigquery, "Client", lambda project: client)

    assert provisioner.main(["--project", "project-1"]) == 0

    assert len(client.calls) == 4
    assert all(sql.startswith("SELECT COUNT(*) AS row_count") for sql, _ in client.calls)
    assert all("DELETE FROM" not in sql and "INSERT INTO" not in sql for sql, _ in client.calls)
    assert all(config is not None for _, config in client.calls)


def test_provisioner_apply_executes_only_targeted_delete_and_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """명시적 --apply는 네 fixture table의 DELETE/INSERT만 실행한다."""
    client = _FakeBigQueryClient("project-1")
    monkeypatch.setattr(provisioner.bigquery, "Client", lambda project: client)

    assert provisioner.main(["--project", "project-1", "--apply"]) == 0

    assert len(client.calls) == 8
    delete_sqls = [sql for sql, _ in client.calls if sql.startswith("DELETE FROM")]
    insert_sqls = [sql for sql, _ in client.calls if sql.startswith("INSERT INTO")]
    assert len(delete_sqls) == len(insert_sqls) == 4
    assert all("WHERE user_id = @user_id" in sql or "WHERE video_id IN UNNEST(@video_ids)" in sql for sql in delete_sqls)
    assert all("WRITE_TRUNCATE" not in sql and "CREATE OR REPLACE" not in sql for sql, _ in client.calls)
