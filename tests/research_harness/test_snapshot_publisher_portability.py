import os
from pathlib import Path
import runpy

import pytest


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime required")
def test_concurrency_module_collects_on_actual_posix_runtime() -> None:
    # Given
    module_path = Path(__file__).with_name("test_snapshot_publisher_concurrency.py")

    # When
    namespace = runpy.run_path(
        str(module_path), run_name="snapshot_publisher_concurrency_collection"
    )

    # Then
    generic_test = namespace["test_two_concurrent_identical_publishers_cooperate"]
    assert callable(generic_test)
    assert getattr(generic_test, "pytestmark", ()) == ()

    windows_only_tests = (
        "test_windows_contention_beyond_crt_retry_limit_eventually_reuses",
        "test_non_contention_lock_failure_is_typed_sanitized_and_cleans_staging",
        "test_lock_release_failure_is_typed_sanitized_and_cleans_staging",
    )
    for test_name in windows_only_tests:
        windows_test = namespace[test_name]
        assert callable(windows_test)
        assert any(
            mark.name == "skipif" and mark.args == (True,)
            for mark in windows_test.pytestmark
        )
