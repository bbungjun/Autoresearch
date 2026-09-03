"""실제 ACL 변경 없이 검증된 coding 입력의 READ 추가 계약을 검증한다."""

from dataclasses import replace
from hashlib import sha256
import importlib
import os
from pathlib import Path
import struct

import pytest


def module():
    return importlib.import_module("autoresearch.research_harness._windows_sandbox_inputs")


@pytest.fixture
def inputs(tmp_path: Path):
    root = tmp_path / "harness_in"
    root.mkdir()
    (root / "history/action_log/dt=2026-08-01").mkdir(parents=True)
    files = {"slate.parquet": b"slate", "history/action_log/dt=2026-08-01/part-0.parquet": b"history"}
    for name, payload in files.items():
        (root / name).write_bytes(payload)
    manifest = {"contract_version": "candidate-data-view-v1", "evaluation_id": "eval_" + "a" * 64,
                "evaluation_start_date": "2026-08-03", "complete_history_label_end_date": "2026-08-01",
                "slate": {"relative_path": "slate.parquet", "rows": 1, "sha256": sha256(b"slate").hexdigest()},
                "history_partitions": [{"relative_path": "history/action_log/dt=2026-08-01/part-0.parquet",
                                        "dt": "2026-08-01", "rows": 1, "sha256": sha256(b"history").hexdigest()}]}
    from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
    payload = canonical_json_bytes(manifest)
    (root / "candidate-view.json").write_bytes(payload)
    return tmp_path, payload, manifest


class FakeNative:
    def __init__(self, m, *, failure=None):
        self.m, self.failure = m, failure
        self.opened, self.closed, self.written = [], [], []
        self.states = {}

    def local_principal(self):
        if self.failure == "principal":
            raise OSError("unverified principal")
        return b"SID1"

    def open(self, path, *, writable, directory):
        if self.failure == "open" and path.name == "slate.parquet":
            raise OSError("open failed")
        self.opened.append((path, writable, directory))
        self.states[path] = self.m._Security(b"owner", b"group", 0x8004, 2, (struct.pack("<BBHI4s", 0, 0, 12, 0x1F01FF, b"OLD1"),))
        return path

    def identity(self, handle):
        info = handle.stat()
        return (info.st_dev, info.st_ino + (1 if self.failure == "identity" and handle.name == "slate.parquet" else 0))

    def digest(self, handle):
        return sha256(handle.read_bytes()).hexdigest()

    def security(self, handle):
        if self.failure == "missing_dacl":
            raise ValueError("missing dacl")
        value = self.states[handle]
        return replace(value, owner=b"changed") if self.failure == "readback" and handle in self.written else value

    def set_dacl(self, handle, acl):
        if self.failure in ("keyboard", "exit") and len(self.written) == 1:
            raise KeyboardInterrupt if self.failure == "keyboard" else SystemExit
        if self.failure == "write" and len(self.written) == 1:
            raise OSError("write failed")
        self.written.append(handle)
        self.states[handle] = replace(self.states[handle], aces=self.m._acl_aces(acl)[1])
        if self.failure in {"auto_inherited", "protected_removed", "auto_removed"}:
            control = self.states[handle].control
            control = control | 0x400 if self.failure == "auto_inherited" else control & ~0x1000 if self.failure == "protected_removed" else control & ~0x400
            self.states[handle] = replace(self.states[handle], control=control)
        if self.failure == "late_change" and len(self.written) == 2:
            first = self.written[0]
            self.states[first] = replace(self.states[first], group=b"changed-later")

    def close(self, handle):
        self.closed.append(handle)
        if self.failure == "close" and len(self.closed) == 1:
            raise OSError("close failed")


def grant(inputs, monkeypatch, *, failure=None):
    cwd, payload, manifest = inputs
    m = module()
    native = FakeNative(m, failure=failure)
    monkeypatch.setattr(m, "_WindowsAclApi", lambda: native)
    identity = m.CandidateInputIdentity(sha256(payload).hexdigest(), manifest["evaluation_id"])
    return m, native, identity


def test_grant_only_exact_verified_input_objects_and_preserve_owner_aces(inputs, monkeypatch):
    m, native, identity = grant(inputs, monkeypatch)
    result = m.grant_input_read(inputs[0], identity)
    assert result["status"] == "complete" and result["applied_count"] == result["object_count"]
    assert all(path.is_relative_to(inputs[0] / "harness_in") for path in native.written)
    assert native.closed == [item[0] for item in reversed(native.opened)]
    assert result["principal"] == {"name": "CodexSandboxUsers", "sid_sha256": sha256(b"SID1").hexdigest()}
    for item in result["objects"]:
        assert item["owner_before_sha256"] == item["owner_after_sha256"]
        assert item["status"] == "applied"


@pytest.mark.parametrize("failure", ["principal", "open", "identity", "write", "readback", "missing_dacl", "close"])
def test_failure_preserves_partial_evidence_and_closes_all_handles(inputs, monkeypatch, failure):
    m, native, identity = grant(inputs, monkeypatch, failure=failure)
    with pytest.raises(m.InputAccessError) as caught:
        m.grant_input_read(inputs[0], identity)
    assert caught.value.receipt["status"] == "failed"
    assert caught.value.receipt["applied_count"] == len(native.written)
    assert native.closed == [item[0] for item in reversed(native.opened)]
    assert str(inputs[0]) not in str(caught.value)


@pytest.mark.parametrize("failure,kind", [("keyboard", KeyboardInterrupt), ("exit", SystemExit)])
def test_partial_acl_interruption_retains_receipt_and_original_exception(inputs, monkeypatch, failure, kind):
    m, native, identity = grant(inputs, monkeypatch, failure=failure)
    with pytest.raises(kind) as caught:
        m.grant_input_read(inputs[0], identity)
    assert caught.value.input_access_receipt["applied_count"] == 1
    assert caught.value.input_access_receipt["interrupted"] is True
    assert native.closed == [item[0] for item in reversed(native.opened)]


def test_wrong_identity_type_is_typed_failure_without_native_calls(inputs, monkeypatch):
    m, native, _ = grant(inputs, monkeypatch)
    with pytest.raises(m.InputAccessError):
        m.grant_input_read(inputs[0], object())
    assert native.opened == []


def test_object_bound_rejects_before_opening_handles(inputs, monkeypatch):
    m, native, identity = grant(inputs, monkeypatch)
    monkeypatch.setattr(m, "_MAX_OBJECTS", 2)
    with pytest.raises(m.InputAccessError):
        m.grant_input_read(inputs[0], identity)
    assert native.opened == []


def test_untrusted_directory_is_rejected_before_descending(inputs, monkeypatch):
    m, native, identity = grant(inputs, monkeypatch)
    original = m._resolved_without_link
    monkeypatch.setattr(m, "_resolved_without_link", lambda path: False if path.name == "history" else original(path))
    original_iterdir = Path.iterdir
    def guarded_iterdir(path):
        assert path.name != "history", "must not descend through rejected alias"
        return original_iterdir(path)
    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    with pytest.raises(m.InputAccessError):
        m.grant_input_read(inputs[0], identity)
    assert native.opened == []


def test_native_open_requests_exclusive_handle_and_only_necessary_host_rights(tmp_path):
    import ctypes
    from types import SimpleNamespace
    m = module()
    api = object.__new__(m._WindowsAclApi)
    calls = []
    api.c = ctypes
    api.k = SimpleNamespace(CreateFileW=lambda *args: calls.append(args) or 123)
    api.open(tmp_path / "input", writable=True, directory=False)
    api.open(tmp_path, writable=False, directory=True)
    assert calls[0][1:3] == (0x80060000, 0)
    assert calls[1][1:3] == (0x20000, 0)
    assert calls[0][5] == 0x00200000 and calls[1][5] == 0x02200000


@pytest.mark.parametrize("python_version", [(3, 11), (3, 12)])
def test_native_identity_matches_python_generation_without_truncation(monkeypatch, python_version):
    import ctypes
    from types import SimpleNamespace
    m = module()
    monkeypatch.setattr(m, "sys", SimpleNamespace(version_info=python_version), raising=False)
    api = object.__new__(m._WindowsAclApi)
    api.c, api.w = ctypes, SimpleNamespace(DWORD=ctypes.c_uint32)
    volume, low, high = 0xD617CCD516263569, 0x1122334455667788, 0x99AABBCCDDEEFF00
    calls = []
    def legacy(handle, buffer):
        assert ctypes.sizeof(buffer._obj) == 52
        buffer._obj[7], buffer._obj[10] = 0x16263569, 1
        buffer._obj[11], buffer._obj[12] = low >> 32, low & 0xffffffff
        return 1
    def extended(handle, information_class, buffer, size):
        assert information_class == 18 and size == ctypes.sizeof(buffer._obj) == 24
        ctypes.memmove(buffer, struct.pack("<QQQ", volume, low, high), size)
        calls.append(information_class)
        return 1
    api.k = SimpleNamespace(GetFileInformationByHandle=legacy, GetFileInformationByHandleEx=extended)
    expected = (0x16263569, low) if python_version < (3, 12) else (volume, low | high << 64)
    assert api.identity(123) == expected
    assert calls == ([] if python_version < (3, 12) else [18])


def test_only_auto_inherited_addition_is_accepted_and_recorded(inputs, monkeypatch):
    m, native, identity = grant(inputs, monkeypatch, failure="auto_inherited")
    receipt = m.grant_input_read(inputs[0], identity)
    assert receipt["status"] == "complete"
    assert all(item["control_after"] == item["control_before"] | 0x400 for item in receipt["objects"])


@pytest.mark.parametrize("failure", ["protected_removed", "auto_removed", "late_change"])
def test_other_control_changes_and_late_object_drift_fail_closed(inputs, monkeypatch, failure):
    m, native, identity = grant(inputs, monkeypatch, failure=failure)
    original = native.open
    def opened(path, **kwargs):
        handle = original(path, **kwargs)
        native.states[handle] = replace(native.states[handle], control=0x9404)
        return handle
    monkeypatch.setattr(native, "open", opened)
    with pytest.raises(m.InputAccessError):
        m.grant_input_read(inputs[0], identity)


@pytest.mark.parametrize("changed_bit", [0x1000, 0x0100, 0x0800, 0x2000, 0x0001, 0x0002])
def test_normalization_does_not_accept_other_security_control_bits(changed_bit):
    m = module()
    before = m._Security(b"owner", b"group", 0x9004, 2, ())
    acl = m._read_acl(before, b"SID1")
    after = replace(before, control=before.control ^ changed_bit, aces=m._acl_aces(acl)[1])
    assert not m._security_matches(before, after, acl)


@pytest.mark.parametrize("mutation", ["extra", "digest", "manifest", "identity", "hardlink"])
def test_invalid_tree_fails_before_any_acl_write(inputs, monkeypatch, mutation):
    m, native, identity = grant(inputs, monkeypatch)
    root = inputs[0] / "harness_in"
    if mutation == "extra":
        (root / "secret.txt").write_text("unexpected")
    elif mutation == "digest":
        (root / "slate.parquet").write_bytes(b"changed")
    elif mutation == "manifest":
        (root / "candidate-view.json").write_text("{}")
    elif mutation == "identity":
        identity = replace(identity, evaluation_id="eval_" + "b" * 64)
    else:
        os.link(root / "slate.parquet", inputs[0] / "alias.parquet")
    with pytest.raises(m.InputAccessError):
        m.grant_input_read(inputs[0], identity)
    assert native.written == []


def test_new_ace_is_read_only_noninherited_and_old_order_is_preserved():
    m = module()
    first = struct.pack("<BBHI4s", 1, 0, 12, 2, b"OLD1")
    second = struct.pack("<BBHI4s", 0, 16, 12, 1, b"OLD2")
    security = m._Security(b"owner", b"group", 0x8004, 2, (first, second))
    result = m._read_acl(security, b"SID1")
    _, aces = m._acl_aces(result)
    assert aces[0] == first and aces[2] == second
    assert struct.unpack("<BBHI", aces[1][:8]) == (0, 0, 12, 0x120089)
    assert aces[1][8:] == b"SID1"
