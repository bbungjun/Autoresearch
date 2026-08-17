"""feature_repo.redis_iam 어댑터 단위 테스트 (feast 그룹 환경 전용)."""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("feast")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from feature_repo import redis_iam  # noqa: E402


class _FakeCredentials:
    def __init__(self, *, token="token-1", valid=True, expiry=None):
        self.token = token
        self.valid = valid
        self.expiry = expiry
        self.refresh_calls = 0

    def refresh(self, request):
        self.refresh_calls += 1
        self.token = f"token-{self.refresh_calls + 1}"
        self.valid = True
        self.expiry = datetime.utcnow() + timedelta(hours=1)


def _provider(credentials):
    return redis_iam.GCPIAMCredentialProvider(
        credentials_factory=lambda: credentials
    )


def test_returns_cached_token_before_refresh_margin():
    credentials = _FakeCredentials(
        expiry=datetime.utcnow() + timedelta(hours=1)
    )
    provider = _provider(credentials)

    assert provider.get_credentials() == ("token-1",)
    assert credentials.refresh_calls == 0


def test_refreshes_token_near_expiry():
    credentials = _FakeCredentials(
        expiry=datetime.utcnow() + timedelta(minutes=1)
    )
    provider = _provider(credentials)

    assert provider.get_credentials() == ("token-2",)
    assert credentials.refresh_calls == 1


def test_refreshes_invalid_credentials():
    credentials = _FakeCredentials(valid=False)
    provider = _provider(credentials)

    assert provider.get_credentials() == ("token-2",)
    assert credentials.refresh_calls == 1


def test_empty_token_raises():
    credentials = _FakeCredentials(
        token="", expiry=datetime.utcnow() + timedelta(hours=1)
    )
    provider = _provider(credentials)

    with pytest.raises(RuntimeError):
        provider.get_credentials()


def _config(**overrides):
    values = {
        "type": "feature_repo.redis_iam.IAMRedisOnlineStore",
        "redis_type": "redis_cluster",
        "connection_string": "10.10.16.3:6379",
    }
    values.update(overrides)
    return redis_iam.IAMRedisOnlineStoreConfig(**values)


def test_config_drops_unexpanded_env_placeholder():
    config = _config(tls_ca_cert_path="${REDIS_TLS_CA_PATH}")

    assert config.tls_ca_cert_path is None


def test_get_client_injects_iam_and_tls_kwargs(monkeypatch, tmp_path):
    ca_path = tmp_path / "ca.pem"
    ca_path.write_text("dummy")
    captured = {}

    def _fake_cluster(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(kind="cluster")

    monkeypatch.setattr(redis_iam, "RedisCluster", _fake_cluster)
    store = redis_iam.IAMRedisOnlineStore()
    fake_provider = SimpleNamespace(get_credentials=lambda: ("t",))
    store._credential_provider = fake_provider

    client = store._get_client(_config(tls_ca_cert_path=str(ca_path)))

    assert client.kind == "cluster"
    assert captured["credential_provider"] is fake_provider
    assert captured["ssl"] is True
    assert captured["ssl_ca_certs"] == str(ca_path)
    assert [(n.host, str(n.port)) for n in captured["startup_nodes"]] == [
        ("10.10.16.3", "6379")
    ]


def test_get_client_missing_ca_file_raises(monkeypatch, tmp_path):
    store = redis_iam.IAMRedisOnlineStore()
    store._credential_provider = SimpleNamespace()

    with pytest.raises(FileNotFoundError):
        store._get_client(
            _config(tls_ca_cert_path=str(tmp_path / "missing.pem"))
        )


def test_get_client_iam_auth_false_uses_parent(monkeypatch):
    import feast.infra.online_stores.redis as feast_redis

    captured = {}

    def _fake_redis(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(kind="plain")

    monkeypatch.setattr(feast_redis, "Redis", _fake_redis)
    store = redis_iam.IAMRedisOnlineStore()

    client = store._get_client(
        _config(redis_type="redis", iam_auth=False)
    )

    assert client.kind == "plain"
    assert "credential_provider" not in captured


def test_get_client_async_not_supported():
    import asyncio

    store = redis_iam.IAMRedisOnlineStore()

    with pytest.raises(NotImplementedError):
        asyncio.run(store._get_client_async(_config()))


def test_generate_hset_keys_does_not_mutate_caller_list():
    """반복 호출해도 호출자 리스트와 조회 필드 수가 늘지 않는다 (#9).

    feast는 이 리스트를 online read 호출 간 재사용하므로, 상류처럼 in-place로
    append하면 HMGET이 가져오는 필드가 요청마다 하나씩 영구 누적되어 서빙 지연이
    누적 요청 수에 선형 비례해 증가한다.
    """
    store = redis_iam.IAMRedisOnlineStore()
    feature_view = SimpleNamespace(name="VideoFeatureView", features=[])
    requested = ["category_id", "view_count"]

    for _ in range(5):
        returned, hset_keys = store._generate_hset_keys_for_features(
            feature_view, requested, fv_name_override="VideoFeatureView"
        )
        # 요청 피처 2개 + 타임스탬프 키 1개로 매 호출 동일해야 한다.
        assert returned[-1] == "_ts:VideoFeatureView"
        assert len(returned) == 3
        assert len(hset_keys) == 3

    assert requested == ["category_id", "view_count"]


def test_generate_hset_keys_defaults_to_feature_view_features():
    """requested_features가 없으면 FeatureView 정의에서 채우는 상위 동작을 보존한다."""
    store = redis_iam.IAMRedisOnlineStore()
    feature_view = SimpleNamespace(
        name="UserStaticView",
        features=[SimpleNamespace(name="age_group"), SimpleNamespace(name="occupation")],
    )

    returned, hset_keys = store._generate_hset_keys_for_features(feature_view)

    assert returned == ["age_group", "occupation", "_ts:UserStaticView"]
    assert len(hset_keys) == 3


def test_upstream_still_mutates_requested_features():
    """상류 feast가 아직 호출자 리스트를 변형하는지 확인하는 tripwire (#9).

    이 테스트가 실패하면 상류가 버그를 고친 것이므로,
    IAMRedisOnlineStore._generate_hset_keys_for_features 오버라이드를 제거한다.
    """
    from feast.infra.online_stores.redis import RedisOnlineStore

    feature_view = SimpleNamespace(name="VideoFeatureView", features=[])
    requested = ["category_id"]

    RedisOnlineStore._generate_hset_keys_for_features(
        RedisOnlineStore(), feature_view, requested, fv_name_override="VideoFeatureView"
    )

    assert requested == ["category_id", "_ts:VideoFeatureView"]
