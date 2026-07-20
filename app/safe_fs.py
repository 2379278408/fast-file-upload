from __future__ import annotations

import ctypes
import errno
import os
import stat
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from uuid import uuid4


_RENAME_NOREPLACE = 1
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


class UnsafeCleanupError(OSError):
    def __init__(self, message: str, *, isolated_name: str | None = None) -> None:
        super().__init__(errno.EPERM, message)
        self.isolated_name = isolated_name


def _require_safe_primitives() -> None:
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    required_dir_fd = (os.open, os.stat, os.unlink, os.rmdir)
    if (
        _RENAMEAT2 is None
        or any(not hasattr(os, flag) for flag in required_flags)
        or any(operation not in os.supports_dir_fd for operation in required_dir_fd)
        or os.listdir not in os.supports_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise OSError(
            errno.ENOTSUP,
            "Secure cleanup requires dir_fd, O_NOFOLLOW, and renameat2 support",
        )


def _validate_name(name: str) -> None:
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError("Cleanup paths must contain simple names")


def _assert_trusted_directory(directory_fd: int) -> os.stat_result:
    details = os.fstat(directory_fd)
    if not stat.S_ISDIR(details.st_mode):
        raise UnsafeCleanupError("Cleanup root is not a directory")
    if details.st_uid != os.geteuid() or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafeCleanupError("Cleanup directory ownership or permissions are unsafe")
    return details


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_root(path: Path) -> int:
    _require_safe_primitives()
    directory_fd = os.open(path, _directory_flags())
    try:
        _assert_trusted_directory(directory_fd)
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def _open_directory_at(parent_fd: int, name: str) -> int:
    _validate_name(name)
    directory_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        _assert_trusted_directory(directory_fd)
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def _rename_noreplace(directory_fd: int, source: str, destination: str) -> None:
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOTSUP, "renameat2 is unavailable")
    result = _RENAMEAT2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), source)


def _hash_file_descriptor(file_fd: int) -> str:
    os.lseek(file_fd, 0, os.SEEK_SET)
    digest = sha256()
    while chunk := os.read(file_fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def quarantine_verified_file(
    root: Path,
    name: str,
    *,
    expected_size: int,
    expected_sha256: str,
) -> bool:
    _validate_name(name)
    root_fd = _open_root(root)
    isolated_name: str | None = None
    try:
        for _ in range(8):
            candidate = f".cleanup-{uuid4().hex}"
            try:
                _rename_noreplace(root_fd, name, candidate)
            except FileNotFoundError:
                return False
            except FileExistsError:
                continue
            isolated_name = candidate
            break
        if isolated_name is None:
            raise OSError(errno.EEXIST, "Unable to allocate an isolated cleanup name")

        try:
            file_fd = os.open(
                isolated_name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
        except OSError as error:
            raise UnsafeCleanupError(
                "Isolated cleanup object cannot be opened safely",
                isolated_name=isolated_name,
            ) from error
        try:
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise UnsafeCleanupError(
                    "Isolated cleanup object is not a single-link regular file",
                    isolated_name=isolated_name,
                )
            actual_sha256 = _hash_file_descriptor(file_fd)
            if opened.st_size != expected_size or actual_sha256 != expected_sha256:
                raise UnsafeCleanupError(
                    "Isolated cleanup object failed durable verification",
                    isolated_name=isolated_name,
                )
            current = os.stat(isolated_name, dir_fd=root_fd, follow_symlinks=False)
            if not _same_object(opened, current):
                raise UnsafeCleanupError(
                    "Isolated cleanup object changed during cleanup",
                    isolated_name=isolated_name,
                )
            os.unlink(isolated_name, dir_fd=root_fd)
            return True
        finally:
            os.close(file_fd)
    finally:
        os.close(root_fd)


def _list_names(directory_fd: int) -> list[str]:
    return os.listdir(directory_fd)


def _entry_details(parent_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _unlink_entry(parent_fd: int, name: str) -> bool:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return False
    return True


def _remove_entry(parent_fd: int, name: str) -> bool:
    _validate_name(name)
    try:
        details = _entry_details(parent_fd, name)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        return _unlink_entry(parent_fd, name)

    try:
        directory_fd = _open_directory_at(parent_fd, name)
    except FileNotFoundError:
        return False
    except OSError:
        try:
            current = _entry_details(parent_fd, name)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(current.st_mode):
            return _unlink_entry(parent_fd, name)
        raise
    try:
        opened = os.fstat(directory_fd)
        for child_name in _list_names(directory_fd):
            _remove_entry(directory_fd, child_name)
        try:
            current = _entry_details(parent_fd, name)
        except FileNotFoundError:
            return True
        if not _same_object(opened, current):
            raise UnsafeCleanupError(f"Directory {name} changed during cleanup")
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        return True
    finally:
        os.close(directory_fd)


def remove_tree_anchored(root: Path, relative_parts: Sequence[str]) -> bool:
    if not relative_parts:
        raise ValueError("Cleanup tree requires a relative path")
    for part in relative_parts:
        _validate_name(part)
    root_fd = _open_root(root)
    opened_fds = [root_fd]
    try:
        parent_fd = root_fd
        for part in relative_parts[:-1]:
            try:
                parent_fd = _open_directory_at(parent_fd, part)
            except FileNotFoundError:
                return False
            opened_fds.append(parent_fd)
        return _remove_entry(parent_fd, relative_parts[-1])
    finally:
        for directory_fd in reversed(opened_fds):
            os.close(directory_fd)
