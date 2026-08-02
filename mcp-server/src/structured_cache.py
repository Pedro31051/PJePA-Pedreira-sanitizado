"""Structured Cache with Dual-Invalidation and Negative Cache (`structured_cache.py`).

Provides StructuredCache class for caching process analysis & extraction artifacts.
Supports dual invalidation (dossier byte/sha256 hash + extractor code version hash),
short-TTL negative caching for temporary failures/unreadable pieces, and cache statistics.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union


def compute_hash(data: Union[str, bytes]) -> str:
    """Utility to compute SHA-256 hash of string or bytes."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class CacheEntry:
    def __init__(
        self,
        key: str,
        value: Any,
        byte_hash: str,
        extractor_hash: str,
        is_negative: bool = False,
        error_reason: Optional[str] = None,
        ttl: Optional[float] = None,
        created_at: Optional[Union[float, str]] = None,
    ):
        self.key = key
        self.value = value
        self.byte_hash = byte_hash
        self.extractor_hash = extractor_hash
        self.is_negative = is_negative
        self.error_reason = error_reason
        self.ttl = ttl
        self.created_at = created_at if created_at is not None else time.time()

    def is_expired(self, current_time: Optional[float] = None) -> bool:
        if self.ttl is None:
            return False
        now = current_time if current_time is not None else time.time()
        created_at_val = self.created_at
        if isinstance(created_at_val, str):
            try:
                created_at_val = float(created_at_val)
            except ValueError:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(created_at_val.replace("Z", "+00:00"))
                    created_at_val = dt.timestamp()
                except Exception:
                    created_at_val = 0.0
        elif not isinstance(created_at_val, (int, float)):
            created_at_val = 0.0

        return (now - created_at_val) > self.ttl

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "byte_hash": self.byte_hash,
            "extractor_hash": self.extractor_hash,
            "is_negative": self.is_negative,
            "error_reason": self.error_reason,
            "ttl": self.ttl,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CacheEntry:
        return cls(
            key=data["key"],
            value=data.get("value"),
            byte_hash=data["byte_hash"],
            extractor_hash=data["extractor_hash"],
            is_negative=data.get("is_negative", False),
            error_reason=data.get("error_reason"),
            ttl=data.get("ttl"),
            created_at=data.get("created_at"),
        )


class StructuredCache:
    def __init__(
        self,
        cache_dir: Optional[Union[str, Path]] = None,
        default_negative_ttl: float = 600.0,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.default_negative_ttl = default_negative_ttl
        self._memory_cache: Dict[str, CacheEntry] = {}
        self._hits: int = 0
        self._misses: int = 0
        self._negative_hits: int = 0

        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not self.cache_dir or not self.cache_dir.exists():
            return
        for file_path in self.cache_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    entry = CacheEntry.from_dict(data)
                    self._memory_cache[entry.key] = entry
            except Exception:
                pass

    def _persist_entry(self, entry: CacheEntry) -> None:
        if not self.cache_dir:
            return
        safe_filename = hashlib.sha256(entry.key.encode("utf-8")).hexdigest() + ".json"
        file_path = self.cache_dir / safe_filename
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(entry.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _remove_persisted_entry(self, key: str) -> None:
        if not self.cache_dir:
            return
        safe_filename = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        file_path = self.cache_dir / safe_filename
        if file_path.exists():
            try:
                file_path.unlink()
            except Exception:
                pass

    def get(
        self,
        key: str,
        byte_hash: str,
        extractor_hash: str,
    ) -> Any | None:
        """Retrieves cached value if dual-hash matches and entry is valid/unexpired."""
        val, status = self.get_with_status(key, byte_hash, extractor_hash)
        return val if status == "HIT" else None

    def get_with_status(
        self,
        key: str,
        byte_hash: str,
        extractor_hash: str,
    ) -> Tuple[Any | None, str]:
        """Returns tuple (value, status) where status is 'HIT', 'NEGATIVE_HIT', or 'MISS'."""
        entry = self._memory_cache.get(key)
        if entry is None:
            self._misses += 1
            return None, "MISS"

        # Dual-hash invalidation check
        if entry.byte_hash != byte_hash or entry.extractor_hash != extractor_hash:
            self._memory_cache.pop(key, None)
            self._remove_persisted_entry(key)
            self._misses += 1
            return None, "MISS"

        # Expiration check
        if entry.is_expired():
            self._memory_cache.pop(key, None)
            self._remove_persisted_entry(key)
            self._misses += 1
            return None, "MISS"

        if entry.is_negative:
            self._negative_hits += 1
            return None, "NEGATIVE_HIT"

        self._hits += 1
        return entry.value, "HIT"

    def put(
        self,
        key: str,
        byte_hash: str,
        extractor_hash: str,
        value: Any,
        ttl: Optional[float] = None,
    ) -> None:
        """Stores a positive cache entry."""
        entry = CacheEntry(
            key=key,
            value=value,
            byte_hash=byte_hash,
            extractor_hash=extractor_hash,
            is_negative=False,
            ttl=ttl,
        )
        self._memory_cache[key] = entry
        self._persist_entry(entry)

    def set(
        self,
        key: str,
        byte_hash: str,
        extractor_hash: str,
        value: Any,
        ttl: Optional[float] = None,
    ) -> None:
        """Alias for put."""
        self.put(key, byte_hash, extractor_hash, value, ttl=ttl)

    def put_negative(
        self,
        key: str,
        byte_hash: str,
        extractor_hash: str,
        error_reason: str = "Extraction failure",
        ttl: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Stores a negative cache entry with short TTL (default 600s)."""
        eff_reason = reason if reason is not None else error_reason
        effective_ttl = ttl if ttl is not None else self.default_negative_ttl
        entry = CacheEntry(
            key=key,
            value=None,
            byte_hash=byte_hash,
            extractor_hash=extractor_hash,
            is_negative=True,
            error_reason=eff_reason,
            ttl=effective_ttl,
        )
        self._memory_cache[key] = entry
        self._persist_entry(entry)

    def set_negative(
        self,
        key: str,
        byte_hash: str,
        extractor_hash: str,
        error_reason: str = "Extraction failure",
        ttl: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Alias for put_negative."""
        self.put_negative(key, byte_hash, extractor_hash, error_reason=error_reason, ttl=ttl, reason=reason)

    def is_negative(
        self,
        key: str,
        byte_hash: str,
        extractor_hash: str,
    ) -> bool:
        """Checks if key is in negative cache and valid."""
        entry = self._memory_cache.get(key)
        if entry is None:
            return False
        if entry.byte_hash != byte_hash or entry.extractor_hash != extractor_hash:
            return False
        if entry.is_expired():
            return False
        return entry.is_negative

    def invalidate(self, key: Optional[str] = None) -> None:
        """Invalidate single key or all keys if key is None."""
        if key is None:
            self._memory_cache.clear()
            if self.cache_dir and self.cache_dir.exists():
                for f in self.cache_dir.glob("*.json"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
        else:
            self._memory_cache.pop(key, None)
            self._remove_persisted_entry(key)

    def clear(self) -> None:
        """Clear all cached entries and reset stats."""
        self.invalidate()
        self.reset_stats()

    def reset_stats(self) -> None:
        self._hits = 0
        self._misses = 0
        self._negative_hits = 0

    def get_cache_stats(self) -> Dict[str, Any]:
        """Returns statistics tracking: hits, misses, negative_hits, hit_ratio."""
        total_requests = self._hits + self._misses + self._negative_hits
        hit_ratio = (
            round((self._hits + self._negative_hits) / total_requests, 4)
            if total_requests > 0
            else 0.0
        )
        return {
            "hits": self._hits,
            "misses": self._misses,
            "negative_hits": self._negative_hits,
            "total_requests": total_requests,
            "hit_ratio": hit_ratio,
        }
