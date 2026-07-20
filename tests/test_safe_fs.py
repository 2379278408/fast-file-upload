from __future__ import annotations

import errno
import os
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import pytest

from app import safe_fs


def test_quarantine_verified_file_removes_matching_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "uploads"
    root.mkdir()
    content = b"owned-final"
    target = root / "upload_report.txt"
    target.write_bytes(content)

    removed = safe_fs.quarantine_verified_file(
        root,
        target.name,
        expected_size=len(content),
        expected_sha256=sha256(content).hexdigest(),
    )

    assert removed
    assert not target.exists()
    assert list(root.iterdir()) == []


def test_quarantine_verified_file_preserves_post_validation_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "uploads"
    root.mkdir()
    content = b"owned-final"
    target = root / "upload_report.txt"
    target.write_bytes(content)
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_bytes(b"outside")
    retained = tmp_path / "retained-owned-final"
    original_hash = safe_fs._hash_file_descriptor

    def replace_after_hash(file_fd: int) -> str:
        digest = original_hash(file_fd)
        isolated = next(root.glob(".cleanup-*"))
        isolated.rename(retained)
        isolated.symlink_to(sentinel)
        return digest

    monkeypatch.setattr(safe_fs, "_hash_file_descriptor", replace_after_hash)

    with pytest.raises(safe_fs.UnsafeCleanupError, match="changed during cleanup"):
        safe_fs.quarantine_verified_file(
            root,
            target.name,
            expected_size=len(content),
            expected_sha256=sha256(content).hexdigest(),
        )

    assert sentinel.read_bytes() == b"outside"
    assert retained.read_bytes() == content
    isolated = next(root.glob(".cleanup-*"))
    assert isolated.is_symlink()


def test_quarantine_verified_file_refuses_symlink_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "upload_report.txt"
    sentinel.write_bytes(b"outside")
    root = tmp_path / "uploads"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        safe_fs.quarantine_verified_file(
            root,
            sentinel.name,
            expected_size=sentinel.stat().st_size,
            expected_sha256=sha256(sentinel.read_bytes()).hexdigest(),
        )

    assert sentinel.read_bytes() == b"outside"


def test_quarantine_verified_file_preserves_hard_linked_object(tmp_path: Path) -> None:
    root = tmp_path / "uploads"
    root.mkdir()
    content = b"shared-inode"
    target = root / "upload_report.txt"
    target.write_bytes(content)
    external_link = tmp_path / "external-link.txt"
    os.link(target, external_link)

    with pytest.raises(safe_fs.UnsafeCleanupError, match="single-link regular file"):
        safe_fs.quarantine_verified_file(
            root,
            target.name,
            expected_size=len(content),
            expected_sha256=sha256(content).hexdigest(),
        )

    assert external_link.read_bytes() == content
    isolated = list(root.glob(".cleanup-*"))
    assert len(isolated) == 1
    assert isolated[0].read_bytes() == content


def test_quarantine_verified_file_retries_isolation_name_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "uploads"
    root.mkdir()
    content = b"collision"
    target = root / "upload_report.txt"
    target.write_bytes(content)
    original_rename = safe_fs._rename_noreplace
    attempts = 0

    def collide_once(directory_fd: int, source: str, destination: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FileExistsError(destination)
        original_rename(directory_fd, source, destination)

    monkeypatch.setattr(safe_fs, "_rename_noreplace", collide_once)

    assert safe_fs.quarantine_verified_file(
        root,
        target.name,
        expected_size=len(content),
        expected_sha256=sha256(content).hexdigest(),
    )
    assert attempts == 2


def test_quarantine_verified_file_is_concurrently_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "uploads"
    root.mkdir()
    content = b"concurrent-final"
    target = root / "upload_report.txt"
    target.write_bytes(content)

    def clean() -> bool:
        return safe_fs.quarantine_verified_file(
            root,
            target.name,
            expected_size=len(content),
            expected_sha256=sha256(content).hexdigest(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: clean(), range(2)))

    assert sorted(results) == [False, True]
    assert list(root.iterdir()) == []


def test_secure_cleanup_refuses_missing_platform_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setattr(safe_fs, "_RENAMEAT2", None)

    with pytest.raises(OSError) as error:
        safe_fs.remove_tree_anchored(root, (".resumable", "a" * 32))

    assert error.value.errno == errno.ENOTSUP


def test_remove_tree_anchored_removes_nested_content_and_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "uploads"
    session = root / ".resumable" / ("a" * 32)
    nested = session / "nested"
    nested.mkdir(parents=True)
    (session / "part-000000").write_bytes(b"part")
    (nested / "temporary").write_bytes(b"temporary")
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_bytes(b"outside")
    (session / "linked").symlink_to(sentinel)

    assert safe_fs.remove_tree_anchored(root, (".resumable", session.name))
    assert not session.exists()
    assert sentinel.read_bytes() == b"outside"
    assert not safe_fs.remove_tree_anchored(root, (".resumable", session.name))


def test_remove_tree_anchored_preserves_external_target_when_session_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "uploads"
    session = root / ".resumable" / ("b" * 32)
    session.mkdir(parents=True)
    (session / "part-000000").write_bytes(b"part")
    moved = root / ".resumable" / "moved-session"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside")
    original_list = safe_fs._list_names
    replaced = False

    def replace_session(directory_fd: int) -> list[str]:
        nonlocal replaced
        names = original_list(directory_fd)
        if not replaced and "part-000000" in names:
            replaced = True
            session.rename(moved)
            session.symlink_to(outside, target_is_directory=True)
        return names

    monkeypatch.setattr(safe_fs, "_list_names", replace_session)

    with pytest.raises(safe_fs.UnsafeCleanupError, match="changed during cleanup"):
        safe_fs.remove_tree_anchored(root, (".resumable", session.name))

    assert sentinel.read_bytes() == b"outside"
    assert session.is_symlink()


def test_remove_tree_anchored_stays_on_open_parent_when_resumable_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "uploads"
    resumable = root / ".resumable"
    session = resumable / ("c" * 32)
    session.mkdir(parents=True)
    (session / "part-000000").write_bytes(b"part")
    moved = root / ".resumable-moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside")
    original_open = safe_fs._open_directory_at
    replaced = False

    def replace_parent(parent_fd: int, name: str) -> int:
        nonlocal replaced
        directory_fd = original_open(parent_fd, name)
        if name == ".resumable" and not replaced:
            replaced = True
            resumable.rename(moved)
            resumable.symlink_to(outside, target_is_directory=True)
        return directory_fd

    monkeypatch.setattr(safe_fs, "_open_directory_at", replace_parent)

    assert safe_fs.remove_tree_anchored(root, (".resumable", session.name))
    assert sentinel.read_bytes() == b"outside"
    assert not (moved / session.name).exists()
    assert resumable.is_symlink()


def test_remove_tree_anchored_is_concurrently_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "uploads"
    session = root / ".resumable" / ("d" * 32)
    nested = session / "nested"
    nested.mkdir(parents=True)
    for index in range(20):
        (nested / f"part-{index:06d}").write_bytes(b"part")

    def clean() -> bool:
        return safe_fs.remove_tree_anchored(root, (".resumable", session.name))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: clean(), range(2)))

    assert any(results)
    assert not session.exists()
