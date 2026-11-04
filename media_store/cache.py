"""Bounded on-disk cache of *ciphertext* blobs.

Without it every profile picture on every page would be a Drive round-trip.
Keyed by the blob's random Drive name, so a cache file is as opaque as the
Drive object: the VPS disk never holds plaintext media. Disposable — a
missing or corrupt entry is just a cache miss. ``prune()`` is LRU by mtime
(the nightly ``prune_media_cache`` beat job); every read touches the file.

Never raises on I/O trouble: a cache that cannot be written is a slower
system, not a broken one.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class MediaCache:
    def __init__(self, root: Path | str, max_bytes: int):
        self.root = Path(root)
        self.max_bytes = int(max_bytes)

    def path(self, drive_name: str) -> Path:
        return self.root / drive_name

    def get(self, drive_name: str) -> bytes | None:
        path = self.path(drive_name)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        try:
            os.utime(path, None)            # LRU touch
        except OSError:
            pass
        return data

    def put(self, drive_name: str, data: bytes) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, self.path(drive_name))     # atomic
        except OSError as exc:
            logger.warning("media cache write failed for %s: %s", drive_name, exc)

    def remove(self, drive_name: str) -> None:
        try:
            self.path(drive_name).unlink(missing_ok=True)
        except OSError:
            pass

    def usage(self) -> tuple[int, int]:
        """``(file_count, total_bytes)``."""
        count = total = 0
        for entry in self._entries():
            count += 1
            total += entry[1]
        return count, total

    def prune(self) -> tuple[int, int]:
        """Drop least-recently-used files until total ≤ ``max_bytes``.
        Returns ``(files_removed, bytes_removed)``."""
        entries = sorted(self._entries(), key=lambda e: e[2])     # oldest mtime first
        total = sum(size for _, size, _ in entries)
        removed = removed_bytes = 0
        for path, size, _ in entries:
            if total <= self.max_bytes:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
            removed_bytes += size
        return removed, removed_bytes

    def _entries(self):
        if not self.root.is_dir():
            return
        for path in self.root.iterdir():
            if not path.is_file() or path.name.startswith(".tmp-"):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            yield path, st.st_size, st.st_mtime
