"""Research Harness의 disposable candidate 작업공간 경계.

[파이프라인] 고정된 champion SHA와 validation ``CandidateDataView``를 받아 coding agent와
LocalRunner가 사용할 일회성 Git worktree를 준비하고, trial 종료 뒤 회수한다.

[기능] 정확한 기준 SHA checkout, validation 전용 입력·예측 출력 경로, credential-free
최소 프로세스 환경, 변경 내용 credential 검사와 ledger용 diff fingerprint를 제공한다.

[비책임] candidate subprocess 실행·seed 전달·timeout 회수, final holdout 소비 권한,
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
from autoresearch.research_harness.candidate_data_view import (
    materialize_candidate_data_view,
)
from autoresearch.research_harness.evaluation_source import ActionLogSource
from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.fixture_models import (
    CandidateDataViewRequest,
    JudgeSnapshotHandoff,
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


@dataclass(frozen=True, slots=True)
class WorkspaceError(Exception):
    """로컬 경로나 credential 값을 노출하지 않는 workspace 오류."""

    code: WorkspaceErrorCode
    stage: str

    def __str__(self) -> str:
        return f"{self.code.value}: stage={self.stage}"


@dataclass(frozen=True, slots=True)
class CandidateWorkspaceRequest:
    """하나의 validation trial worktree를 만드는 Harness 소유 입력."""

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
) -> Iterator[CandidateWorkspace]:
    """정확한 SHA의 validation worktree를 열고 모든 종료 경로에서 회수한다."""

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
            data_view = materialize_candidate_data_view(
                CandidateDataViewRequest(request.judge, workspace),
                source=source,
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
    tracked_patch = _run_git(
        root,
        "-c",
        "core.filemode=true",
        "-c",
        "diff.renames=false",
        "-c",
        "diff.algorithm=myers",
        "diff",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        "--full-index",
        base_sha,
        "--",
        ".",
        ":(exclude)harness_in",
        ":(exclude)harness_out",
        ":(exclude).harness-in.lock",
    )
    tracked_paths = _nul_paths(
        _run_git(
            root,
            "-c",
            "diff.renames=false",
            "diff",
            "--no-renames",
            "--name-only",
            "--diff-filter=ACDMRTUXB",
            "-z",
            base_sha,
        )
    )
    untracked_paths = _nul_paths(
        _run_git(root, "ls-files", "--others", "-z")
    )
    changed_paths = tuple(
        sorted(
            path
            for path in set((*tracked_paths, *untracked_paths))
            if not _is_harness_path(path)
        )
    )
    _require_credential_free(root, changed_paths)

    digest = sha256()
    _update_digest(digest, b"tracked-patch", tracked_patch)
    for relative_path in sorted(
        path for path in untracked_paths if not _is_harness_path(path)
    ):
        path = root.joinpath(*relative_path.split("/"))
        payload = _current_payload(path)
        _update_digest(
            digest,
            f"{_current_mode(path)}:{relative_path}".encode("utf-8"),
            payload,
        )
    return CandidateChangeReceipt(
        diff_fingerprint=f"sha256:{digest.hexdigest()}",
        changed_paths=changed_paths,
    )


def _require_credential_free(root: Path, changed_paths: tuple[str, ...]) -> None:
    for relative_path in changed_paths:
        path = root.joinpath(*relative_path.split("/"))
        if not path.exists() and not path.is_symlink():
            continue
        payload = _current_payload(path)
        text = payload.decode("utf-8", errors="replace")
        if contains_credential_value(text) or any(
            pattern.search(text) is not None
            for pattern in _ADDITIONAL_CREDENTIAL_PATTERNS
        ):
            raise WorkspaceError(
                WorkspaceErrorCode.CREDENTIAL_DETECTED,
                "candidate_change_scan",
            )


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
        if (
            removal.returncode != 0
            and (identity is None or not _worktree_identity_matches(workspace, identity))
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
