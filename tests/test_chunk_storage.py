import asyncio
from hashlib import sha256
from pathlib import Path

import pytest

from app.chunk_storage import (
    ChunkDigestMismatch,
    ChunkSizeMismatch,
    ChunkStorage,
    PartConflict,
    PartIntegrityError,
)
from app.upload_repository import PartRecord


def test_write_part_streams_and_atomically_confirms(tmp_path: Path) -> None:
    storage = ChunkStorage(tmp_path, buffer_size=2)
    observed: list[int] = []

    async def chunks():
        yield b"abcd"

    async def on_bytes(size: int) -> None:
        observed.append(size)

    stored = asyncio.run(
        storage.write_part(
            "a" * 32, 0, chunks(), 4, sha256(b"abcd").hexdigest(), on_bytes
        )
    )

    assert stored.size_bytes == 4
    assert stored.sha256 == sha256(b"abcd").hexdigest()
    assert stored.path.read_bytes() == b"abcd"
    assert observed == [2, 2]
    assert not storage.incoming_path("a" * 32, 0).exists()


@pytest.mark.parametrize(
    ("expected_size", "expected_digest", "error"),
    [
        (4, sha256(b"wrong").hexdigest(), ChunkSizeMismatch),
        (5, sha256(b"right").hexdigest(), ChunkDigestMismatch),
    ],
)
def test_write_failure_removes_incoming_and_preserves_confirmed_parts(
    tmp_path: Path,
    expected_size: int,
    expected_digest: str,
    error: type[Exception],
) -> None:
    storage = ChunkStorage(tmp_path)
    confirmed = storage.part_path("b" * 32, 0)
    confirmed.parent.mkdir(parents=True)
    confirmed.write_bytes(b"kept")

    async def chunks():
        yield b"wrong"

    with pytest.raises(error):
        asyncio.run(
            storage.write_part("b" * 32, 1, chunks(), expected_size, expected_digest)
        )

    assert confirmed.read_bytes() == b"kept"
    assert not storage.incoming_path("b" * 32, 1).exists()


@pytest.mark.parametrize(
    ("upload_id", "part_index"),
    [
        ("a" * 31, 0),
        ("A" * 32, 0),
        ("g" * 32, 0),
        ("../" + "a" * 29, 0),
        ("a" * 32, -1),
    ],
)
def test_storage_paths_require_strict_keys(
    tmp_path: Path, upload_id: str, part_index: int
) -> None:
    storage = ChunkStorage(tmp_path)

    with pytest.raises(ValueError, match="Invalid upload storage key"):
        storage.part_path(upload_id, part_index)


def test_assemble_streams_ordered_parts_and_computes_server_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = ChunkStorage(tmp_path, buffer_size=2)
    upload_id = "c" * 32
    session = {
        "upload_id": upload_id,
        "size_bytes": 8,
        "original_name": "../report?.txt",
        "mime_type": "text/plain",
    }
    first_path = storage.part_path(upload_id, 0)
    first_path.parent.mkdir(parents=True)
    first_path.write_bytes(b"abcd")
    second_path = storage.part_path(upload_id, 1)
    second_path.write_bytes(b"efgh")
    parts = [
        PartRecord(upload_id, 1, 4, 7, 4, sha256(b"efgh").hexdigest(), "later"),
        PartRecord(upload_id, 0, 0, 3, 4, sha256(b"abcd").hexdigest(), "earlier"),
    ]
    read_sizes: list[int] = []
    original_open = Path.open

    class TrackingReader:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.source.__exit__(*args)

        def read(self, size: int):
            read_sizes.append(size)
            return self.source.read(size)

    def tracking_open(path: Path, *args, **kwargs):
        opened = original_open(path, *args, **kwargs)
        if args and args[0] == "rb":
            return TrackingReader(opened)
        return opened

    monkeypatch.setattr(Path, "open", tracking_open)
    pending = storage.assemble(session, parts)

    assert pending.temporary_path.read_bytes() == b"abcdefgh"
    assert pending.sha256 == sha256(b"abcdefgh").hexdigest()
    assert pending.original_name == "..report.txt"
    assert pending.final_path == tmp_path / f"{upload_id}_..report.txt"
    assert read_sizes and set(read_sizes) == {2}


def test_assemble_rejects_symlink_part(tmp_path: Path) -> None:
    storage = ChunkStorage(tmp_path)
    upload_id = "d" * 32
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    part = storage.part_path(upload_id, 0)
    part.parent.mkdir(parents=True)
    part.symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic link"):
        storage.assemble(
            {
                "upload_id": upload_id,
                "size_bytes": 6,
                "original_name": "safe.txt",
                "mime_type": "text/plain",
            },
            [PartRecord(upload_id, 0, 0, 5, 6, sha256(b"secret").hexdigest(), "now")],
        )


def test_reconcile_discards_incoming_and_preserves_confirmed_parts(tmp_path: Path) -> None:
    storage = ChunkStorage(tmp_path)
    upload_id = "e" * 32
    confirmed = storage.part_path(upload_id, 0)
    confirmed.parent.mkdir(parents=True)
    confirmed.write_bytes(b"kept")
    incoming = storage.incoming_path(upload_id, 1)
    incoming.write_bytes(b"partial")
    missing_upload = "f" * 32
    orphan_upload = "1" * 32
    storage.part_path(orphan_upload, 0).parent.mkdir(parents=True)

    result = storage.reconcile(
        {upload_id, missing_upload}, {(upload_id, 0), (missing_upload, 2)}
    )

    assert confirmed.read_bytes() == b"kept"
    assert not incoming.exists()
    assert result.missing_confirmed == {(missing_upload, 2)}
    assert result.orphan_sessions == {orphan_upload}


def test_concurrent_different_part_cannot_overwrite_confirmed(tmp_path: Path) -> None:
    storage = ChunkStorage(tmp_path, buffer_size=2)
    upload_id = "2" * 32

    async def write(content: bytes):
        async def chunks():
            yield content
            await asyncio.sleep(0)

        return await storage.write_part(
            upload_id, 0, chunks(), len(content), sha256(content).hexdigest()
        )

    async def race():
        return await asyncio.gather(
            write(b"first"), write(b"other"), return_exceptions=True
        )

    results = asyncio.run(race())
    conflicts = [result for result in results if isinstance(result, PartConflict)]

    assert len(conflicts) == 1
    assert storage.part_path(upload_id, 0).read_bytes() in {b"first", b"other"}
    assert not list(storage.part_path(upload_id, 0).parent.glob("incoming-*"))


def test_concurrent_identical_part_is_idempotent(tmp_path: Path) -> None:
    storage = ChunkStorage(tmp_path, buffer_size=2)
    upload_id = "3" * 32
    content = b"same"

    async def write():
        async def chunks():
            yield content
            await asyncio.sleep(0)

        return await storage.write_part(
            upload_id, 0, chunks(), len(content), sha256(content).hexdigest()
        )

    async def race():
        return await asyncio.gather(write(), write())

    first, second = asyncio.run(race())

    assert first == second
    assert first.path.read_bytes() == content
    assert not list(first.path.parent.glob("incoming-*"))


def _assembly_case(tmp_path: Path, content: bytes = b"abcdefgh"):
    storage = ChunkStorage(tmp_path, buffer_size=2)
    upload_id = "4" * 32
    session = {
        "upload_id": upload_id,
        "size_bytes": len(content),
        "original_name": "safe.txt",
        "mime_type": "text/plain",
    }
    first, second = content[:4], content[4:]
    first_path = storage.part_path(upload_id, 0)
    first_path.parent.mkdir(parents=True)
    first_path.write_bytes(first)
    storage.part_path(upload_id, 1).write_bytes(second)
    parts = [
        PartRecord(upload_id, 0, 0, 3, 4, sha256(first).hexdigest(), "first"),
        PartRecord(
            upload_id, 1, 4, len(content) - 1, len(second), sha256(second).hexdigest(), "second"
        ),
    ]
    return storage, session, parts


@pytest.mark.parametrize(
    "replacement",
    [
        PartRecord("4" * 32, 1, 3, 7, 5, sha256(b"efgh").hexdigest(), "overlap"),
        PartRecord("4" * 32, 1, 5, 7, 3, sha256(b"efgh").hexdigest(), "gap"),
    ],
)
def test_assemble_rejects_overlapping_or_gapped_ranges(
    tmp_path: Path, replacement: PartRecord
) -> None:
    storage, session, parts = _assembly_case(tmp_path)
    parts[1] = replacement

    with pytest.raises(PartIntegrityError):
        storage.assemble(session, parts)

    assert not (storage.part_path("4" * 32, 0).parent / "final.uploading").exists()


def test_assemble_rejects_record_size_mismatch(tmp_path: Path) -> None:
    storage, session, parts = _assembly_case(tmp_path)
    parts[0] = PartRecord("4" * 32, 0, 0, 3, 3, parts[0].sha256, "bad")

    with pytest.raises(PartIntegrityError):
        storage.assemble(session, parts)


@pytest.mark.parametrize("damage", [b"abc", b"wxyz"])
def test_assemble_rejects_disk_size_or_digest_damage(
    tmp_path: Path, damage: bytes
) -> None:
    storage, session, parts = _assembly_case(tmp_path)
    storage.part_path("4" * 32, 0).write_bytes(damage)

    with pytest.raises(PartIntegrityError):
        storage.assemble(session, parts)


def test_assemble_reports_missing_stored_part_as_integrity_error(tmp_path: Path) -> None:
    storage, session, parts = _assembly_case(tmp_path)
    storage.part_path("4" * 32, 1).unlink()

    with pytest.raises(PartIntegrityError, match="missing"):
        storage.assemble(session, parts)


def test_write_part_stops_consuming_immediately_after_size_limit(tmp_path: Path) -> None:
    storage = ChunkStorage(tmp_path, buffer_size=2)
    consumed = 0

    async def chunks():
        nonlocal consumed
        consumed += 1
        yield b"abcde"
        consumed += 1
        yield b"must-not-be-read"

    with pytest.raises(ChunkSizeMismatch):
        asyncio.run(
            storage.write_part("5" * 32, 0, chunks(), 4, sha256(b"abcd").hexdigest())
        )

    assert consumed == 1
    assert not list((tmp_path / ".resumable" / ("5" * 32)).glob("incoming-*"))


def test_write_part_preserves_original_error_when_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = ChunkStorage(tmp_path)
    original_open = Path.open

    class CloseFailure:
        def __init__(self, output):
            self.output = output

        def __getattr__(self, name):
            return getattr(self.output, name)

        def close(self):
            self.output.close()
            raise OSError("close failed")

    def failing_open(path: Path, *args, **kwargs):
        opened = original_open(path, *args, **kwargs)
        if args and args[0] == "xb" and path.name.startswith("incoming-"):
            return CloseFailure(opened)
        return opened

    async def chunks():
        yield b"ab"
        raise RuntimeError("upstream failed")

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(RuntimeError, match="upstream failed"):
        asyncio.run(
            storage.write_part("6" * 32, 0, chunks(), 2, sha256(b"ab").hexdigest())
        )

    assert not list((tmp_path / ".resumable" / ("6" * 32)).glob("incoming-*"))


def test_assemble_removes_control_whitespace_from_safe_name(tmp_path: Path) -> None:
    storage, session, parts = _assembly_case(tmp_path)
    session["original_name"] = "quarterly\n report\t final.txt"

    pending = storage.assemble(session, parts)

    assert pending.original_name == "quarterly report final.txt"
    assert "\n" not in pending.storage_name
    assert "\t" not in pending.storage_name
