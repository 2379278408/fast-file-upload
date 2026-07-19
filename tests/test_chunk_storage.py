import asyncio
from hashlib import sha256
from pathlib import Path

import pytest

from app.chunk_storage import ChunkDigestMismatch, ChunkSizeMismatch, ChunkStorage
from app.upload_repository import PartRecord


def test_write_part_streams_and_atomically_confirms(tmp_path: Path) -> None:
    storage = ChunkStorage(tmp_path)
    observed: list[int] = []

    async def chunks():
        yield b"ab"
        yield b"cd"

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
