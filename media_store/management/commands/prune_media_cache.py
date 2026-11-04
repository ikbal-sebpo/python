"""Trim the local ciphertext cache to ``MEDIA_CACHE_MAX_BYTES`` (LRU).

Nightly beat job (scheduler/schedule.py). Safe to run any time: the cache is
disposable and a pruned blob is re-fetched from Drive on next read.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from media_store.cache import MediaCache


class Command(BaseCommand):
    help = "LRU-prune the media cache down to MEDIA_CACHE_MAX_BYTES."

    def handle(self, *args, **options):
        cache = MediaCache(settings.MEDIA_CACHE_ROOT, settings.MEDIA_CACHE_MAX_BYTES)
        before_count, before_bytes = cache.usage()
        removed, removed_bytes = cache.prune()
        self.stdout.write(
            f"cache {cache.root}: {before_count} files / {before_bytes / 2**20:,.1f} MiB before; "
            f"removed {removed} files / {removed_bytes / 2**20:,.1f} MiB; "
            f"limit {cache.max_bytes / 2**20:,.0f} MiB"
        )
