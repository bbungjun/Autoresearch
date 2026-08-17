import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError

import autoresearch.action_log_generation.llm_generator as llm_module
from autoresearch.action_log_generation.llm_generator import (
    OpenRouterActionLogGenerator,
    OpenRouterRequestError,
    build_action_log_prompt,
)
from autoresearch.action_log_generation.observability import action_log_work_log_context


def _user():
    return {
        "user_id": "vu_test",
        "primary_categories": ["Gaming"],
        "interest_keywords": ["게임"],
    }


def _videos():
    return [
        {
            "video_id": "video_test",
            "title": "테스트 영상",
            "description": "설명",
            "tags": ["게임"],
        }
    ]


def test_prompt_explicitly_lists_all_24_candidate_rows_and_indexes():
    videos = [
        {
            "video_id": f"video_{index}",
            "title": f"테스트 영상 {index}",
            "description": "설명",
            "tags": ["게임"],
        }
        for index in range(24)
    ]

    prompt = build_action_log_prompt(_user(), videos)

    expected_candidates = json.dumps(
        [[index, f"테스트 영상 {index}", ["게임"], "", "설명"] for index in range(24)],
        ensure_ascii=False,
    )
    expected_skeleton = json.dumps(
        {"j": [[index, 0.0, 0.0] for index in range(24)]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    required_indexes = json.dumps(list(range(24)), separators=(",", ":"))

    assert "Prompt version: action_log_ctr_v4" in prompt
    assert "컬럼 순서 = [index, title, tags, channel, description]" in prompt
    assert expected_candidates in prompt
    assert f"required_indexes={required_indexes}" in prompt
    assert "expected_count=24" in prompt
    assert expected_skeleton in prompt
    assert "..." not in prompt


def test_schema_retry_prompt_names_failure_without_reusing_raw_response(monkeypatch):
    client = _FakeClient([_success('{"j":[[0,0.2,0.3]]}')])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(api_key="test-api-key")

    result = generator.generate_schema_retry(
        _user(),
        _videos(),
        error_type="invalid_json",
    )

    prompt = client.completions.calls[0]["messages"][1]["content"]
    assert result == '{"j":[[0,0.2,0.3]]}'
    assert "이전 응답은 invalid_json 검증에 실패했다" in prompt
    assert '{"j":[[0,0.0,0.0]]}' in prompt


def _success(content: str = '{"judgments": []}'):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _timeout_error() -> APITimeoutError:
    return APITimeoutError(
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    )


class _StatusError(Exception):
    def __init__(self, status_code: int, headers: dict[str, str] | None = None):
        super().__init__(f"unsafe upstream detail for {status_code}")
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})


class _FakeCompletions:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else _success()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, outcomes=None):
        self.completions = _FakeCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    def close(self):
        self.closed = True


def test_openrouter_reuses_thread_local_clients_and_closes_lifecycle(monkeypatch):
    clients = []
    client_kwargs = []

    def _factory(**kwargs):
        client = _FakeClient()
        clients.append(client)
        client_kwargs.append(kwargs)
        return client

    monkeypatch.setattr("openai.OpenAI", _factory)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        timeout_seconds=12.5,
    )

    generator.generate(_user(), _videos())
    generator.generate(_user(), _videos())
    barrier = Barrier(3)

    def _generate_in_worker():
        barrier.wait()
        return generator.generate(_user(), _videos())

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_generate_in_worker) for _ in range(2)]
        barrier.wait()
        [future.result() for future in futures]

    assert len(clients) == 3
    assert len(clients[0].completions.calls) == 2
    assert sum(len(client.completions.calls) for client in clients) == 4
    assert all(kwargs["timeout"] == 12.5 for kwargs in client_kwargs)
    assert all(kwargs["max_retries"] == 0 for kwargs in client_kwargs)

    generator.close()
    generator.close()
    assert all(client.closed for client in clients)
    with pytest.raises(RuntimeError, match="is closed"):
        generator.generate(_user(), _videos())


def test_openrouter_retries_only_allowlisted_status_with_retry_after_and_backoff(
    monkeypatch,
):
    client = _FakeClient(
        [
            _StatusError(
                429,
                {"retry-after": "2", "x-openrouter-provider": "provider-a"},
            ),
            _StatusError(503, {"x-openrouter-provider": "provider-b"}),
            _success(),
        ]
    )
    sleeps = []
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(llm_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.25)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=2,
        retry_backoff_base_seconds=1.0,
        retry_backoff_max_seconds=10.0,
    )

    assert generator.generate(_user(), _videos()) == '{"judgments": []}'
    assert len(client.completions.calls) == 3
    assert sleeps == [2.25, 2.25]


@pytest.mark.parametrize("status", [400, 401, 402, 403])
def test_openrouter_does_not_retry_non_retryable_client_errors(monkeypatch, status):
    client = _FakeClient(
        [_StatusError(status, {"x-openrouter-provider": "provider-a"})]
    )
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=3,
    )

    with pytest.raises(OpenRouterRequestError) as exc_info:
        generator.generate(_user(), _videos())

    assert len(client.completions.calls) == 1
    assert exc_info.value.status == status
    assert exc_info.value.provider == "provider-a"
    assert exc_info.value.attempts == 1
    assert "unsafe upstream detail" not in str(exc_info.value)


def test_openrouter_retry_exhaustion_is_structured(monkeypatch):
    client = _FakeClient([_StatusError(504), _StatusError(504), _StatusError(504)])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.0)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=2,
        retry_backoff_base_seconds=0.0,
        retry_backoff_max_seconds=0.0,
    )

    with pytest.raises(OpenRouterRequestError) as exc_info:
        generator.generate(_user(), _videos())

    assert exc_info.value.log_fields == {
        "status": 504,
        "error_type": "_StatusError",
        "provider": "unknown",
        "attempts": 3,
    }
    assert len(client.completions.calls) == 3


def test_openrouter_retries_api_timeout_with_separate_limit(monkeypatch):
    client = _FakeClient([_timeout_error(), _success()])
    sleeps = []
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(llm_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.0)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=1,
        timeout_max_retries=1,
        retry_backoff_base_seconds=0.0,
        retry_backoff_max_seconds=0.0,
    )

    assert generator.generate(_user(), _videos()) == '{"judgments": []}'
    assert len(client.completions.calls) == 2
    assert sleeps == [0.0]


def test_openrouter_timeout_retry_limit_is_independent_from_total_limit(monkeypatch):
    client = _FakeClient([_timeout_error(), _timeout_error(), _success()])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.0)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=3,
        timeout_max_retries=1,
        retry_backoff_base_seconds=0.0,
        retry_backoff_max_seconds=0.0,
    )

    with pytest.raises(OpenRouterRequestError) as exc_info:
        generator.generate(_user(), _videos())

    assert exc_info.value.status is None
    assert exc_info.value.error_type == "APITimeoutError"
    assert exc_info.value.attempts == 2
    assert len(client.completions.calls) == 2


def test_openrouter_retry_total_is_capped_across_timeout_and_http_errors(monkeypatch):
    client = _FakeClient(
        [_timeout_error(), _StatusError(503), _StatusError(503), _success()]
    )
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.0)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=2,
        timeout_max_retries=1,
        retry_backoff_base_seconds=0.0,
        retry_backoff_max_seconds=0.0,
    )

    with pytest.raises(OpenRouterRequestError) as exc_info:
        generator.generate(_user(), _videos())

    assert exc_info.value.status == 503
    assert exc_info.value.attempts == 3
    assert len(client.completions.calls) == 3


def test_openrouter_provider_preferences_are_optional_and_do_not_change_model(
    monkeypatch,
):
    default_client = _FakeClient()
    configured_client = _FakeClient()
    clients = iter([default_client, configured_client])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: next(clients))

    default = OpenRouterActionLogGenerator(api_key="test-api-key")
    configured = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        provider_sort="throughput",
        allow_fallbacks=False,
        require_parameters=True,
    )
    default.generate(_user(), _videos())
    configured.generate(_user(), _videos())

    default_request = default_client.completions.calls[0]
    configured_request = configured_client.completions.calls[0]
    assert "extra_body" not in default_request
    assert configured_request["extra_body"] == {
        "provider": {
            "sort": "throughput",
            "allow_fallbacks": False,
            "require_parameters": True,
        }
    }
    assert default_request["model"] == configured_request["model"]
    assert ":nitro" not in configured_request["model"]


def test_openrouter_returns_invalid_json_without_retry(monkeypatch):
    client = _FakeClient([_success("{not valid json")])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=3,
    )

    assert generator.generate(_user(), _videos()) == "{not valid json"
    assert len(client.completions.calls) == 1


def test_openrouter_structured_logs_include_attempt_usage_without_sensitive_data(
    monkeypatch,
    caplog,
):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"judgments": []}'))],
        provider="provider-safe",
        usage=SimpleNamespace(
            prompt_tokens=120,
            completion_tokens=30,
            cost=0.001,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=4),
        ),
    )
    client = _FakeClient([response])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(api_key="test-api-key")

    with caplog.at_level(
        logging.INFO,
        logger="autoresearch.action_log_generation.llm_generator",
    ):
        with action_log_work_log_context(
            shard_index=2,
            work_sequence=7,
            detailed=True,
        ):
            generator.generate(_user(), _videos())

    events = [json.loads(record.message) for record in caplog.records]
    attempt = next(
        event for event in events if event["event"] == "openrouter_attempt_complete"
    )
    request = next(
        event for event in events if event["event"] == "openrouter_request_complete"
    )
    assert attempt["shard_index"] == request["shard_index"] == 2
    assert attempt["work_sequence"] == request["work_sequence"] == 7
    assert attempt["attempt"] == 1
    assert attempt["http_status"] == 200
    assert attempt["provider"] == "provider-safe"
    assert attempt["attempt_elapsed_ms"] >= 0
    assert request["request_elapsed_ms"] >= attempt["attempt_elapsed_ms"]
    assert request["retry_count"] == 0
    assert request["prompt_tokens"] == 120
    assert request["completion_tokens"] == 30
    assert request["reasoning_tokens"] == 4
    assert request["reported_cost"] == 0.001

    serialized = json.dumps(events, ensure_ascii=False)
    assert "test-api-key" not in serialized
    assert "vu_test" not in serialized
    assert "테스트 영상" not in serialized
    assert "judgments" not in serialized


def test_openrouter_retry_log_separates_attempt_and_backoff(monkeypatch, caplog):
    client = _FakeClient(
        [
            _StatusError(429, {"x-openrouter-provider": "provider-a"}),
            _success(),
        ]
    )
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    event_order = []
    original_emit = llm_module.emit_action_log_event

    def _record_event(*args, **kwargs):
        event_order.append(args[2])
        return original_emit(*args, **kwargs)

    def _record_sleep(seconds):
        event_order.append("sleep")

    monkeypatch.setattr(llm_module, "emit_action_log_event", _record_event)
    monkeypatch.setattr(llm_module.time, "sleep", _record_sleep)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.0)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=1,
        retry_backoff_base_seconds=1.0,
        retry_backoff_max_seconds=1.0,
    )

    with caplog.at_level(
        logging.INFO,
        logger="autoresearch.action_log_generation.llm_generator",
    ):
        with action_log_work_log_context(
            shard_index=0,
            work_sequence=0,
            detailed=True,
        ):
            generator.generate(_user(), _videos())

    events = [json.loads(record.message) for record in caplog.records]
    attempts = [
        event for event in events if event["event"] == "openrouter_attempt_complete"
    ]
    retry_scheduled = next(
        event for event in events if event["event"] == "openrouter_retry_scheduled"
    )
    request = next(
        event for event in events if event["event"] == "openrouter_request_complete"
    )
    assert len(attempts) == 2
    assert event_order.index("openrouter_retry_scheduled") < event_order.index("sleep")
    assert retry_scheduled["attempt"] == 1
    assert retry_scheduled["retry_count"] == 1
    assert retry_scheduled["backoff_seconds"] == 1.0
    assert retry_scheduled["http_status"] == 429
    assert retry_scheduled["provider"] == "provider-a"
    assert retry_scheduled["request_elapsed_ms"] >= 0
    assert attempts[0]["outcome"] == "retry"
    assert attempts[0]["http_status"] == 429
    assert attempts[0]["provider"] == "provider-a"
    assert attempts[0]["backoff_scheduled_ms"] == 1000.0
    assert attempts[0]["backoff_elapsed_ms"] >= 0
    assert attempts[1]["outcome"] == "success"
    assert request["retry_count"] == 1
    assert request["attempt"] == 2
    serialized = json.dumps(events, ensure_ascii=False)
    assert "test-api-key" not in serialized
    assert "vu_test" not in serialized
    assert "테스트 영상" not in serialized


def test_openrouter_success_detail_logs_are_suppressed_for_large_runs(
    monkeypatch,
    caplog,
):
    client = _FakeClient([_success()])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(api_key="test-api-key")

    with caplog.at_level(
        logging.INFO,
        logger="autoresearch.action_log_generation.llm_generator",
    ):
        with action_log_work_log_context(
            shard_index=0,
            work_sequence=0,
            detailed=False,
        ):
            generator.generate(_user(), _videos())

    assert caplog.records == []


def _empty_choices(code=502, provider="TestProvider", message="upstream detail"):
    """choices 없이 error 본문만 담아 200으로 돌아온 OpenRouter 응답."""

    return SimpleNamespace(
        choices=None,
        model_extra={
            "error": {
                "code": code,
                "message": message,
                "metadata": {"provider_name": provider},
            }
        },
    )


def test_response_without_choices_raises_error_carrying_provider_code(monkeypatch):
    client = _FakeClient([_empty_choices(code=502, provider="TestProvider")])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(api_key="test-api-key")

    with pytest.raises(OpenRouterRequestError) as excinfo:
        generator.generate(_user(), _videos())

    error = excinfo.value
    assert error.status == 502
    assert error.error_type == "empty_choices"
    assert error.provider == "TestProvider"


def test_empty_choices_error_does_not_leak_upstream_message(monkeypatch):
    client = _FakeClient([_empty_choices(message="unsafe upstream detail")])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(api_key="test-api-key")

    with pytest.raises(OpenRouterRequestError) as excinfo:
        generator.generate(_user(), _videos())

    assert "unsafe upstream detail" not in str(excinfo.value)


def test_empty_choices_list_without_error_payload_still_raises(monkeypatch):
    client = _FakeClient([SimpleNamespace(choices=[], provider="FallbackProvider")])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(api_key="test-api-key")

    with pytest.raises(OpenRouterRequestError) as excinfo:
        generator.generate(_user(), _videos())

    error = excinfo.value
    assert error.status is None
    assert error.error_type == "empty_choices"
    assert error.provider == "FallbackProvider"
