"""Windows coding prepare에서 평가 정답을 제외한 candidate 입력에 읽기 ACE를 추가한다.

[파이프라인] 새 candidate worktree 게시 후 실제 Codex process 시작 직전 구간이다.
[기능] manifest·exact tree·파일 identity/digest를 대조하고 exclusive handle을 고정한
객체에만 local sandbox group의 비상속 READ ACE를 추가한다. 기존 owner/ACE와 부모
ACL을 대조하며 부분 실패를 정제된 receipt로 남긴다.
Windows가 기록하는 AUTO_INHERITED의 0→1만 구분해 허용하고 모든 control 값을
기록한다. 전체 적용 후 각 handle을 다시 대조하여 이후 변경도 확인한다.
[비책임] 공용 입력 게시, host prediction, final Judge, 상위 폴더 권한 변경, 기존
실패 workspace 회수 또는 전체 effective 권한을 read-only로 제한하는 정책은 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
from pathlib import Path
import re
import stat
import struct
import sys

from pydantic import ValidationError

from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.fixture_models import CandidateDataManifest, CandidateDataManifestV2
from autoresearch.research_harness.local_evaluation_fixture import _resolved_without_link


_MAX_OBJECTS = 128  # 30 history partitions, their directories and v2 metadata fit below 70.
_READ_MASK = 0x120089  # FILE_GENERIC_READ; no write/delete/execute/owner/DACL rights.
_GROUP = "CodexSandboxUsers"


@dataclass(frozen=True, slots=True)
class CandidateInputIdentity:
    """Trusted prepare가 전달한 validation 입력 identity. 경로·SID 입력은 받지 않는다."""

    manifest_sha256: str
    evaluation_id: str

    def __post_init__(self) -> None:
        if (not isinstance(self.manifest_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256) is None
                or not isinstance(self.evaluation_id, str) or re.fullmatch(r"eval_[0-9a-f]{64}", self.evaluation_id) is None):
            raise ValueError("invalid candidate input identity")


@dataclass(slots=True)
class InputAccessError(Exception):
    """원본 경로·SID를 노출하지 않는 실패와 부분 적용 증거."""

    stage: str
    receipt: dict = field(repr=False)

    def __str__(self) -> str:
        return f"candidate_input_access_failed: stage={self.stage}"


@dataclass(frozen=True, slots=True)
class _Security:
    owner: bytes
    group: bytes
    control: int
    revision: int
    aces: tuple[bytes, ...]


def _acl_aces(acl: bytes) -> tuple[int, tuple[bytes, ...]]:
    if len(acl) < 8:
        raise ValueError("acl_header")
    revision, reserved, size, count, reserved2 = struct.unpack("<BBHHH", acl[:8])
    if revision not in (2, 4) or reserved or reserved2 or size != len(acl):
        raise ValueError("acl_header")
    offset, aces = 8, []
    for _ in range(count):
        if offset + 4 > size:
            raise ValueError("acl_ace")
        length = struct.unpack_from("<H", acl, offset + 2)[0]
        if length < 4 or length % 4 or offset + length > size:
            raise ValueError("acl_ace")
        aces.append(acl[offset:offset + length])
        offset += length
    if any(acl[offset:]):
        raise ValueError("acl_padding")
    return revision, tuple(aces)


def _acl_bytes(revision: int, aces: tuple[bytes, ...]) -> bytes:
    size = 8 + sum(map(len, aces))
    if size > 65535:
        raise ValueError("acl_size")
    return struct.pack("<BBHHH", revision, 0, size, len(aces), 0) + b"".join(aces)


def _read_acl(security: _Security, sid: bytes) -> bytes:
    ace = struct.pack("<BBHI", 0, 0, 8 + len(sid), _READ_MASK) + sid
    # Explicit allow precedes inherited ACEs; all existing ACE bytes/order stay intact.
    index = next((i for i, old in enumerate(security.aces) if old[1] & 0x10), len(security.aces))
    return _acl_bytes(security.revision, security.aces[:index] + (ace,) + security.aces[index:])


def _security_matches(before: _Security, after: _Security, expected_acl: bytes) -> bool:
    # SetSecurityInfo may set SE_DACL_AUTO_INHERITED. Only this 0->1 transition is
    # allowed; protection/owner/group/other control flags and all old ACEs remain exact.
    return (after.control in {before.control, before.control | 0x400}
            and after == replace(before, control=after.control, aces=_acl_aces(expected_acl)[1]))


def _path_identity(path: Path, *, directory: bool) -> tuple[int, int]:
    info = path.lstat()
    if (not _resolved_without_link(path) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
            or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
        raise ValueError("input_alias")
    return info.st_dev, info.st_ino


def _targets(cwd: Path, identity: CandidateInputIdentity) -> tuple[list[Path], dict[Path, str], dict[Path, tuple[int, int]]]:
    root = cwd / "harness_in"
    if (not isinstance(identity, CandidateInputIdentity)
            or re.fullmatch(r"[0-9a-f]{64}", identity.manifest_sha256) is None
            or not cwd.is_absolute() or not _resolved_without_link(cwd) or not _resolved_without_link(root)):
        raise ValueError("input_identity")
    manifest_path = root / "candidate-view.json"
    info = manifest_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536 or not _resolved_without_link(manifest_path):
        raise ValueError("manifest_file")
    payload = manifest_path.read_bytes()
    if sha256(payload).hexdigest() != identity.manifest_sha256:
        raise ValueError("manifest_digest")
    model = CandidateDataManifestV2 if json.loads(payload).get("contract_version") == "candidate-data-view-v2" else CandidateDataManifest
    manifest = model.model_validate_json(payload)
    if (str(manifest.evaluation_id) != identity.evaluation_id
            or canonical_json_bytes(manifest.model_dump(mode="json")) != payload):
        raise ValueError("manifest_identity")
    receipts = (manifest.slate, *manifest.history_partitions)
    if isinstance(manifest, CandidateDataManifestV2):
        receipts += (manifest.user_metadata, manifest.video_metadata)
    digests = {root / receipt.relative_path: receipt.sha256 for receipt in receipts}
    digests[manifest_path] = identity.manifest_sha256
    directories = {root, root / "history", root / "history/action_log"}
    for path in digests:
        directories.update(parent for parent in path.parents if parent.is_relative_to(root))
    expected = directories | set(digests)
    if len(expected) > _MAX_OBJECTS:
        raise ValueError("input_object_limit")
    actual, pending = {root}, [root]
    identities = {cwd: _path_identity(cwd, directory=True), root: _path_identity(root, directory=True)}
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            actual.add(path)
            if len(actual) > _MAX_OBJECTS or path not in expected:
                raise ValueError("input_tree")
            identities[path] = _path_identity(path, directory=path in directories)
            if path in directories:
                pending.append(path)  # Never descend into an unchecked symlink/reparse entry.
    if actual != expected:
        raise ValueError("input_tree")
    paths = [cwd, *sorted(expected, key=lambda path: (len(path.parts), str(path)))]
    if len(set(identities.values())) != len(paths):
        raise ValueError("input_alias")
    return paths, digests, identities


def grant_input_read(cwd: Path, identity: CandidateInputIdentity) -> dict:
    """새 입력만 검증·고정·READ 추가한다. 실제 호출은 Windows Codex adapter만 한다."""
    receipt = {"version": "candidate-input-access-v1", "status": "failed", "object_count": 0,
               "applied_count": 0, "objects": [], "principal": None,
               "manifest_sha256": None, "added_access_mask": _READ_MASK,
               "limitations": "Only added rights are READ; pre-existing effective rights are not reduced."}
    handles: list[tuple[Path, int]] = []
    api = None
    interrupted = False
    stage = "input_validation"
    try:
        paths, digests, identities = _targets(cwd, identity)
        receipt["manifest_sha256"] = identity.manifest_sha256
        receipt["object_count"] = len(paths) - 1
        stage = "principal_lookup"
        api = _WindowsAclApi()
        sid = api.local_principal()
        receipt["principal"] = {"name": _GROUP, "sid_sha256": sha256(sid).hexdigest()}
        stage = "handle_identity"
        for path in paths:
            handle = api.open(path, writable=path != cwd, directory=path not in digests)
            handles.append((path, handle))
            if api.identity(handle) != identities[path]:
                raise ValueError("input_handle_identity")
        stage = "input_digest"
        for path, handle in handles:
            if path in digests and api.digest(handle) != digests[path]:
                raise ValueError("input_digest")
        stage = "acl_snapshot"
        before = {path: api.security(handle) for path, handle in handles}
        planned = {path: _read_acl(before[path], sid) for path, _ in handles if path != cwd}
        stage = "acl_apply"
        for path, handle in handles[1:]:
            acl = planned[path]
            item = {"path": path.relative_to(cwd).as_posix(), "status": "pending",
                    "control_before": before[path].control, "control_after": None,
                    "owner_before_sha256": sha256(before[path].owner).hexdigest(), "owner_after_sha256": None,
                    "dacl_before_sha256": sha256(_acl_bytes(before[path].revision, before[path].aces)).hexdigest(),
                    "dacl_after_sha256": None}
            receipt["objects"].append(item)
            api.set_dacl(handle, acl)
            receipt["applied_count"] += 1
            item["status"] = "applied_unverified"
            after = api.security(handle)
            item["control_after"] = after.control
            item["owner_after_sha256"] = sha256(after.owner).hexdigest()
            item["dacl_after_sha256"] = sha256(_acl_bytes(after.revision, after.aces)).hexdigest()
            if not _security_matches(before[path], after, acl):
                raise ValueError("acl_readback")
            item["status"] = "applied"
        stage = "acl_final_readback"
        for (path, handle), item in zip(handles[1:], receipt["objects"], strict=True):
            after = api.security(handle)
            item.update(control_after=after.control, owner_after_sha256=sha256(after.owner).hexdigest(),
                        dacl_after_sha256=sha256(_acl_bytes(after.revision, after.aces)).hexdigest())
            if not _security_matches(before[path], after, planned[path]):
                item["status"] = "applied_unverified"
                raise ValueError("acl_final_readback")
        stage = "parent_readback"
        if api.security(handles[0][1]) != before[cwd]:
            raise ValueError("parent_acl_changed")
        receipt["status"] = "complete"
    except (OSError, ValueError, TypeError, AttributeError, ValidationError, StageCError):
        receipt["failure_stage"] = stage
        raise InputAccessError(stage, receipt) from None
    except (KeyboardInterrupt, SystemExit) as interruption:
        interrupted = True
        receipt.update(status="failed", failure_stage=stage, interrupted=True)
        interruption.input_access_receipt = receipt
        raise
    finally:
        if api is not None:
            close_failed = False
            for _, handle in reversed(handles):
                try:
                    api.close(handle)
                except OSError:
                    close_failed = True
            if close_failed:
                receipt.update(status="failed", failure_stage="handle_close")
                if not interrupted:
                    raise InputAccessError("handle_close", receipt) from None
    return receipt


class _WindowsAclApi:
    """Windows handle 호출만 감추는 내부 seam; 테스트에서는 객체 전체를 fake로 바꾼다."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as w

        self.c, self.w = ctypes, w
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = ctypes.c_void_p
        declarations = (
            (self.k, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, pointer, w.DWORD, w.DWORD, pointer], pointer),
            (self.k, "CloseHandle", [pointer], w.BOOL),
            (self.k, "GetFileInformationByHandle", [pointer, pointer], w.BOOL),
            (self.k, "GetFileInformationByHandleEx", [pointer, ctypes.c_int, pointer, w.DWORD], w.BOOL),
            (self.k, "ReadFile", [pointer, pointer, w.DWORD, pointer, pointer], w.BOOL),
            (self.k, "GetComputerNameW", [w.LPWSTR, pointer], w.BOOL),
            (self.k, "LocalFree", [pointer], pointer),
            (self.a, "LookupAccountNameW", [w.LPCWSTR, w.LPCWSTR, pointer, pointer, w.LPWSTR, pointer, pointer], w.BOOL),
            (self.a, "IsValidSid", [pointer], w.BOOL),
            (self.a, "GetLengthSid", [pointer], w.DWORD),
            (self.a, "GetSecurityInfo", [pointer, ctypes.c_int, w.DWORD, pointer, pointer, pointer, pointer, pointer], w.DWORD),
            (self.a, "GetSecurityDescriptorControl", [pointer, pointer, pointer], w.BOOL),
            (self.a, "IsValidAcl", [pointer], w.BOOL),
            (self.a, "SetSecurityInfo", [pointer, ctypes.c_int, w.DWORD, pointer, pointer, pointer, pointer], w.DWORD),
        )
        for library, name, args, result in declarations:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result

    def _require(self, success: object) -> None:
        if not success:
            raise OSError(self.c.get_last_error(), "windows_input_access")

    def local_principal(self) -> bytes:
        c, w = self.c, self.w
        computer, length = c.create_unicode_buffer(256), w.DWORD(256)
        self._require(self.k.GetComputerNameW(computer, c.byref(length)))
        name = computer.value + "\\" + _GROUP
        sid_size, domain_size, kind = w.DWORD(), w.DWORD(), w.DWORD()
        self.a.LookupAccountNameW(None, name, None, c.byref(sid_size), None, c.byref(domain_size), c.byref(kind))
        if c.get_last_error() != 122 or not 0 < sid_size.value <= 1024 or not 0 < domain_size.value <= 256:
            raise ValueError("principal_lookup")
        sid, domain = c.create_string_buffer(sid_size.value), c.create_unicode_buffer(domain_size.value)
        self._require(self.a.LookupAccountNameW(None, name, sid, c.byref(sid_size), domain, c.byref(domain_size), c.byref(kind)))
        if kind.value != 4 or domain.value.casefold() != computer.value.casefold() or not self.a.IsValidSid(sid):
            raise ValueError("principal_not_local_alias")
        return c.string_at(sid, self.a.GetLengthSid(sid))

    def open(self, path: Path, *, writable: bool, directory: bool) -> int:
        # share=0 pins identity and suppresses SetSecurityInfo child ACE propagation.
        # https://learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo
        access = 0x20000 | (0x40000 if writable else 0) | (0 if directory else 0x80000000)
        handle = self.k.CreateFileW(str(path), access, 0, None, 3, 0x00200000 | (0x02000000 if directory else 0), None)
        if handle in (None, self.c.c_void_p(-1).value):
            raise OSError(self.c.get_last_error(), "input_handle_open")
        return handle

    def identity(self, handle: int) -> tuple[int, int]:
        # BY_HANDLE_FILE_INFORMATION contains thirteen DWORDs, including three FILETIMEs.
        info = (self.c.c_uint32 * 13)()
        self._require(self.k.GetFileInformationByHandle(handle, self.c.byref(info)))
        if info[0] & 0x400 or (not info[0] & 0x10 and info[10] != 1):
            raise ValueError("input_handle_alias")
        if sys.version_info < (3, 12):
            return info[7], (info[11] << 32) | info[12]
        # CPython 3.12 uses 64-bit st_dev and 128-bit st_ino. FILE_ID_INFO is
        # ULONGLONG VolumeSerialNumber followed by FILE_ID_128 (Windows little-endian).
        # Do not truncate a 3.12 identity to the older BY_HANDLE fields.
        identity = (self.c.c_uint64 * 3)()
        self._require(self.k.GetFileInformationByHandleEx(handle, 18, self.c.byref(identity), self.c.sizeof(identity)))
        return identity[0], identity[1] | (identity[2] << 64)

    def digest(self, handle: int) -> str:
        digest, buffer, count = sha256(), self.c.create_string_buffer(1024 * 1024), self.w.DWORD()
        while True:
            self._require(self.k.ReadFile(handle, buffer, len(buffer), self.c.byref(count), None))
            if not count.value:
                return digest.hexdigest()
            digest.update(buffer.raw[:count.value])

    def security(self, handle: int) -> _Security:
        c = self.c
        owner, group, dacl, descriptor = (c.c_void_p() for _ in range(4))
        code = self.a.GetSecurityInfo(handle, 1, 0x1 | 0x2 | 0x4, c.byref(owner), c.byref(group), c.byref(dacl), None, c.byref(descriptor))
        if code:
            raise OSError(code, "input_security_read")
        try:
            control, revision = self.w.WORD(), self.w.DWORD()
            self._require(self.a.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision)))
            if not control.value & 4 or not dacl or not owner or not group or not self.a.IsValidAcl(dacl):
                raise ValueError("input_missing_dacl")
            size = struct.unpack_from("<H", c.string_at(dacl, 8), 2)[0]
            acl_revision, aces = _acl_aces(c.string_at(dacl, size))
            return _Security(c.string_at(owner, self.a.GetLengthSid(owner)), c.string_at(group, self.a.GetLengthSid(group)),
                             control.value, acl_revision, aces)
        finally:
            self.k.LocalFree(descriptor)

    def set_dacl(self, handle: int, acl: bytes) -> None:
        buffer = self.c.create_string_buffer(acl)
        self._require(self.a.IsValidAcl(buffer))
        code = self.a.SetSecurityInfo(handle, 1, 4, None, None, buffer, None)
        if code:
            raise OSError(code, "input_acl_write")

    def close(self, handle: int) -> None:
        self._require(self.k.CloseHandle(handle))
