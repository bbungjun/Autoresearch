"""Research Harness의 disposable candidate 작업공간 경계.

[파이프라인] 고정된 champion SHA와 label-free ``CandidateDataView``를 받아 coding agent와
LocalRunner가 사용할 일회성 Git worktree를 준비하고, trial 종료 뒤 회수한다.

[기능] 정확한 기준 SHA checkout, label-free 입력·예측 출력 경로, credential-free
최소 프로세스 환경, 변경 내용 credential 검사와 ledger용 diff fingerprint를 제공한다.
Prepared metadata를 명시적으로 주입하면 validation v2를 게시하고 전체 view digest를 전달한다.
별도 final interface는 소비 grant 검증 뒤에만 final v2를 게시하며 같은 회수 구현을 쓴다.

[비책임] candidate subprocess 실행·seed 전달·timeout 회수, final holdout 소비 권한 발급,
Judge 채점·ledger 기록, 기존 executor verifier 정책은 담당하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum, unique
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Protocol

from applications.experiment_platform.executor.safety import contains_credential_value
from autoresearch.research_harness.consumption_registry import FinalConsumptionGrant
from autoresearch.research_harness.candidate_data_view import (
    materialize_candidate_data_view,
    materialize_candidate_data_view_v2,
    materialize_final_candidate_data_view,
)
from autoresearch.research_harness.evaluation_source import ActionLogSource
from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.fixture_models import (
    CandidateDataViewRequest,
    JudgeSnapshotHandoff,
    PreparedCandidateMetadata,
)


_SHA_LENGTH = 40
_HARNESS_PATHS = frozenset({".harness-in.lock", "harness_in", "harness_out"})
_INHERITED_ENVIRONMENT = (
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
)
_ADDITIONAL_CREDENTIAL_PATTERNS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?im)^\s*AWS_SECRET_ACCESS_KEY\s*=\s*"
        r"(?:['\"]?[A-Za-z0-9/+=]{40}['\"]?)\s*$"
    ),
)


class _Digest(Protocol):
    def update(self, payload: bytes) -> object: ...


@dataclass(frozen=True, slots=True)
class _WorktreeIdentity:
    root: tuple[int, int]
    git_file: tuple[int, int]
    git_file_sha256: str


@unique
class WorkspaceErrorCode(StrEnum):
    """Controller가 재시도 여부를 판단할 수 있는 workspace 실패 코드."""

    INVALID_REQUEST = "workspace_invalid_request"
    WORKSPACE_CONFLICT = "workspace_conflict"
    GIT_FAILED = "workspace_git_failed"
    DATA_VIEW_INVALID = "workspace_data_view_invalid"
    CREDENTIAL_DETECTED = "workspace_credential_detected"
    CLEANUP_FAILED = "workspace_cleanup_failed"


@dataclass(slots=True)
class WorkspaceError(Exception):
    """로컬 경로나 credential 값을 노출하지 않는 workspace 오류."""

    code: WorkspaceErrorCode
    stage: str

    def __str__(self) -> str:
        return f"{self.code.value}: stage={self.stage}"


@dataclass(frozen=True, slots=True)
class CandidateWorkspaceRequest:
    """하나의 label-free trial worktree를 만드는 Harness 소유 입력."""

    repository_root: Path
    base_sha: str
    workspace_root: Path
    judge: JudgeSnapshotHandoff


@dataclass(frozen=True, slots=True)
class CandidateProcessContext:
    """LocalRunner가 candidate 프로세스에 공개할 수 있는 최소 경로와 환경."""

    cwd: Path
    slate: Path
    predictions: Path
    environment: tuple[tuple[str, str], ...]

    def environment_dict(self) -> dict[str, str]:
        """Return a fresh subprocess environment mapping."""

        return dict(self.environment)


@dataclass(frozen=True, slots=True)
class CandidateChangeReceipt:
    """시크릿 검사를 통과한 candidate diff의 ledger 전달용 증거."""

    diff_fingerprint: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateWorkspace:
    """활성 context 안에서만 유효한 candidate workspace handle."""

    root: Path
    base_sha: str
    evaluation_id: str
    candidate_view_sha256: str
    process: CandidateProcessContext

    def inspect_changes(self) -> CandidateChangeReceipt:
        """변경 파일의 credential을 검사하고 전체 diff fingerprint를 반환한다."""

        return _inspect_changes(self.root, self.base_sha)


@contextmanager
def open_candidate_workspace(
    request: CandidateWorkspaceRequest,
    *,
    source: ActionLogSource,
    metadata: PreparedCandidateMetadata | None = None,
) -> Iterator[CandidateWorkspace]:
    """정확한 SHA의 validation worktree를 열고 모든 종료 경로에서 회수한다.

    metadata를 생략하면 v1, 준비한 bundle을 주면 v2 입력을 게시한다. Bundle은
    process context에 전달하지 않으며 기존 candidate_view_sha256이 전체 입력을 식별한다.
    """

    with _open_candidate_workspace(request, source=source, metadata=metadata, grant=None) as workspace:
        yield workspace


@contextmanager
def open_final_candidate_workspace(
    request: CandidateWorkspaceRequest,
    *,
    source: ActionLogSource,
    metadata: PreparedCandidateMetadata,
    grant: FinalConsumptionGrant,
) -> Iterator[CandidateWorkspace]:
    """유효한 final 소비 권한으로만 v2 worktree를 열고 종료 시 회수한다.

    Args:
        request: 고정 SHA, Judge handoff와 독립 workspace root.
        source: 같은 fixture의 검증된 history source.
        metadata: Judge 측에서 미리 준비한 final 전용 불변 metadata.
        grant: 같은 snapshot과 현재 marker에 결속된 소비 권한.

    Yields:
        grant나 Judge 경로를 포함하지 않는 candidate process context의 workspace.

    Raises:
        WorkspaceError: 권한·입력·checkout·회수 검증이 실패한 경우.
    """
    if not isinstance(grant, FinalConsumptionGrant) or not grant._authorizes(request.judge):
        raise WorkspaceError(WorkspaceErrorCode.DATA_VIEW_INVALID, "final_grant")
    with _open_candidate_workspace(request, source=source, metadata=metadata, grant=grant) as workspace:
        yield workspace


@contextmanager
def _open_candidate_workspace(
    request: CandidateWorkspaceRequest,
    *,
    source: ActionLogSource,
    metadata: PreparedCandidateMetadata | None,
    grant: FinalConsumptionGrant | None,
) -> Iterator[CandidateWorkspace]:

    repository, workspace = _validate_request(request)
    created = False
    identity: _WorktreeIdentity | None = None
    try:
        _run_git(
            repository,
            "worktree",
            "add",
            "--detach",
            str(workspace),
            request.base_sha,
        )
        created = True
        identity = _worktree_identity(workspace)
        head = _run_git(workspace, "rev-parse", "HEAD").decode("ascii").strip()
        branch = _run_git(workspace, "branch", "--show-current").decode("utf-8").strip()
        if head != request.base_sha or branch:
            raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "checkout_identity")
        try:
            view_request = CandidateDataViewRequest(request.judge, workspace)
            if grant is not None:
                if metadata is None:
                    raise WorkspaceError(WorkspaceErrorCode.DATA_VIEW_INVALID, "final_metadata_required")
                data_view = materialize_final_candidate_data_view(
                    view_request, source=source, metadata=metadata, grant=grant,
                )
            elif metadata is None:
                data_view = materialize_candidate_data_view(view_request, source=source)
            else:
                data_view = materialize_candidate_data_view_v2(
                    view_request, source=source, metadata=metadata,
                )
        except StageCError:
            raise WorkspaceError(
                WorkspaceErrorCode.DATA_VIEW_INVALID,
                "candidate_data_view",
            ) from None
        output = workspace / "harness_out"
        try:
            output.mkdir()
        except OSError:
            raise WorkspaceError(
                WorkspaceErrorCode.WORKSPACE_CONFLICT,
                "output_directory",
            ) from None
        yield CandidateWorkspace(
            root=workspace,
            base_sha=request.base_sha,
            evaluation_id=str(data_view.manifest.evaluation_id),
            candidate_view_sha256=data_view.manifest_sha256,
            process=CandidateProcessContext(
                cwd=workspace,
                slate=data_view.root / "slate.parquet",
                predictions=output / "predictions.csv",
                environment=_candidate_environment(os.environ),
            ),
        )
    finally:
        if created:
            _remove_worktree(repository, workspace, identity)


def _validate_request(request: CandidateWorkspaceRequest) -> tuple[Path, Path]:
    if (
        not isinstance(request.base_sha, str)
        or len(request.base_sha) != _SHA_LENGTH
        or any(character not in "0123456789abcdef" for character in request.base_sha)
        or not request.repository_root.is_absolute()
        or not request.workspace_root.is_absolute()
    ):
        raise WorkspaceError(WorkspaceErrorCode.INVALID_REQUEST, "request_validation")
    try:
        repository = request.repository_root.resolve(strict=True)
        workspace_parent = request.workspace_root.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkspaceError(WorkspaceErrorCode.INVALID_REQUEST, "path_validation") from None
    workspace = workspace_parent / request.workspace_root.name
    if (
        not repository.is_dir()
        or workspace.exists()
        or workspace.is_symlink()
        or workspace == repository
        or repository.is_relative_to(workspace)
        or workspace.is_relative_to(repository)
    ):
        raise WorkspaceError(WorkspaceErrorCode.WORKSPACE_CONFLICT, "workspace_validation")
    resolved = _run_git(
        repository,
        "rev-parse",
        "--verify",
        f"{request.base_sha}^{{commit}}",
        allow_failure=True,
    )
    if resolved.decode("ascii").strip() != request.base_sha:
        raise WorkspaceError(WorkspaceErrorCode.INVALID_REQUEST, "base_sha_validation")
    return repository, workspace


def _candidate_environment(host: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    environment = {
        name: host[name]
        for name in _INHERITED_ENVIRONMENT
        if name in host and host[name]
    }
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return tuple(sorted(environment.items()))


def _inspect_changes(root: Path, base_sha: str) -> CandidateChangeReceipt:
    changed_paths, indexed_paths = _changed_path_groups(root, base_sha)
    _require_credential_free(root, base_sha, changed_paths, indexed_paths)

    digest = sha256()
    _update_digest(digest, b"base-sha", base_sha.encode("ascii"))
    for relative_path in changed_paths:
        index_records = _index_records(root, relative_path)
        _update_digest(
            digest,
            f"index:{relative_path}".encode("utf-8"),
            index_records,
        )
        path = root.joinpath(*relative_path.split("/"))
        mode, payload = _worktree_entry(
            path,
            index_records,
            submodule_base_ref=_base_gitlink_oid(root, base_sha, relative_path),
        )
        _update_digest(
            digest,
            f"worktree:{mode}:{relative_path}".encode("utf-8"),
            payload,
        )
    return CandidateChangeReceipt(
        diff_fingerprint=f"sha256:{digest.hexdigest()}",
        changed_paths=changed_paths,
    )


def _changed_path_groups(
    root: Path,
    base_ref: str,
    *,
    exclude_harness_paths: bool = True,
) -> tuple[tuple[str, ...], frozenset[str]]:
    common = (
        "-c",
        "core.filemode=true",
        "-c",
        "diff.renames=false",
        "diff",
        "--no-renames",
        "--ignore-submodules=none",
        "--no-ext-diff",
        "--no-textconv",
        "--name-only",
        "--diff-filter=ACDMRTUXB",
        "-z",
    )
    indexed = _nul_paths(_run_git(root, *common, "--cached", base_ref))
    worktree = _nul_paths(_run_git(root, *common))
    untracked = _nul_paths(_run_git(root, "ls-files", "--others", "-z"))
    changed_paths = tuple(
        sorted(
            path
            for path in set((*indexed, *worktree, *untracked))
            if not exclude_harness_paths or not _is_harness_path(path)
        )
    )
    return changed_paths, frozenset(
        path
        for path in indexed
        if not exclude_harness_paths or not _is_harness_path(path)
    )


def _require_credential_free(
    root: Path,
    base_ref: str,
    changed_paths: tuple[str, ...],
    indexed_paths: frozenset[str],
) -> None:
    for relative_path in changed_paths:
        index_records = _index_records(root, relative_path)
        if relative_path in indexed_paths:
            for payload in _index_blob_payloads(root, index_records):
                _require_payload_credential_free(payload)
        path = root.joinpath(*relative_path.split("/"))
        if _index_mode(index_records) == "160000":
            base_oid = _base_gitlink_oid(root, base_ref, relative_path)
            staged_oid = _index_gitlink_oid(index_records)
            if not path.is_dir():
                if staged_oid is not None and staged_oid != base_oid:
                    raise WorkspaceError(
                        WorkspaceErrorCode.GIT_FAILED,
                        "submodule_unavailable",
                    )
                continue
            _canonical_submodule_state(path, base_oid, staged_oid)
            continue
        if not path.exists() and not path.is_symlink():
            continue
        _require_payload_credential_free(_current_payload(path))


def _require_payload_credential_free(payload: bytes) -> None:
    text = payload.decode("utf-8", errors="replace")
    if contains_credential_value(text) or any(
        pattern.search(text) is not None
        for pattern in _ADDITIONAL_CREDENTIAL_PATTERNS
    ):
        raise WorkspaceError(
            WorkspaceErrorCode.CREDENTIAL_DETECTED,
            "candidate_change_scan",
        )


def _index_records(root: Path, relative_path: str) -> bytes:
    return _run_git(
        root,
        "ls-files",
        "--stage",
        "-z",
        "--",
        f":(literal){relative_path}",
    )


def _index_blob_payloads(root: Path, records: bytes) -> tuple[bytes, ...]:
    payloads: list[bytes] = []
    for record in records.split(b"\0"):
        if not record:
            continue
        try:
            header, _path = record.split(b"\t", maxsplit=1)
            mode, object_id, _stage = header.split(b" ", maxsplit=2)
        except ValueError:
            raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "index_parse") from None
        if mode == b"160000":
            continue
        payloads.append(_run_git(root, "cat-file", "blob", object_id.decode("ascii")))
    return tuple(payloads)


def _index_mode(records: bytes) -> str | None:
    for record in records.split(b"\0"):
        if not record:
            continue
        try:
            header, _path = record.split(b"\t", maxsplit=1)
            mode, _object_id, stage = header.split(b" ", maxsplit=2)
        except ValueError:
            raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "index_parse") from None
        if stage == b"0":
            return mode.decode("ascii")
    return None


def _index_gitlink_oid(records: bytes) -> str | None:
    for record in records.split(b"\0"):
        if not record:
            continue
        try:
            header, _path = record.split(b"\t", maxsplit=1)
            mode, object_id, stage = header.split(b" ", maxsplit=2)
        except ValueError:
            raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "index_parse") from None
        if stage != b"0" or mode != b"160000":
            continue
        try:
            candidate = object_id.decode("ascii")
        except UnicodeDecodeError:
            raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "index_parse") from None
        if len(candidate) != _SHA_LENGTH or any(
            character not in "0123456789abcdef" for character in candidate
        ):
            raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "index_parse")
        return candidate
    return None


def _base_gitlink_oid(root: Path, base_ref: str, relative_path: str) -> str | None:
    record = _run_git(
        root,
        "ls-tree",
        "-z",
        base_ref,
        "--",
        f":(literal){relative_path}",
    ).rstrip(b"\0")
    if not record:
        return None
    try:
        header, _path = record.split(b"\t", maxsplit=1)
        mode, object_type, object_id = header.split(b" ", maxsplit=2)
    except ValueError:
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "base_tree_parse") from None
    if mode != b"160000" or object_type != b"commit":
        return None
    try:
        return object_id.decode("ascii")
    except UnicodeDecodeError:
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "base_tree_parse") from None


def _worktree_entry(
    path: Path,
    index_records: bytes,
    *,
    submodule_base_ref: str | None,
) -> tuple[str, bytes]:
    if not path.exists() and not path.is_symlink():
        if _index_mode(index_records) == "160000":
            return "160000-unavailable", b""
        return "deleted", b""
    if path.is_dir() and _index_mode(index_records) == "160000":
        return "160000", _canonical_submodule_state(
            path,
            submodule_base_ref,
            _index_gitlink_oid(index_records),
        )
    return _current_mode(path), _current_payload(path)


def _canonical_submodule_state(
    root: Path,
    base_ref: str | None,
    expected_head: str | None,
) -> bytes:
    top_level = _run_git_result(root, "rev-parse", "--show-toplevel")
    try:
        is_exact_repository = (
            top_level.returncode == 0
            and Path(top_level.stdout.decode("utf-8").strip()).resolve()
            == root.resolve()
        )
    except (OSError, RuntimeError, UnicodeError):
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "submodule_root") from None
    if not is_exact_repository:
        if expected_head is not None and expected_head != base_ref:
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_FAILED,
                "submodule_unavailable",
            )
        return b"uninitialized"
    head_result = _run_git_result(root, "rev-parse", "--verify", "HEAD^{commit}")
    if head_result.returncode != 0:
        return b"uninitialized"
    head = head_result.stdout.strip()
    try:
        head_ref = head.decode("ascii")
    except UnicodeDecodeError:
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "submodule_head") from None
    if expected_head is None or head_ref != expected_head:
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "submodule_head_mismatch")
    digest = sha256()
    _update_digest(digest, b"submodule-head", head)
    if base_ref is None:
        indexed_paths = frozenset(
            _nul_paths(_run_git(root, "ls-files", "-z"))
        )
        worktree_paths = _nul_paths(
            _run_git(
                root,
                "-c",
                "core.filemode=true",
                "diff",
                "--ignore-submodules=none",
                "--no-ext-diff",
                "--no-textconv",
                "--name-only",
                "-z",
            )
        )
        untracked_paths = _nul_paths(
            _run_git(root, "ls-files", "--others", "-z")
        )
        changed_paths = tuple(
            sorted(set((*indexed_paths, *worktree_paths, *untracked_paths)))
        )
    else:
        changed_paths, indexed_paths = _changed_path_groups(
            root,
            base_ref,
            exclude_harness_paths=False,
        )
    for relative_path in changed_paths:
        records = _index_records(root, relative_path)
        if relative_path in indexed_paths:
            for payload in _index_blob_payloads(root, records):
                _require_payload_credential_free(payload)
        _update_digest(
            digest,
            f"submodule-index:{relative_path}".encode("utf-8"),
            records,
        )
        path = root.joinpath(*relative_path.split("/"))
        mode, payload = _worktree_entry(
            path,
            records,
            submodule_base_ref=(
                _base_gitlink_oid(root, base_ref, relative_path)
                if base_ref is not None
                else None
            ),
        )
        if mode != "160000":
            _require_payload_credential_free(payload)
        _update_digest(
            digest,
            f"submodule-worktree:{mode}:{relative_path}".encode("utf-8"),
            payload,
        )
    return digest.digest()


def _current_payload(path: Path) -> bytes:
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            return os.readlink(path).encode("utf-8")
        if stat.S_ISREG(mode):
            return path.read_bytes()
        return b""
    except (OSError, RuntimeError, UnicodeError):
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "change_read") from None


def _current_mode(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "change_mode") from None
    if stat.S_ISLNK(mode):
        return "120000"
    if stat.S_ISREG(mode):
        return "100755" if mode & stat.S_IXUSR else "100644"
    return f"special:{stat.S_IFMT(mode):o}"


def _update_digest(digest: _Digest, name: bytes, payload: bytes) -> None:
    digest.update(len(name).to_bytes(8, "big"))
    digest.update(name)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _nul_paths(payload: bytes) -> tuple[str, ...]:
    try:
        return tuple(
            item.decode("utf-8")
            for item in payload.split(b"\0")
            if item
        )
    except UnicodeDecodeError:
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "path_decode") from None


def _is_harness_path(relative_path: str) -> bool:
    first = relative_path.split("/", maxsplit=1)[0]
    return first in _HARNESS_PATHS


def _run_git_result(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
        )
    except OSError:
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "git_start") from None


def _run_git(root: Path, *arguments: str, allow_failure: bool = False) -> bytes:
    result = _run_git_result(root, *arguments)
    if result.returncode != 0 and not allow_failure:
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "git_command")
    return result.stdout


def _worktree_identity(workspace: Path) -> _WorktreeIdentity:
    git_file = workspace / ".git"
    try:
        root_stat = workspace.lstat()
        git_stat = git_file.lstat()
        git_payload = git_file.read_bytes()
    except OSError:
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "worktree_identity") from None
    if not stat.S_ISDIR(root_stat.st_mode) or not stat.S_ISREG(git_stat.st_mode):
        raise WorkspaceError(WorkspaceErrorCode.GIT_FAILED, "worktree_identity")
    return _WorktreeIdentity(
        root=(root_stat.st_dev, root_stat.st_ino),
        git_file=(git_stat.st_dev, git_stat.st_ino),
        git_file_sha256=sha256(git_payload).hexdigest(),
    )


def _worktree_identity_matches(workspace: Path, expected: _WorktreeIdentity) -> bool:
    try:
        return _worktree_identity(workspace) == expected
    except WorkspaceError:
        return False


def _remove_worktree(
    repository: Path,
    workspace: Path,
    identity: _WorktreeIdentity | None,
) -> None:
    if workspace.exists() or workspace.is_symlink():
        if identity is None or not _worktree_identity_matches(workspace, identity):
            raise WorkspaceError(
                WorkspaceErrorCode.CLEANUP_FAILED,
                "worktree_identity",
            )
    removal = _run_git_result(
        repository,
        "worktree",
        "remove",
        "--force",
        str(workspace),
    )
    if workspace.exists() or workspace.is_symlink():
        if removal.returncode == 0:
            raise WorkspaceError(
                WorkspaceErrorCode.CLEANUP_FAILED,
                "worktree_replaced_after_remove",
            )
        if (
            identity is None
            or not _worktree_identity_matches(workspace, identity)
        ):
            raise WorkspaceError(
                WorkspaceErrorCode.CLEANUP_FAILED,
                "worktree_identity",
            )
        try:
            if workspace.is_symlink():
                workspace.unlink()
            else:
                shutil.rmtree(workspace)
        except OSError:
            raise WorkspaceError(WorkspaceErrorCode.CLEANUP_FAILED, "worktree_remove") from None
    prune = _run_git_result(repository, "worktree", "prune")
    listing = _run_git_result(repository, "worktree", "list", "--porcelain")
    if (
        prune.returncode != 0
        or listing.returncode != 0
        or _worktree_is_registered(listing.stdout, workspace)
    ):
        raise WorkspaceError(WorkspaceErrorCode.CLEANUP_FAILED, "worktree_prune")


def _worktree_is_registered(payload: bytes, workspace: Path) -> bool:
    expected = workspace.resolve()
    for line in payload.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("worktree "):
            continue
        try:
            if Path(line.removeprefix("worktree ")).resolve() == expected:
                return True
        except (OSError, RuntimeError):
            return True
    return False
