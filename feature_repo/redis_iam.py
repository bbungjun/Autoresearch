"""Memorystore Redis Cluster IAM 인증·TLS Feast online store 어댑터.

[파이프라인] 피처 구간 — 학습·서빙이 Feast online store(Redis)를 읽고 쓰기
직전, Memorystore Redis Cluster 접속에 필요한 IAM 토큰 인증과 TLS를 주입하는
구간을 담당한다.

[기능] 만료 여백을 두고 GCP IAM 액세스 토큰을 갱신하는 credential provider,
redis-py 클라이언트(standalone·cluster) 생성 시의 인증·TLS kwargs 주입, 그리고
상류 feast 0.64.0의 online read 필드 목록 누적 버그(#9) 우회를 담당한다.

[비책임] FeatureStore 생성과 CA 번들 조달(feature_repo/bootstrap.py),
Entity·FeatureView 정의(feature_repo/feature_definitions.py), 서빙의 온라인
피처 조회·조립 계약(applications/reranking_api/online_features.py).
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Literal, Optional, Tuple

import google.auth
import google.auth.transport.requests
from feast import FeatureView
from feast.infra.online_stores.redis import (
    RedisOnlineStore,
    RedisOnlineStoreConfig,
    RedisType,
)
from pydantic import StrictStr, field_validator
from redis import Redis
from redis.cluster import ClusterNode, RedisCluster
from redis.credentials import CredentialProvider

_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _default_credentials() -> Any:
    credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
    return credentials


class GCPIAMCredentialProvider(CredentialProvider):
    """연결 시점마다 유효한 IAM access token을 Redis AUTH 자격으로 제공한다."""

    def __init__(
        self, credentials_factory: Callable[[], Any] | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._credentials = (credentials_factory or _default_credentials)()

    def get_credentials(self) -> Tuple[str]:
        with self._lock:
            if self._needs_refresh():
                request = google.auth.transport.requests.Request()
                self._credentials.refresh(request)
            token: Optional[str] = self._credentials.token
        if not token:
            raise RuntimeError("IAM access token could not be issued")
        return (token,)

    def _needs_refresh(self) -> bool:
        if not self._credentials.valid:
            return True
        expiry = self._credentials.expiry
        if expiry is None:
            return False
        # google-auth의 expiry는 naive UTC이므로 같은 표현으로 비교한다.
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        return expiry - now_utc < _TOKEN_REFRESH_MARGIN


class IAMRedisOnlineStoreConfig(RedisOnlineStoreConfig):
    """IAM 인증·TLS Redis Cluster용 online store 설정."""

    type: Literal["feature_repo.redis_iam.IAMRedisOnlineStore"] = (
        "feature_repo.redis_iam.IAMRedisOnlineStore"
    )

    iam_auth: bool = True

    tls_ca_cert_path: Optional[StrictStr] = None

    @field_validator("tls_ca_cert_path", mode="before")
    @classmethod
    def _drop_unexpanded_env(cls, value: object) -> object:
        if isinstance(value, str) and value.startswith("${"):
            return None
        return value


class IAMRedisOnlineStore(RedisOnlineStore):
    """IAM token 인증과 TLS를 주입하는 RedisOnlineStore 확장."""

    _credential_provider: Optional[GCPIAMCredentialProvider] = None

    def _generate_hset_keys_for_features(
        self,
        feature_view: FeatureView,
        requested_features: Optional[list[str]] = None,
        fv_name_override: Optional[str] = None,
    ) -> Tuple[list[str], list[str]]:
        """호출자의 리스트를 변형하지 않도록 복사본을 상위 구현에 넘긴다.

        feast 0.64.0의 ``RedisOnlineStore._generate_hset_keys_for_features``는
        전달받은 ``requested_features``에 ``_ts:<fv_name>``을 append 한다. feast는
        그 리스트를 online read 호출 간 재사용하므로 항목이 요청마다 하나씩 영구
        누적되고, HMGET이 가져오는 필드 수가 조회 엔티티 수만큼 곱해져 online read
        지연이 누적 요청 수에 선형 비례해 무제한 증가한다(#9). 프로세스를 재시작해야만
        회복되므로 장수명 서빙에서 특히 문제가 된다.

        상위 구현은 반환한 리스트를 호출부가 다시 바인딩해 쓰므로(``online_read``),
        복사본을 넘겨도 조회 동작은 동일하다. 상류 feast가 이 in-place 변형을 없애면
        이 오버라이드를 제거한다.
        """
        return super()._generate_hset_keys_for_features(
            feature_view,
            list(requested_features) if requested_features else None,
            fv_name_override=fv_name_override,
        )

    def _iam_kwargs(self, config: IAMRedisOnlineStoreConfig) -> dict[str, Any]:
        if self._credential_provider is None:
            self._credential_provider = GCPIAMCredentialProvider()
        kwargs: dict[str, Any] = {
            "credential_provider": self._credential_provider,
            "ssl": True,
        }
        if config.tls_ca_cert_path:
            if not os.path.exists(config.tls_ca_cert_path):
                raise FileNotFoundError(
                    f"Redis TLS CA bundle not found: {config.tls_ca_cert_path}"
                )
            kwargs["ssl_ca_certs"] = config.tls_ca_cert_path
        return kwargs

    def _get_client(self, online_store_config: IAMRedisOnlineStoreConfig):
        if not online_store_config.iam_auth:
            return super()._get_client(online_store_config)
        if not self._client:
            startup_nodes, kwargs = self._parse_connection_string(
                online_store_config.connection_string
            )
            kwargs.update(self._iam_kwargs(online_store_config))
            if online_store_config.redis_type == RedisType.redis_cluster:
                kwargs["startup_nodes"] = [
                    ClusterNode(**node) for node in startup_nodes
                ]
                self._client = RedisCluster(**kwargs)
            else:
                kwargs["host"] = startup_nodes[0]["host"]
                kwargs["port"] = startup_nodes[0]["port"]
                self._client = Redis(**kwargs)
        return self._client

    async def _get_client_async(
        self, online_store_config: IAMRedisOnlineStoreConfig
    ):
        raise NotImplementedError(
            "IAMRedisOnlineStore does not support the async client yet"
        )
