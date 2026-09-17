"""Fixed Git identity and fail-closed local storage foundations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import time
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from .model import MAX_LEDGER_BYTES, LedgerError, parse_ledger


_GIT_ARGV = [
    "git",
    "rev-parse",
    "--path-format=absolute",
    "--show-toplevel",
    "--absolute-git-dir",
    "--git-common-dir",
]
_HEAD_ARGV = ["git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}"]
_MAX_DISCOVERY_OUTPUT = 16 * 1024
_MAX_HEAD_OUTPUT = 65
_STORAGE_DIR = "papercuts"
_LOCK_NAME = "write.lock"
_LEDGER_NAME = "events.jsonl"
_APPEND_RETRY_INTERVAL = 2.0
_APPEND_RETRY_SLEEP = 0.010
_monotonic = time.monotonic
_sleep = time.sleep


class RepoError(RuntimeError):
    """A stable, non-disclosing repository discovery error."""

    def __init__(self, rule_id: str):
        self.rule_id = rule_id
        super().__init__(rule_id)

    def __str__(self) -> str:
        return f"papercuts: {self.rule_id}"


class StorageError(RuntimeError):
    """A stable, non-disclosing storage safety or lock error."""

    def __init__(self, rule_id: str):
        self.rule_id = rule_id
        super().__init__(rule_id)

    def __str__(self) -> str:
        return f"papercuts: {self.rule_id}"


class _AppendLockBusy(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RepoPaths:
    top_level: str
    git_dir: str
    common_dir: str


@dataclass(frozen=True, slots=True)
class GitHead:
    state: str
    commit: str | None


def _freeze_snapshot_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_snapshot_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_snapshot_value(item) for item in value)
    return value


def _materialize_snapshot_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _materialize_snapshot_value(item) for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_materialize_snapshot_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    raw_bytes: bytes
    events: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_bytes", bytes(self.raw_bytes))
        object.__setattr__(
            self,
            "events",
            tuple(_freeze_snapshot_value(event) for event in self.events),
        )

    def materialize_events(self) -> list[dict]:
        """Return independent ordinary containers for compatibility consumers."""

        return _materialize_snapshot_value(self.events)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    paths: RepoPaths
    worktree_kind: str
    head: GitHead
    storage: StorageSnapshot


def _repo_error() -> RepoError:
    return RepoError("E_GIT_DISCOVERY")


def _git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }


def _lexical_absolute(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _repo_error()
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise _repo_error()
    parts = value.split("/")
    if any(part in {".", ".."} for part in parts):
        raise _repo_error()
    return value


def _worktree_kind(paths: RepoPaths) -> str:
    if not isinstance(paths, RepoPaths):
        raise _repo_error()
    top_level = _lexical_absolute(paths.top_level)
    git_dir = _lexical_absolute(paths.git_dir)
    common_dir = _lexical_absolute(paths.common_dir)
    worktree_parent = os.path.dirname(git_dir)
    if git_dir == common_dir:
        return "main"
    if (
        os.path.basename(worktree_parent) == "worktrees"
        and os.path.dirname(worktree_parent) == common_dir
    ):
        return "linked"
    raise _repo_error()


def discover_repository(cwd: str | os.PathLike[str] = ".") -> RepoPaths:
    """Discover the repository using one fixed, sanitized Git invocation."""

    try:
        completed = subprocess.run(
            _GIT_ARGV,
            cwd=cwd,
            env=_git_environment(),
            shell=False,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0 or completed.stderr != b"":
            raise _repo_error()
        stdout = completed.stdout
        if not isinstance(stdout, bytes) or len(stdout) > _MAX_DISCOVERY_OUTPUT:
            raise _repo_error()
        if not stdout.endswith(b"\n") or b"\r" in stdout or b"\x00" in stdout:
            raise _repo_error()
        lines = stdout.split(b"\n")[:-1]
        if len(lines) != 3 or any(not line for line in lines):
            raise _repo_error()
        decoded = [line.decode("utf-8", errors="strict") for line in lines]
        top_level, git_dir, common_dir = (_lexical_absolute(item) for item in decoded)
        paths = RepoPaths(top_level, git_dir, common_dir)
        _worktree_kind(paths)
        return paths
    except RepoError:
        raise
    except (OSError, UnicodeError, AttributeError, TypeError, ValueError):
        raise _repo_error() from None


def inspect_head(paths: RepoPaths) -> GitHead:
    """Inspect only the exact commit at HEAD with a fixed Git invocation."""

    try:
        _worktree_kind(paths)
        completed = subprocess.run(
            _HEAD_ARGV,
            cwd=paths.top_level,
            env=_git_environment(),
            shell=False,
            capture_output=True,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
            raise _repo_error()
        if len(stdout) > _MAX_HEAD_OUTPUT or stderr != b"":
            raise _repo_error()
        if completed.returncode == 1 and stdout == b"":
            return GitHead("unborn", None)
        if completed.returncode != 0:
            raise _repo_error()
        if (
            not stdout.endswith(b"\n")
            or b"\r" in stdout
            or b"\x00" in stdout
            or len(stdout) not in {41, 65}
        ):
            raise _repo_error()
        encoded_oid = stdout[:-1]
        if any(byte not in b"0123456789abcdef" for byte in encoded_oid):
            raise _repo_error()
        return GitHead("commit", encoded_oid.decode("ascii"))
    except RepoError:
        raise
    except (OSError, UnicodeError, AttributeError, TypeError, ValueError):
        raise _repo_error() from None


def _storage_error(rule_id: str = "E_STORAGE_UNSAFE") -> StorageError:
    return StorageError(rule_id)


def _absolute_storage_path(value: str | os.PathLike[str]) -> str:
    try:
        path = os.fspath(value)
    except TypeError:
        raise _storage_error() from None
    if not isinstance(path, str) or not path or "\x00" in path:
        raise _storage_error()
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise _storage_error()
    if any(part in {".", ".."} for part in path.split("/")):
        raise _storage_error()
    return path


def _open_directory_chain(path: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open("/", flags)
        try:
            for component in path.split("/")[1:]:
                if not component:
                    continue
                child = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except Exception:
            os.close(fd)
            raise
    except (OSError, ValueError):
        raise _storage_error() from None


def _verify_directory(fd: int, *, storage: bool = False) -> None:
    try:
        info = os.fstat(fd)
    except OSError:
        raise _storage_error() from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise _storage_error()
    if storage and stat.S_IMODE(info.st_mode) != 0o700:
        raise _storage_error()


def _open_storage_dir(common_fd: int, *, create: bool) -> tuple[int, bool]:
    created = False
    storage_fd = -1
    try:
        try:
            storage_fd = os.open(
                _STORAGE_DIR,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=common_fd,
            )
        except FileNotFoundError:
            if not create:
                return -1, False
            try:
                os.mkdir(_STORAGE_DIR, 0o700, dir_fd=common_fd)
            except FileExistsError:
                pass
            else:
                os.fsync(common_fd)
                created = True
            storage_fd = os.open(
                _STORAGE_DIR,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=common_fd,
            )
        _verify_directory(storage_fd, storage=True)
        try:
            named_storage = os.stat(
                _STORAGE_DIR,
                dir_fd=common_fd,
                follow_symlinks=False,
            )
        except (OSError, ValueError):
            raise _storage_error() from None
        if (
            not stat.S_ISDIR(named_storage.st_mode)
            or named_storage.st_uid != os.geteuid()
            or stat.S_IMODE(named_storage.st_mode) != 0o700
        ):
            raise _storage_error()
        opened_storage = os.fstat(storage_fd)
        if (
            opened_storage.st_dev != named_storage.st_dev
            or opened_storage.st_ino != named_storage.st_ino
        ):
            raise _storage_error()
        return storage_fd, created
    except StorageError:
        if storage_fd >= 0:
            try:
                os.close(storage_fd)
            except OSError:
                pass
        raise
    except (OSError, ValueError):
        if storage_fd >= 0:
            try:
                os.close(storage_fd)
            except OSError:
                pass
        raise _storage_error() from None


def _entry_stat(storage_fd: int, name: str):
    try:
        return os.stat(name, dir_fd=storage_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise _storage_error() from None


def _entry_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_uid, info.st_nlink, stat.S_IMODE(info.st_mode))


def _verify_fixed_entry(
    storage_fd: int,
    name: str,
    *,
    writable: bool = False,
    append: bool = False,
) -> tuple[int, tuple[int, int, int, int, int]]:
    info = _entry_stat(storage_fd, name)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise _storage_error()
    if info.st_uid != os.geteuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        raise _storage_error()
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
    if append:
        flags |= os.O_APPEND
    fd = -1
    try:
        fd = os.open(name, flags, dir_fd=storage_fd)
        after = os.fstat(fd)
    except (OSError, ValueError):
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        raise _storage_error() from None
    identity = _entry_identity(info)
    if _entry_identity(after) != identity or not stat.S_ISREG(after.st_mode):
        os.close(fd)
        raise _storage_error()
    return fd, identity


def _state(storage_fd: int) -> tuple[bool, bool]:
    lock = _entry_stat(storage_fd, _LOCK_NAME)
    ledger = _entry_stat(storage_fd, _LEDGER_NAME)
    if lock is not None:
        if not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.geteuid() or lock.st_nlink != 1 or stat.S_IMODE(lock.st_mode) != 0o600:
            raise _storage_error()
    if ledger is not None:
        if not stat.S_ISREG(ledger.st_mode) or ledger.st_uid != os.geteuid() or ledger.st_nlink != 1 or stat.S_IMODE(ledger.st_mode) != 0o600:
            raise _storage_error()
    return lock is not None, ledger is not None


def _lock(fd: int, operation: int) -> None:
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        if isinstance(exc, BlockingIOError) or exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise _storage_error("E_LOCK_BUSY") from None
        raise _storage_error() from None


def _read_complete(fd: int, identity: tuple[int, int, int, int, int]) -> bytes:
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, MAX_LEDGER_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_LEDGER_BYTES:
                break
        after = os.fstat(fd)
        if _entry_identity(after) != identity or total != after.st_size:
            raise _storage_error()
        return b"".join(chunks)
    except StorageError:
        raise
    except OSError:
        raise _storage_error() from None


def _open_common_and_storage(common: str, *, create: bool) -> tuple[int, int]:
    common_fd = _open_directory_chain(common)
    try:
        storage_fd, _ = _open_storage_dir(common_fd, create=create)
        return common_fd, storage_fd
    except Exception:
        os.close(common_fd)
        raise


def _append_event_once(common: str, line: bytes, decide) -> None:
    common_fd = storage_fd = lock_fd = ledger_fd = -1
    mutation_started = False
    try:
        common_fd, storage_fd = _open_common_and_storage(common, create=True)
        initial = _state(storage_fd)
        if not initial[0] and initial[1]:
            raise _storage_error("E_STORAGE_LEDGER_WITHOUT_LOCK")
        if initial[0]:
            lock_fd, lock_identity = _verify_fixed_entry(storage_fd, _LOCK_NAME, writable=True)
        else:
            try:
                lock_fd = os.open(
                    _LOCK_NAME,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=storage_fd,
                )
                lock_info = os.fstat(lock_fd)
                lock_identity = _entry_identity(lock_info)
                if lock_identity[2:] != (os.geteuid(), 1, 0o600):
                    raise _storage_error()
                os.fsync(storage_fd)
            except FileExistsError:
                lock_fd, lock_identity = _verify_fixed_entry(
                    storage_fd, _LOCK_NAME, writable=True
                )
            except StorageError:
                raise
            except (OSError, ValueError):
                raise _storage_error() from None
        try:
            _lock(lock_fd, fcntl.LOCK_EX)
        except StorageError as exc:
            if exc.rule_id == "E_LOCK_BUSY":
                raise _AppendLockBusy from None
            raise
        current_lock = _entry_stat(storage_fd, _LOCK_NAME)
        current = _state(storage_fd)
        if (
            current_lock is None
            or _entry_identity(current_lock) != lock_identity
            or not current[0]
            or (initial[1] and not current[1])
        ):
            raise _storage_error()

        ledger_info = _entry_stat(storage_fd, _LEDGER_NAME)
        if ledger_info is None:
            events = []
            decide(events)
            candidate = line
            parse_ledger(candidate)
            try:
                ledger_fd = os.open(
                    _LEDGER_NAME,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=storage_fd,
                )
            except FileExistsError:
                ledger_fd, ledger_identity = _verify_fixed_entry(
                    storage_fd, _LEDGER_NAME, writable=True, append=True
                )
                os.lseek(ledger_fd, 0, os.SEEK_SET)
                data = _read_complete(ledger_fd, ledger_identity)
                events = parse_ledger(data)
                decide(events)
                candidate = data + line
                parse_ledger(candidate)
            except (OSError, ValueError):
                raise _storage_error() from None
            else:
                ledger_identity = _entry_identity(os.fstat(ledger_fd))
                if ledger_identity[2:] != (os.geteuid(), 1, 0o600):
                    raise _storage_error()
                os.fsync(storage_fd)
        else:
            if (
                not stat.S_ISREG(ledger_info.st_mode)
                or ledger_info.st_uid != os.geteuid()
                or ledger_info.st_nlink != 1
                or stat.S_IMODE(ledger_info.st_mode) != 0o600
            ):
                raise _storage_error()
            ledger_fd, ledger_identity = _verify_fixed_entry(
                storage_fd, _LEDGER_NAME, writable=True, append=True
            )
            os.lseek(ledger_fd, 0, os.SEEK_SET)
            data = _read_complete(ledger_fd, ledger_identity)
            events = parse_ledger(data)
            decide(events)
            candidate = data + line
            parse_ledger(candidate)

        mutation_started = True
        view = memoryview(line)
        while view:
            written = os.write(ledger_fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "short write")
            view = view[written:]
        os.fsync(ledger_fd)
        final_info = _entry_stat(storage_fd, _LEDGER_NAME)
        if final_info is None or _entry_identity(final_info) != ledger_identity:
            raise _storage_error("E_STORAGE_INDETERMINATE")
        os.fsync(storage_fd)
    except (LedgerError, StorageError):
        if mutation_started:
            raise _storage_error("E_STORAGE_INDETERMINATE") from None
        raise
    except (OSError, ValueError):
        raise _storage_error("E_STORAGE_INDETERMINATE" if mutation_started else "E_STORAGE") from None
    finally:
        for fd in (ledger_fd, lock_fd, storage_fd, common_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def append_event(common_dir: str | os.PathLike[str], line: bytes, decide) -> None:
    """Validate and append one canonical line within one descriptor-bound transaction."""

    common = _absolute_storage_path(common_dir)
    deadline = _monotonic() + _APPEND_RETRY_INTERVAL
    while True:
        try:
            _append_event_once(common, line, decide)
            return
        except _AppendLockBusy:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise _storage_error("E_LOCK_BUSY") from None
            _sleep(min(_APPEND_RETRY_SLEEP, remaining))

def read_storage_snapshot(common_dir: str | os.PathLike[str]) -> StorageSnapshot:
    """Return exact bytes and parsed events from one validated locked read."""

    common = _absolute_storage_path(common_dir)
    common_fd, storage_fd = _open_common_and_storage(common, create=False)
    if storage_fd < 0:
        os.close(common_fd)
        return StorageSnapshot(b"", ())
    lock_fd = ledger_fd = -1
    try:
        initial = _state(storage_fd)
        if not initial[0] and initial[1]:
            raise _storage_error("E_STORAGE_LEDGER_WITHOUT_LOCK")
        if not initial[0]:
            return StorageSnapshot(b"", ())
        lock_fd, lock_identity = _verify_fixed_entry(storage_fd, _LOCK_NAME)
        _lock(lock_fd, fcntl.LOCK_SH)
        current = _state(storage_fd)
        current_lock = _entry_stat(storage_fd, _LOCK_NAME)
        if (
            current != initial
            or current != (True, initial[1])
            or current_lock is None
            or _entry_identity(current_lock) != lock_identity
        ):
            raise _storage_error()
        if not current[1]:
            return StorageSnapshot(b"", ())
        ledger_fd, ledger_identity = _verify_fixed_entry(storage_fd, _LEDGER_NAME)
        data = _read_complete(ledger_fd, ledger_identity)
        return StorageSnapshot(data, tuple(parse_ledger(data)))
    finally:
        for fd in (ledger_fd, lock_fd, storage_fd, common_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def read_storage(common_dir: str | os.PathLike[str]) -> list[dict]:
    """Return the same parsed event list exposed by the original reader."""

    return read_storage_snapshot(common_dir).materialize_events()


def capture_source_snapshot(paths: RepoPaths) -> SourceSnapshot:
    """Bind one ledger snapshot to stable repository and HEAD observations."""

    _worktree_kind(paths)
    before_paths = discover_repository(paths.top_level)
    if before_paths != paths:
        raise RepoError("E_PACKET_SOURCE_CHANGED")
    before_head = inspect_head(before_paths)
    storage = read_storage_snapshot(before_paths.common_dir)
    after_paths = discover_repository(before_paths.top_level)
    after_head = inspect_head(after_paths)
    if after_paths != before_paths or after_head != before_head:
        raise RepoError("E_PACKET_SOURCE_CHANGED")
    return SourceSnapshot(
        paths=before_paths,
        worktree_kind=_worktree_kind(before_paths),
        head=before_head,
        storage=storage,
    )


@contextmanager
def shared_lock(common_dir: str | os.PathLike[str]) -> Iterator[str]:
    """Acquire an existing persistent lock shared and nonblocking."""

    common = _absolute_storage_path(common_dir)
    common_fd, storage_fd = _open_common_and_storage(common, create=False)
    lock_fd = -1
    try:
        if storage_fd < 0:
            raise _storage_error()
        state = _state(storage_fd)
        if not state[0]:
            raise _storage_error()
        lock_fd, identity = _verify_fixed_entry(storage_fd, _LOCK_NAME)
        _lock(lock_fd, fcntl.LOCK_SH)
        check = _entry_stat(storage_fd, _LOCK_NAME)
        if check is None or _entry_identity(check) != identity:
            raise _storage_error()
        yield common
    finally:
        for fd in (lock_fd, storage_fd, common_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


@contextmanager
def exclusive_lock(common_dir: str | os.PathLike[str]) -> Iterator[str]:
    """Create/verify writer scaffolding and acquire its lock exclusively."""

    common = _absolute_storage_path(common_dir)
    common_fd, storage_fd = _open_common_and_storage(common, create=True)
    lock_fd = -1
    try:
        if storage_fd < 0:
            raise _storage_error()
        initial = _state(storage_fd)
        if not initial[0] and initial[1]:
            raise _storage_error("E_STORAGE_LEDGER_WITHOUT_LOCK")
        if initial[0]:
            lock_fd, identity = _verify_fixed_entry(storage_fd, _LOCK_NAME, writable=True)
        else:
            try:
                lock_fd = os.open(
                    _LOCK_NAME,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=storage_fd,
                )
                created_info = os.fstat(lock_fd)
                if _entry_identity(created_info)[2:] != (os.geteuid(), 1, 0o600):
                    raise _storage_error()
                os.fsync(storage_fd)
                identity = _entry_identity(created_info)
            except StorageError:
                raise
            except (OSError, ValueError):
                raise _storage_error() from None
        _lock(lock_fd, fcntl.LOCK_EX)
        current = _state(storage_fd)
        if not current[0] or current[1] != initial[1] or (_entry_stat(storage_fd, _LOCK_NAME) is None):
            raise _storage_error()
        current_lock = _entry_stat(storage_fd, _LOCK_NAME)
        if current_lock is None or _entry_identity(current_lock) != identity:
            raise _storage_error()
        if current[1]:
            ledger_fd, ledger_identity = _verify_fixed_entry(storage_fd, _LEDGER_NAME, writable=False)
            try:
                parse_ledger(_read_complete(ledger_fd, ledger_identity))
            finally:
                os.close(ledger_fd)
        yield common
    finally:
        if lock_fd >= 0:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        for fd in (storage_fd, common_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


# Explicit aliases for callers that name the acquisition operation.
acquire_shared_lock = shared_lock
acquire_exclusive_lock = exclusive_lock
read_ledger = read_storage


__all__ = [
    "append_event",
    "capture_source_snapshot",
    "GitHead",
    "RepoError",
    "RepoPaths",
    "SourceSnapshot",
    "StorageSnapshot",
    "StorageError",
    "acquire_exclusive_lock",
    "acquire_shared_lock",
    "discover_repository",
    "exclusive_lock",
    "inspect_head",
    "read_ledger",
    "read_storage",
    "read_storage_snapshot",
    "shared_lock",
]
