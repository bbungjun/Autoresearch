"""simulate_policy_round 배치 테스트 — stub Reranker + rule-based LLM."""

import builtins
import sys

import numpy as np
import pandas as pd
import pytest

from autoresearch.action_log_generation.llm_generator import RuleBasedActionLogGenerator
from autoresearch.feature_engineering.model_contract import FeatureContractError, MODEL_FEATURE_COLUMNS
from autoresearch.recommendation.simulate_policy_round import (
    _to_candidate_videos,
    build_pool_feature_frame,
    main,
)
from applications.reranking_api.service import Reranker


class _CategoryLovingModel:
    """category_id가 'Gaming'인 후보에 높은 확률을 주는 stub predict_proba."""

    def predict_proba(self, features):
        p1 = np.where(features["category_id"].astype(str) == "Gaming", 0.9, 0.1)
        return np.column_stack([1 - p1, p1])


def _videos_raw(n: int = 30) -> pd.DataFrame:
    rows = []
    for i in range(n):
        cat = "Gaming" if i % 3 == 0 else "Music"
        rows.append(
            {
                "video_id": f"v{i:03d}",
                "categoryId": cat,
                "duration": 100 + i,
                "viewCount": 1000 + i,
                "likeCount": 10,
                "commentCount": 1,
                "publishedAt": "2026-07-01",
                "title": f"{cat} video {i}",
                "description": f"{cat} 설명 {i}",
                "tags": "",
                "channelSubscriberCount": 100_000 + i,
                "channelViewCount": 10_000_000 + i,
                "channelVideoCount": 100 + i,
            }
        )
    return pd.DataFrame(rows)


def _personas(n: int = 4) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "uuid": [f"u{i}" for i in range(n)],
            "age": [25] * n,
            "occupation": ["student"] * n,
            "watch_time_band": ["night"] * n,
            "hobbies_and_interests_list": ['["gaming"]'] * n,
        }
    )


def _virtual_users(n: int = 4) -> list[dict]:
    return [
        {
            "user_id": f"u{i}",
            "age": 25,
            "occupation": "student",
            "interest_keywords": ["게임"],
            "hobby_keywords": [],
            "lifestyle_keywords": [],
            "primary_categories": ["Gaming"],
        }
        for i in range(n)
    ]


def _empty_events() -> pd.DataFrame:
    # 빈 프레임은 컬럼 dtype이 없으면 DuckDB가 INTEGER로 추론해 user_id 문자열
    # 비교가 깨진다. 실데이터(read_csv) 경로와 동일하게 문자열/정수형을 명시한다.
    return pd.DataFrame(
        columns=["event_id", "user_id", "video_id", "timestamp", "clicked", "liked", "watch_time_sec"]
    ).astype(
        {
            "event_id": "string",
            "user_id": "string",
            "video_id": "string",
            "timestamp": "string",
            "clicked": "Int64",
            "liked": "Int64",
            "watch_time_sec": "Int64",
        }
    )


def _events_with_history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "event_id": ["e1", "e2"],
            "user_id": ["u0", "u0"],
            "video_id": ["v000", "v001"],
            "timestamp": ["2026-07-20 10:00:00", "2026-07-18 10:00:00"],
            "clicked": [1, 0],
            "liked": [1, 0],
            "watch_time_sec": [120, 0],
        }
    ).astype(
        {
            "event_id": "string",
            "user_id": "string",
            "video_id": "string",
            "timestamp": "string",
            "clicked": "Int64",
            "liked": "Int64",
            "watch_time_sec": "Int64",
        }
    )


@pytest.fixture()
def stub_reranker() -> Reranker:
    return Reranker(
        model=_CategoryLovingModel(),
        feature_columns=MODEL_FEATURE_COLUMNS,
        categorical_categories={"category_id": ("Gaming", "Music")},
    )


def test_build_pool_feature_frame_covers_model_columns(stub_reranker):
    frame = build_pool_feature_frame(
        personas=_personas(1),
        events=_empty_events(),
        videos_raw=_videos_raw(6),
        user_id="u0",
        as_of="2026-07-20 00:00:00",
    )
    assert len(frame) == 6
    for column in stub_reranker.feature_columns:
        assert column in frame.columns, column


def test_build_pool_feature_frame_includes_all_missing_contract_features():
    frame = build_pool_feature_frame(
        personas=_personas(1),
        events=_events_with_history(),
        videos_raw=_videos_raw(2),
        user_id="u0",
        as_of="2026-07-22 00:00:00",
        snapshot_date="2026-07-22",
    )

    assert set(MODEL_FEATURE_COLUMNS).issubset(frame.columns)
    assert frame.loc[0, "watch_time_band"] == "night"
    assert frame.loc[0, "recent_view_count_7d"] == 1
    assert frame.loc[0, "total_event_count_7d"] == 5
    assert frame.loc[0, "channel_subscriber_count"] == 100_000
    assert frame.loc[0, "channel_view_count"] == 10_000_000
    assert frame.loc[0, "channel_video_count"] == 100

    candidates = _to_candidate_videos(frame, MODEL_FEATURE_COLUMNS)
    assert tuple(candidates[0].features) == MODEL_FEATURE_COLUMNS


@pytest.mark.parametrize(
    "feature_columns",
    [MODEL_FEATURE_COLUMNS[:-1], MODEL_FEATURE_COLUMNS[1:] + MODEL_FEATURE_COLUMNS[:1]],
)
def test_to_candidate_videos_rejects_noncanonical_feature_contract(feature_columns):
    with pytest.raises(FeatureContractError):
        _to_candidate_videos(pd.DataFrame(), feature_columns)


def test_build_pool_feature_frame_skip_embedding_passes_through():
    # skip_embedding이 compute_interaction_columns까지 그대로 전달되는지 고정한다(#426).
    # 전달이 끊기면 conftest의 임베딩 대역이 실수를 채워 넣어 이 단언이 깨진다.
    common = dict(
        personas=_personas(1),
        events=_empty_events(),
        videos_raw=_videos_raw(3),
        user_id="u0",
        as_of="2026-07-20 00:00:00",
    )
    skipped = build_pool_feature_frame(**common, skip_embedding=True)
    assert skipped["topic_similarity"].isna().all()

    default = build_pool_feature_frame(**common)  # 기본값은 기존 동작(임베딩 계산) 유지
    assert default["topic_similarity"].notna().all()


def test_build_pool_feature_frame_snapshot_date_decoupled_from_as_of():
    # 영상 나이(days_since_upload)는 snapshot_date 기준, 유저 이력은 as_of 기준으로 분리한다.
    common = dict(
        personas=_personas(1),
        events=_empty_events(),
        videos_raw=_videos_raw(1),  # publishedAt 2026-07-01
        user_id="u0",
        as_of="2026-07-20 00:00:00",
    )
    explicit = build_pool_feature_frame(**common, snapshot_date="2026-07-21")
    assert explicit["days_since_upload"].iloc[0] == 20

    fallback = build_pool_feature_frame(**common)  # 기본값은 기존 동작(as_of 날짜) 유지
    assert fallback["days_since_upload"].iloc[0] == 19


def test_round_report_prefers_model_policy_when_model_is_right(tmp_path, stub_reranker):
    """모델이 유저 취향(Gaming)을 맞히면 합동 정규화 후 model CTR ≥ baseline CTR."""
    report = main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    assert set(report["policies"]) == {"baseline", "model"}
    model = report["policies"]["model"]
    baseline = report["policies"]["baseline"]
    assert model["impressions"] == 6 * 4
    assert 0.0 <= report["overlap_jaccard_mean"] <= 1.0
    # rule-based generator는 관련도 기반 propensity를 주므로 Gaming만 노출한
    # model 정책의 평균 propensity가 baseline(혼합 노출) 이상이어야 한다.
    assert model["mean_click_propensity"] >= baseline["mean_click_propensity"]
    assert (tmp_path / "policy_round_report.json").is_file()
    assert (tmp_path / "event_log.parquet").is_file()


def test_round_events_are_tagged_per_policy(tmp_path, stub_reranker):
    import pyarrow.parquet as pq

    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    table = pq.read_table(tmp_path / "event_log.parquet").to_pandas()
    assert set(table["policy"].dropna().unique()) == {"baseline", "model"}
    assert (table["source"] == "online_simulated").all()
    model_imps = table[(table["policy"] == "model") & (table["event_type"] == "impression")]
    assert model_imps["ctr_score"].notna().all()
    assert (model_imps["policy_version"] == "stub-run").all()


def test_round_final_parquet_keeps_context_free_slate_ids_null(tmp_path, stub_reranker):
    import pyarrow.parquet as pq

    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )

    slate_ids = pq.read_table(tmp_path / "event_log.parquet", columns=["slate_id"])
    assert slate_ids.num_rows > 0
    assert slate_ids.column("slate_id").null_count == slate_ids.num_rows


def test_round_output_feeds_retraining_path(tmp_path, stub_reranker):
    """policy=model 필터 후 derive_wide_events가 라벨을 복원할 수 있어야 한다."""
    import pyarrow.parquet as pq

    from autoresearch.model_training.build_training_dataset import derive_wide_events

    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    table = pq.read_table(tmp_path / "event_log.parquet").to_pandas()
    model_long = table[table["policy"] == "model"][
        ["event_id", "event_timestamp", "user_id", "event_type", "video_id", "watch_time_sec"]
    ]
    wide = derive_wide_events(model_long)
    impressions = len(model_long[model_long["event_type"] == "impression"])
    assert len(wide) == impressions
    assert wide["clicked"].sum() >= 1  # click_threshold=0.0으로 유저별 최고 1개는 항상 클릭


def test_round_clicks_are_at_most_one_per_user(tmp_path, stub_reranker) -> None:
    import pyarrow.parquet as pq

    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    table = pq.read_table(tmp_path / "event_log.parquet").to_pandas()
    clicks = table[table["event_type"] == "click"]
    per_user = clicks.groupby(["policy", "user_id"]).size()
    assert (per_user <= 1).all()


def test_render_report_html_contains_policies_and_values():
    from autoresearch.reporting.report_html import render_report_html

    report = {
        "policy_version": "run-x", "k": 10, "exploration_ratio": 0.1,
        "click_threshold": 0.55, "seed": 42, "users": 100,
        "skipped_users": [], "dropped_exposures_without_judgment": 0,
        "overlap_jaccard_mean": 0.25, "unseen_category_counts": {},
        "quarantined_chunks": 0,
        "policies": {
            "baseline": {"impressions": 1000, "clicks": 15, "ctr": 0.015,
                          "mean_click_propensity": 0.31,
                          "exploration_impressions": 0, "exploration_clicks": 0},
            "model": {"impressions": 1000, "clicks": 25, "ctr": 0.025,
                       "mean_click_propensity": 0.44,
                       "exploration_impressions": 100, "exploration_clicks": 2},
        },
    }
    html = render_report_html(report)
    assert "<!doctype html>" in html.lower()
    assert "baseline" in html and "model" in html
    assert "2.50%" in html and "1.50%" in html  # 정책별 CTR
    assert "run-x" in html
    assert "<table" in html  # 접근성용 데이터 테이블
    assert "http" not in html.split("</head>")[0]  # head에 외부 리소스 없음


def test_round_writes_html_report(tmp_path, stub_reranker):
    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    html_path = tmp_path / "policy_round_report.html"
    assert html_path.is_file()
    assert "stub-run" in html_path.read_text(encoding="utf-8")


def test_round_dumps_drafts_and_meta(tmp_path, stub_reranker):
    """LLM 판정이 draft parquet + 사이드카 메타로 남아야 한다(캘리브레이션 입력)."""
    import json

    from autoresearch.action_log_generation.pipeline import read_action_log_draft_parquet
    from autoresearch.action_log_generation.schema import (
        ACTION_LOG_SCHEMA_VERSION,
        PROMPT_VERSION,
    )

    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        as_of="2026-07-20 00:00:00",
        policy_version="stub-run",
        output_dir=str(tmp_path),
        input_paths={"personas": "demo/personas.csv"},
    )

    drafts = read_action_log_draft_parquet(tmp_path / "action_log_drafts.parquet")
    assert drafts
    assert all(0.0 <= d.click_propensity <= 1.0 for d in drafts)

    meta = json.loads((tmp_path / "action_log_drafts_meta.json").read_text(encoding="utf-8"))
    assert meta["llm_model"] == "fixture-rule-action-log"
    assert meta["prompt_version"] == PROMPT_VERSION
    assert meta["schema_version"] == ACTION_LOG_SCHEMA_VERSION
    assert meta["exposure_args"] == {
        "seed": 42,
        "k": 6,
        "exploration_ratio": 0.0,
        "as_of": "2026-07-20 00:00:00",
    }
    assert meta["policy_version"] == "stub-run"
    assert meta["virtual_users"] == 4
    assert meta["users"] == 4
    assert meta["drafts"] == len(drafts)
    assert meta["inputs"] == {"personas": "demo/personas.csv"}
    # click_threshold는 리플레이에서 바꾸는 값이므로 노출 인자에 없어야 한다.
    assert "click_threshold" not in meta["exposure_args"]


def test_round_meta_virtual_users_and_users_diverge_when_persona_missing(tmp_path, stub_reranker):
    """persona가 없는 유저는 skipped_users로 격리되어 virtual_users > users로 갈라진다.

    메타의 virtual_users는 입력 virtual user 수를, users는 노출 결정에 성공한
    유저 수를 각각 그대로 반영해야 한다(두 값이 뒤바뀌면 안 됨).
    """
    import json

    main(
        personas=_personas(2),  # u0, u1만 persona 보유
        virtual_users=_virtual_users(4),  # u0..u3
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        as_of="2026-07-20 00:00:00",
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )

    report = json.loads((tmp_path / "policy_round_report.json").read_text(encoding="utf-8"))
    assert report["users"] == 2
    assert set(report["skipped_users"]) == {"u2", "u3"}

    meta = json.loads((tmp_path / "action_log_drafts_meta.json").read_text(encoding="utf-8"))
    assert meta["virtual_users"] == 4
    assert meta["users"] == 2
    assert meta["virtual_users"] != meta["users"]


def _run_round(tmp_path, stub_reranker, **overrides):
    """덤프까지 수행하는 표준 라운드 실행 헬퍼."""
    kwargs = dict(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        as_of="2026-07-20 00:00:00",
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    kwargs.update(overrides)
    return main(**kwargs)


def _load_replay(round_dir, *, with_exposure_keys=False):
    """덤프된 판정과 계보를 DraftReplay로 되살린다.

    with_exposure_keys=False(기본)는 exposure_keys가 없던 구버전 사이드카의
    리플레이를 재현한다 — 기존 커버리지 휴리스틱 테스트들이 이 폴백 경로를
    계속 검증하도록 유지하기 위해서다.
    """
    import json

    from autoresearch.action_log_generation.pipeline import read_action_log_draft_parquet
    from autoresearch.recommendation.simulate_policy_round import (
        DRAFTS_FILENAME,
        DRAFTS_META_FILENAME,
        DraftReplay,
    )

    meta = json.loads((round_dir / DRAFTS_META_FILENAME).read_text(encoding="utf-8"))
    exposure_keys = None
    if with_exposure_keys:
        exposure_keys = {
            user: frozenset(videos) for user, videos in meta["exposure_keys"].items()
        }
    return DraftReplay(
        drafts=read_action_log_draft_parquet(round_dir / DRAFTS_FILENAME),
        llm_model=str(meta["llm_model"]),
        exposure_args=meta["exposure_args"],
        exposure_keys=exposure_keys,
    )


def test_replay_reproduces_identical_round(tmp_path, stub_reranker):
    """같은 커트라인으로 리플레이하면 LLM 없이 동일한 결과가 나와야 한다."""
    first_dir = tmp_path / "a"
    original = _run_round(
        first_dir, stub_reranker, generator=RuleBasedActionLogGenerator()
    )

    replayed = _run_round(
        tmp_path / "b",
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        output_dir=str(tmp_path / "b"),
    )

    assert replayed["policies"] == original["policies"]
    assert replayed["dropped_exposures_without_judgment"] == 0


def test_replay_with_higher_threshold_reduces_clicks(tmp_path, stub_reranker):
    """판정을 재사용한 채 커트라인만 올리면 클릭이 줄어야 한다(캘리브레이션 전제)."""
    first_dir = tmp_path / "a"
    original = _run_round(
        first_dir, stub_reranker, generator=RuleBasedActionLogGenerator()
    )

    strict = _run_round(
        tmp_path / "b",
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        click_threshold=1.0,  # 어떤 propensity도 넘을 수 없는 커트라인
        output_dir=str(tmp_path / "b"),
    )

    assert original["policies"]["model"]["clicks"] >= 1
    assert strict["policies"]["model"]["clicks"] == 0
    assert strict["policies"]["baseline"]["clicks"] == 0


def test_replay_fails_when_a_users_slate_is_partially_covered(tmp_path, stub_reranker):
    """유저 슬레이트가 일부만 덮이면(노출 집합 불일치 신호) 실패해야 한다.

    한 유저의 draft를 전부 지우는 것(=원본에서 quarantine된 유저)과, 일부만
    지우는 것(=노출 집합이 어긋난 신호)은 의미가 다르다. 이 테스트는 후자를
    명시적으로 만든다 — 대상 유저에게 draft를 1건 이상 남겨 "일부만 덮임"을
    보장한다.
    """
    first_dir = tmp_path / "a"
    _run_round(first_dir, stub_reranker, generator=RuleBasedActionLogGenerator())

    replay = _load_replay(first_dir)
    from autoresearch.recommendation.simulate_policy_round import DraftReplay

    target_user = replay.drafts[0].user_id
    user_drafts = [d for d in replay.drafts if d.user_id == target_user]
    assert len(user_drafts) > 1, "부분 커버리지를 만들려면 대상 유저에 draft가 2건 이상 필요합니다"

    removed_one = user_drafts[0]
    partial = [d for d in replay.drafts if d is not removed_one]
    truncated = DraftReplay(
        drafts=partial,
        llm_model=replay.llm_model,
        exposure_args=replay.exposure_args,
    )
    # 대상 유저는 draft가 남아 있되(부분 커버리지) 전부는 아니어야 한다.
    remaining_for_target = [d for d in partial if d.user_id == target_user]
    assert 0 < len(remaining_for_target) < len(user_drafts)

    with pytest.raises(ValueError, match="partially cover"):
        _run_round(
            tmp_path / "b",
            stub_reranker,
            generator=None,
            replay=truncated,
            output_dir=str(tmp_path / "b"),
        )


def test_replay_tolerates_a_user_with_no_drafts_at_all(tmp_path, stub_reranker):
    """draft가 하나도 없는 유저(원본에서 quarantine)는 관용하고 dropped로 계수해야 한다.

    발견 사항 2 재현: 실 LLM 라운드에서 quarantine된 유저가 있으면 그 유저의
    draft는 parquet에 아예 없다. 이런 라운드의 리플레이가 항상 실패하면 안
    된다 — 원본 라운드와 동일하게 그 유저 노출만 dropped로 세고 성공해야 한다.
    """
    first_dir = tmp_path / "a"
    _run_round(first_dir, stub_reranker, generator=RuleBasedActionLogGenerator())

    replay = _load_replay(first_dir)
    from autoresearch.recommendation.simulate_policy_round import DraftReplay

    quarantined_user = replay.drafts[0].user_id
    remaining = [d for d in replay.drafts if d.user_id != quarantined_user]
    assert len(remaining) < len(replay.drafts)  # 해당 유저의 draft가 전부 빠졌는지 확인

    replay_without_user = DraftReplay(
        drafts=remaining,
        llm_model=replay.llm_model,
        exposure_args=replay.exposure_args,
    )

    replayed = _run_round(
        tmp_path / "b",
        stub_reranker,
        generator=None,
        replay=replay_without_user,
        output_dir=str(tmp_path / "b"),
    )
    assert replayed["dropped_exposures_without_judgment"] > 0


def test_round_meta_records_exposure_keys(tmp_path, stub_reranker):
    """덤프 사이드카에 유저별 합집합 노출 키 집합이 기록되어야 한다(#274)."""
    import json

    from autoresearch.recommendation.simulate_policy_round import DRAFTS_META_FILENAME

    report = _run_round(tmp_path, stub_reranker, generator=RuleBasedActionLogGenerator())

    meta = json.loads((tmp_path / DRAFTS_META_FILENAME).read_text(encoding="utf-8"))
    assert len(meta["exposure_keys"]) == report["users"]
    for videos in meta["exposure_keys"].values():
        assert videos, "노출된 유저의 키 목록은 비어 있을 수 없습니다"
        assert videos == sorted(videos)


def test_replay_with_exposure_keys_tolerates_partial_user_drafts(tmp_path, stub_reranker):
    """노출 키 집합이 있으면 유저의 부분 draft 누락(청크 부분 격리)을 관용해야 한다(#274).

    chunk_size > 0 라운드에서 한 유저의 청크 일부만 격리되면 그 유저의 draft가
    일부만 남는다. 구버전 휴리스틱은 이를 노출 불일치로 오인해 실패했지만,
    노출 키 집합 비교가 통과하면 미판정 노출은 원본 격리로 확정되므로 관용하고
    dropped로 계수해야 한다.
    """
    first_dir = tmp_path / "a"
    _run_round(first_dir, stub_reranker, generator=RuleBasedActionLogGenerator())

    replay = _load_replay(first_dir, with_exposure_keys=True)
    from autoresearch.recommendation.simulate_policy_round import DraftReplay

    target_user = replay.drafts[0].user_id
    user_drafts = [d for d in replay.drafts if d.user_id == target_user]
    assert len(user_drafts) > 1

    removed_one = user_drafts[0]
    partial = DraftReplay(
        drafts=[d for d in replay.drafts if d is not removed_one],
        llm_model=replay.llm_model,
        exposure_args=replay.exposure_args,
        exposure_keys=replay.exposure_keys,
    )

    replayed = _run_round(
        tmp_path / "b",
        stub_reranker,
        generator=None,
        replay=partial,
        output_dir=str(tmp_path / "b"),
    )
    assert replayed["dropped_exposures_without_judgment"] > 0


def test_replay_with_exposure_keys_fails_on_exposure_set_mismatch(tmp_path, stub_reranker):
    """노출 키 집합이 원본과 다르면(비디오 구성 상이) 정확 비교가 실패해야 한다."""
    first_dir = tmp_path / "a"
    _run_round(first_dir, stub_reranker, generator=RuleBasedActionLogGenerator())

    replay = _load_replay(first_dir, with_exposure_keys=True)
    from autoresearch.recommendation.simulate_policy_round import DraftReplay

    assert replay.exposure_keys is not None
    target_user = next(iter(replay.exposure_keys))
    tampered_keys = dict(replay.exposure_keys)
    # 원본 노출에서 비디오 1개를 빼서 "판정 라운드의 노출이 달랐다"를 재현한다.
    tampered_keys[target_user] = frozenset(sorted(tampered_keys[target_user])[:-1])
    tampered = DraftReplay(
        drafts=replay.drafts,
        llm_model=replay.llm_model,
        exposure_args=replay.exposure_args,
        exposure_keys=tampered_keys,
    )

    with pytest.raises(ValueError, match="노출 키 집합"):
        _run_round(
            tmp_path / "b",
            stub_reranker,
            generator=None,
            replay=tampered,
            output_dir=str(tmp_path / "b"),
        )


def test_replay_with_exposure_keys_fails_on_user_set_mismatch(tmp_path, stub_reranker):
    """노출 유저 집합이 원본과 다르면 실패해야 한다 — 전원 zero-coverage 은폐 차단.

    구버전 휴리스틱에서는 유저 수는 같고 id만 다른 파일로 리플레이하면 전원이
    zero-coverage가 되어 draft 유저 검사에만 의존했다. 노출 키 집합 비교는
    유저 집합 차이를 직접 검출한다.
    """
    first_dir = tmp_path / "a"
    _run_round(first_dir, stub_reranker, generator=RuleBasedActionLogGenerator())

    replay = _load_replay(first_dir, with_exposure_keys=True)
    from autoresearch.recommendation.simulate_policy_round import DraftReplay

    assert replay.exposure_keys is not None
    dropped_user = next(iter(replay.exposure_keys))
    reduced_keys = {
        user: keys for user, keys in replay.exposure_keys.items() if user != dropped_user
    }
    # 해당 유저의 draft도 함께 제거해 draft 유저 검사가 아니라 유저 집합
    # 비교가 검출함을 보장한다.
    reduced = DraftReplay(
        drafts=[d for d in replay.drafts if d.user_id != dropped_user],
        llm_model=replay.llm_model,
        exposure_args=replay.exposure_args,
        exposure_keys=reduced_keys,
    )

    with pytest.raises(ValueError, match="노출 유저 집합"):
        _run_round(
            tmp_path / "b",
            stub_reranker,
            generator=None,
            replay=reduced,
            output_dir=str(tmp_path / "b"),
        )


class _FlakyGenerator:
    """특정 (user, video)가 포함된 청크에서만 실패하는 판정기.

    chunk_size > 0에서 같은 유저의 청크 일부만 quarantine되는 실제 경로를
    재현한다(#274). 나머지 청크는 RuleBased 판정에 위임한다.
    """

    def __init__(self, poison_user: str, poison_video: str) -> None:
        self.model_name = "flaky-rule"
        self._delegate = RuleBasedActionLogGenerator(model_name=self.model_name)
        self._poison_user = poison_user
        self._poison_video = poison_video

    def generate(self, virtual_user: dict, videos: list[dict]) -> str:
        user_id = str(virtual_user.get("user_id", ""))
        if user_id == self._poison_user and any(
            str(v.get("video_id")) == self._poison_video for v in videos
        ):
            raise RuntimeError("intentional chunk failure for partial-quarantine repro")
        return self._delegate.generate(virtual_user, videos)


def test_partially_quarantined_chunk_round_replays_successfully(tmp_path, stub_reranker):
    """chunk_size > 0 부분 격리 라운드를 같은 노출 인자로 리플레이하면 성공해야 한다(#274).

    이슈 재현 경로 그대로: 유저 한 명의 청크 하나만 격리된 판정 라운드를
    만들고(draft parquet에는 그 (user, video)만 없다), 노출 키 집합이 기록된
    사이드카로 리플레이한다. 구버전 휴리스틱이라면 부분 커버리지로 실패했을
    라운드다.
    """
    # 1) 정찰 라운드로 결정적 노출에서 poison 대상 (user, video)를 고른다.
    scout_dir = tmp_path / "scout"
    _run_round(scout_dir, stub_reranker, generator=RuleBasedActionLogGenerator())
    scout = _load_replay(scout_dir, with_exposure_keys=True)
    assert scout.exposure_keys is not None
    poison_user = next(iter(sorted(scout.exposure_keys)))
    poison_video = sorted(scout.exposure_keys[poison_user])[0]

    # 2) chunk_size=1로 판정 라운드 실행 — poison 청크만 quarantine된다.
    round_dir = tmp_path / "round"
    original = _run_round(
        round_dir,
        stub_reranker,
        generator=_FlakyGenerator(poison_user, poison_video),
        chunk_size=1,
    )
    assert original["quarantined_chunks"] >= 1
    replay = _load_replay(round_dir, with_exposure_keys=True)
    judged_keys = {(d.user_id, d.video_id) for d in replay.drafts}
    assert (poison_user, poison_video) not in judged_keys
    assert any(d.user_id == poison_user for d in replay.drafts), (
        "부분 격리 재현 실패 — 대상 유저의 다른 청크는 성공해야 합니다"
    )

    # 3) 같은 노출 인자로 리플레이 — 성공하고 격리분은 dropped로 계수된다.
    replayed = _run_round(
        tmp_path / "b",
        stub_reranker,
        generator=None,
        replay=replay,
        output_dir=str(tmp_path / "b"),
    )
    assert replayed["dropped_exposures_without_judgment"] > 0


def test_replay_event_log_keeps_original_llm_model(tmp_path, stub_reranker):
    """리플레이 event log의 계보는 원본 판정 모델이어야 한다."""
    import pyarrow.parquet as pq

    first_dir = tmp_path / "a"
    _run_round(
        first_dir,
        stub_reranker,
        generator=RuleBasedActionLogGenerator(model_name="judge-v9"),
    )

    second_dir = tmp_path / "b"
    _run_round(
        second_dir,
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        output_dir=str(second_dir),
    )

    table = pq.read_table(second_dir / "event_log.parquet").to_pandas()
    assert set(table["llm_model"].unique()) == {"judge-v9"}


def test_main_rejects_replay_with_mismatched_exposure_args(tmp_path, stub_reranker):
    """main()을 직접 호출해도 replay.exposure_args 불일치를 잡아야 한다(발견 사항 1).

    resolve_exposure_args는 _cli()에서만 검사하므로, main()을 직접 호출하는
    경로(테스트·후속 배치·노트북)는 이 검사가 없으면 노출 인자가 달라도 통과해
    CTR 분모가 왜곡된다. 리뷰어가 재현한 시나리오와 동일하게 k를 바꿔 리플레이한다.
    """
    first_dir = tmp_path / "a"
    _run_round(
        first_dir, stub_reranker, generator=RuleBasedActionLogGenerator(), k=6
    )

    with pytest.raises(ValueError, match="k"):
        _run_round(
            tmp_path / "b",
            stub_reranker,
            generator=None,
            replay=_load_replay(first_dir),
            k=3,  # 판정 라운드(k=6)와 다른 노출 인자
            output_dir=str(tmp_path / "b"),
        )


def test_main_requires_exactly_one_of_generator_or_replay(tmp_path, stub_reranker):
    with pytest.raises(ValueError, match="정확히 하나"):
        _run_round(tmp_path / "a", stub_reranker, generator=None)

    first_dir = tmp_path / "b"
    _run_round(first_dir, stub_reranker, generator=RuleBasedActionLogGenerator())
    with pytest.raises(ValueError, match="정확히 하나"):
        _run_round(
            tmp_path / "c",
            stub_reranker,
            generator=RuleBasedActionLogGenerator(),
            replay=_load_replay(first_dir),
            output_dir=str(tmp_path / "c"),
        )


def test_resolve_exposure_args_uses_defaults_without_meta():
    from autoresearch.recommendation.simulate_policy_round import resolve_exposure_args

    resolved = resolve_exposure_args(
        explicit={"seed": None, "k": 6, "exploration_ratio": None, "as_of": None},
        defaults={"seed": 42, "k": 10, "exploration_ratio": 0.1, "as_of": "now"},
        meta_exposure_args=None,
    )
    assert resolved == {"seed": 42, "k": 6, "exploration_ratio": 0.1, "as_of": "now"}


def test_resolve_exposure_args_inherits_meta_when_unspecified():
    from autoresearch.recommendation.simulate_policy_round import resolve_exposure_args

    meta = {"seed": 7, "k": 6, "exploration_ratio": 0.0, "as_of": "2026-07-20 00:00:00"}
    resolved = resolve_exposure_args(
        explicit={"seed": None, "k": None, "exploration_ratio": None, "as_of": None},
        defaults={"seed": 42, "k": 10, "exploration_ratio": 0.1, "as_of": "now"},
        meta_exposure_args=meta,
    )
    assert resolved == meta


def test_resolve_exposure_args_rejects_mismatch():
    from autoresearch.recommendation.simulate_policy_round import resolve_exposure_args

    meta = {"seed": 7, "k": 6, "exploration_ratio": 0.0, "as_of": "2026-07-20 00:00:00"}
    with pytest.raises(ValueError, match="seed"):
        resolve_exposure_args(
            explicit={"seed": 42, "k": None, "exploration_ratio": None, "as_of": None},
            defaults={"seed": 42, "k": 10, "exploration_ratio": 0.1, "as_of": "now"},
            meta_exposure_args=meta,
        )


def test_read_drafts_meta_requires_sidecar(tmp_path):
    from autoresearch.recommendation.simulate_policy_round import _read_drafts_meta

    with pytest.raises(FileNotFoundError, match="llm_model"):
        _read_drafts_meta(tmp_path / "action_log_drafts_meta.json")


def test_cli_replay_runs_without_generator(tmp_path, stub_reranker, monkeypatch):
    """CLI 리플레이는 --generator 없이 메타에서 인자를 상속해 동작해야 한다."""
    import json
    import sys

    import pyarrow as pa
    import pyarrow.parquet as pq

    from autoresearch.recommendation import simulate_policy_round as module

    # 입력 파일 준비
    personas_path = tmp_path / "personas.csv"
    _personas().to_csv(personas_path, index=False)
    videos_path = tmp_path / "videos.csv"
    _videos_raw().to_csv(videos_path, index=False)
    events_path = tmp_path / "events.csv"
    # 빈 프레임을 CSV로 왕복시키면 dtype이 전부 object로 추론돼 DuckDB의
    # user_id 비교가 깨진다. 실데이터와 같은 형태로 이력이 있는 프레임을 쓴다.
    _events_with_history().to_csv(events_path, index=False)
    users_path = tmp_path / "virtual_users.parquet"
    pq.write_table(pa.Table.from_pylist(_virtual_users()), users_path)

    monkeypatch.setattr(module, "load_reranker", lambda settings: stub_reranker)
    monkeypatch.setattr(module, "load_model_settings_from_environment", lambda: None)

    round_a = tmp_path / "round_a"
    argv = [
        "prog",
        "--personas", str(personas_path),
        "--virtual-users", str(users_path),
        "--videos", str(videos_path),
        "--events", str(events_path),
        "--generator", "rule-based",
        "--click-threshold", "0.0",
        "--k", "6",
        "--exploration-ratio", "0.0",
        "--as-of", "2026-07-20 00:00:00",
        "--output-dir", str(round_a),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    module._cli()

    meta = json.loads(
        (round_a / "action_log_drafts_meta.json").read_text(encoding="utf-8")
    )
    assert meta["exposure_args"]["k"] == 6

    # 리플레이 — k/seed/as-of/generator 모두 생략하고 메타에서 상속한다
    round_b = tmp_path / "round_b"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--personas", str(personas_path),
            "--virtual-users", str(users_path),
            "--videos", str(videos_path),
            "--events", str(events_path),
            "--replay-drafts", str(round_a / "action_log_drafts.parquet"),
            "--click-threshold", "0.0",
            "--output-dir", str(round_b),
        ],
    )
    module._cli()

    original = json.loads((round_a / "policy_round_report.json").read_text(encoding="utf-8"))
    replayed = json.loads((round_b / "policy_round_report.json").read_text(encoding="utf-8"))
    assert replayed["policies"] == original["policies"]


def test_cli_replay_rejects_generator_flag(tmp_path, monkeypatch):
    import sys

    from autoresearch.recommendation import simulate_policy_round as module

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--personas", "p.csv", "--virtual-users", "u.parquet",
            "--videos", "v.csv", "--events", "e.csv",
            "--replay-drafts", str(tmp_path / "action_log_drafts.parquet"),
            "--generator", "rule-based",
            "--click-threshold", "0.5",
        ],
    )
    with pytest.raises(SystemExit):
        module._cli()


def test_report_records_replay_provenance(tmp_path, stub_reranker):
    """산출물만 보고 원본 판정 라운드와 리플레이를 구분할 수 있어야 한다."""
    first_dir = tmp_path / "a"
    original = _run_round(
        first_dir,
        stub_reranker,
        generator=RuleBasedActionLogGenerator(model_name="judge-v9"),
    )
    assert original["replay"] is False
    assert original["llm_model"] == "judge-v9"

    second_dir = tmp_path / "b"
    replayed = _run_round(
        second_dir,
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        output_dir=str(second_dir),
    )
    assert replayed["replay"] is True
    assert replayed["llm_model"] == "judge-v9"

    html = (second_dir / "policy_round_report.html").read_text(encoding="utf-8")
    assert "judge-v9" in html
    assert "replay" in html


def test_replay_fails_when_judged_users_are_absent_from_exposures(tmp_path, stub_reranker):
    """유저 수는 같고 id만 다른 virtual users로 리플레이하면 실패해야 한다.

    전량 zero-coverage는 quarantine 관용 규칙에 걸려 조용히 통과하고
    impressions=0·CTR=0 리포트를 만들어낸다 — 정상 종료로 오인되는 실패다.
    """
    first_dir = tmp_path / "a"
    _run_round(first_dir, stub_reranker, generator=RuleBasedActionLogGenerator())

    other_personas = _personas(4).assign(uuid=[f"z{i}" for i in range(4)])
    other_users = [dict(user, user_id=f"z{i}") for i, user in enumerate(_virtual_users(4))]

    with pytest.raises(ValueError, match="판정이 있는 유저"):
        _run_round(
            tmp_path / "b",
            stub_reranker,
            generator=None,
            replay=_load_replay(first_dir),
            personas=other_personas,
            virtual_users=other_users,
            output_dir=str(tmp_path / "b"),
        )


def _fake_pool_frame(store, user_id, candidate_video_ids, as_of) -> pd.DataFrame:
    """build_pool_feature_frame_feast 대역 — pool 영상당 21피처 1행."""
    rows = []
    for video_id in candidate_video_ids:
        row = {c: 0 for c in MODEL_FEATURE_COLUMNS}
        row["category_id"] = "Gaming" if video_id[-1] in "0369" else "Music"
        row["video_id"] = video_id
        rows.append(row)
    return pd.DataFrame(rows)


def test_main_feast_source_routes_through_offline_pit(tmp_path, stub_reranker, monkeypatch):
    # assembly_source='feast'면 모델 피처를 offline PIT(build_pool_feature_frame_feast)로만
    # 만들고, raw 재계산(duckdb build_pool_feature_frame)은 절대 안 탄다(#359 A2).
    from autoresearch.recommendation import simulate_policy_round as module

    calls: list[tuple] = []

    def _fake_feast(store, user_id, candidate_video_ids, as_of):
        calls.append((store, user_id, tuple(candidate_video_ids), as_of))
        return _fake_pool_frame(store, user_id, candidate_video_ids, as_of)

    def _boom(*args, **kwargs):
        raise AssertionError("feast 모드에서 duckdb build_pool_feature_frame이 호출됨")

    monkeypatch.setattr(module, "build_pool_feature_frame_feast", _fake_feast)
    monkeypatch.setattr(module, "build_pool_feature_frame", _boom)

    sentinel_store = object()
    report = main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        policy_version="feast-run",
        output_dir=str(tmp_path),
        assembly_source="feast",
        feature_store=sentinel_store,
    )

    assert set(report["policies"]) == {"baseline", "model"}
    # 유저당 1회 조립, 주입 store가 그대로 전달되고 pool 전량(30)이 후보로 넘어간다.
    assert len(calls) == len(_virtual_users())
    assert calls[0][0] is sentinel_store
    assert len(calls[0][2]) == 30


def test_main_verifies_credentials_when_assembly_source_is_duckdb(
    tmp_path, stub_reranker, monkeypatch
):
    # duckdb 경로는 유저별 피처 조립에서 embed_texts를 실제로 호출하므로, 라운드
    # 시작 시 자격증명 사전점검이 1회 실행돼야 한다(#426).
    from autoresearch.recommendation import simulate_policy_round as module

    calls: list[str] = []
    monkeypatch.setattr(module, "verify_vertex_ai_credentials", lambda: calls.append("called"))

    _run_round(tmp_path, stub_reranker, generator=RuleBasedActionLogGenerator())

    assert calls == ["called"]


class _PreflightMarker(Exception):
    """자격증명 사전점검이 실행된 지점을 식별하기 위한 마커 예외."""


def test_main_credential_check_runs_before_simulation_stages(
    tmp_path, stub_reranker, monkeypatch
):
    # "언젠가 호출됐다"가 아니라 "비싼 단계보다 먼저 호출됐다"를 고정한다(#426 최종리뷰).
    # 사전점검이 뒤로 밀리면 피처 조립(_boom)이 먼저 터져 마커 예외가 안 나오고,
    # 5단계를 지나 밀리면 산출물이 이미 쓰인 뒤라 두 번째 단언도 깨진다.
    from autoresearch.recommendation import simulate_policy_round as module

    def _raise_marker():
        raise _PreflightMarker("preflight")

    def _boom(*args, **kwargs):
        raise AssertionError("자격증명 사전점검보다 피처 조립이 먼저 실행됨")

    monkeypatch.setattr(module, "verify_vertex_ai_credentials", _raise_marker)
    monkeypatch.setattr(module, "build_pool_feature_frame", _boom)

    with pytest.raises(_PreflightMarker):
        _run_round(tmp_path, stub_reranker, generator=RuleBasedActionLogGenerator())

    assert list(tmp_path.iterdir()) == []  # 어떤 산출물도 쓰이기 전에 멈췄다


def test_main_skips_credential_check_when_assembly_source_is_feast(
    tmp_path, stub_reranker, monkeypatch
):
    # feast 경로는 build_pool_feature_frame_feast가 BigQuery 사전계산값을 읽어
    # embed_texts를 호출하지 않으므로 사전점검이 불필요하다 — 실행되면 안 된다(#426).
    from autoresearch.recommendation import simulate_policy_round as module

    def _fail_if_called():
        raise AssertionError("assembly_source='feast'인데 자격증명 사전점검이 호출됨")

    monkeypatch.setattr(module, "verify_vertex_ai_credentials", _fail_if_called)
    monkeypatch.setattr(module, "build_pool_feature_frame_feast", _fake_pool_frame)

    report = main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        policy_version="feast-run",
        output_dir=str(tmp_path),
        assembly_source="feast",
        feature_store=object(),
    )

    assert set(report["policies"]) == {"baseline", "model"}


def test_main_feast_requires_feature_store(stub_reranker):
    with pytest.raises(ValueError, match="feature_store 주입이 필요"):
        main(
            personas=_personas(),
            virtual_users=_virtual_users(),
            videos_raw=_videos_raw(),
            events=_empty_events(),
            generator=RuleBasedActionLogGenerator(),
            reranker=stub_reranker,
            click_threshold=0.0,
            assembly_source="feast",
            feature_store=None,
        )


def test_cli_feast_requires_project_before_feast_import(monkeypatch):
    """프로젝트가 없으면 Feast import와 offline store 구성 전에 실패한다."""
    from autoresearch.model_training import build_training_dataset
    from autoresearch.recommendation import simulate_policy_round as module

    monkeypatch.setattr(build_training_dataset, "BIGQUERY_PROJECT", None)
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://test-bucket/registry.pb")
    monkeypatch.setenv("GCS_STAGING_LOCATION", "gs://test-bucket/staging")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "simulate_policy_round.py",
            "--personas",
            "personas.parquet",
            "--virtual-users",
            "virtual-users.parquet",
            "--videos",
            "videos.csv",
            "--events",
            "events.csv",
            "--click-threshold",
            "0.0",
            "--assembly-source",
            "feast",
        ],
    )
    original_import = builtins.__import__

    def _forbid_feast_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "autoresearch.feature_engineering.feast_retrieval":
            raise AssertionError("프로젝트 확인 전에 Feast를 import하면 안 됩니다")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _forbid_feast_import)

    with pytest.raises(ValueError, match="CTR_TRAINING_BQ_PROJECT"):
        module._cli()
