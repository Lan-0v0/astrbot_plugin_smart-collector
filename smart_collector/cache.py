from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .models import CollectedAsset, ContentType, safe_output_name


class CacheStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.files_dir = data_dir / "cache"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "collector.sqlite3"
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS assets (
                asset_key TEXT PRIMARY KEY,
                source_key TEXT NOT NULL,
                source_name TEXT NOT NULL,
                content_type TEXT NOT NULL,
                origin_url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                text_content TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                local_path TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                last_accessed REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_assets_source ON assets(source_key, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_assets_origin
                ON assets(source_key, origin_url, created_at DESC);
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                asset_key TEXT NOT NULL,
                sent_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_history_source ON history(source_key, id DESC);
            CREATE INDEX IF NOT EXISTS idx_history_asset ON history(source_key, asset_key);
            CREATE TABLE IF NOT EXISTS profiles (
                source_key TEXT PRIMARY KEY,
                profile_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                source_key TEXT NOT NULL,
                umo TEXT NOT NULL,
                sender_id TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                subscribed_at REAL NOT NULL,
                last_slot TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(source_key, umo)
            );
            CREATE TABLE IF NOT EXISTS schedule_state (
                source_key TEXT NOT NULL,
                umo TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_slot TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(source_key, umo)
            );
            """
        )
        conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    async def get_asset(self, asset_key: str) -> CollectedAsset | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_asset_sync, asset_key)

    async def get_asset_by_origin(self, source_key: str, origin_url: str) -> CollectedAsset | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_asset_by_origin_sync, source_key, origin_url)

    async def get_allowed_assets_by_origins(
        self, source_key: str, origin_urls: list[str], dedupe: int
    ) -> dict[str, CollectedAsset]:
        unique_urls = list(dict.fromkeys(url for url in origin_urls if url))
        if not unique_urls:
            return {}
        async with self._lock:
            return await asyncio.to_thread(
                self._get_allowed_assets_by_origins_sync,
                source_key,
                unique_urls,
                dedupe,
            )

    def _get_allowed_assets_by_origins_sync(
        self, source_key: str, origin_urls: list[str], dedupe: int
    ) -> dict[str, CollectedAsset]:
        assert self._conn is not None
        placeholders = ",".join("?" for _ in origin_urls)
        rows = self._conn.execute(
            f"SELECT * FROM assets WHERE source_key = ? AND origin_url IN ({placeholders}) "
            "ORDER BY created_at DESC",
            (source_key, *origin_urls),
        ).fetchall()
        disallowed = self._history_asset_keys_sync(
            source_key, dedupe, [str(row["asset_key"]) for row in rows]
        )
        assets: dict[str, CollectedAsset] = {}
        for row in rows:
            if row["asset_key"] in disallowed or row["origin_url"] in assets:
                continue
            path = Path(row["local_path"]) if row["local_path"] else None
            if path and not path.exists():
                continue
            assets[row["origin_url"]] = self._row_to_asset(row, cached=True)
        return assets

    def _history_asset_keys_sync(
        self, source_key: str, dedupe: int, asset_keys: list[str]
    ) -> set[str]:
        assert self._conn is not None
        if dedupe < 0 or not asset_keys:
            return set()
        if dedupe == 0:
            result: set[str] = set()
            for offset in range(0, len(asset_keys), 900):
                chunk = asset_keys[offset : offset + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT DISTINCT asset_key FROM history WHERE source_key = ? "
                    f"AND asset_key IN ({placeholders})",
                    (source_key, *chunk),
                ).fetchall()
                result.update(str(row["asset_key"]) for row in rows)
            return result
        else:
            rows = self._conn.execute(
                "SELECT asset_key FROM history WHERE source_key = ? ORDER BY id DESC LIMIT ?",
                (source_key, dedupe),
            ).fetchall()
        allowed_keys = set(asset_keys)
        return {str(row["asset_key"]) for row in rows if str(row["asset_key"]) in allowed_keys}

    def _get_asset_by_origin_sync(self, source_key: str, origin_url: str) -> CollectedAsset | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT * FROM assets WHERE source_key = ? AND origin_url = ? ORDER BY created_at DESC LIMIT 1",
            (source_key, origin_url),
        ).fetchone()
        if not row:
            return None
        path = Path(row["local_path"]) if row["local_path"] else None
        if path and not path.exists():
            return None
        return self._row_to_asset(row, cached=True)

    def _get_asset_sync(self, asset_key: str) -> CollectedAsset | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT * FROM assets WHERE asset_key = ?", (asset_key,)
        ).fetchone()
        if not row:
            return None
        local_path = Path(row["local_path"]) if row["local_path"] else None
        if local_path and not local_path.exists():
            return None
        self._conn.execute(
            "UPDATE assets SET last_accessed = ? WHERE asset_key = ?", (time.time(), asset_key)
        )
        self._conn.commit()
        return self._row_to_asset(row, cached=True)

    async def list_assets(self, source_key: str) -> list[CollectedAsset]:
        async with self._lock:
            return await asyncio.to_thread(self._list_assets_sync, source_key)

    async def list_allowed_assets(self, source_key: str, dedupe: int) -> list[CollectedAsset]:
        async with self._lock:
            return await asyncio.to_thread(self._list_allowed_assets_sync, source_key, dedupe)

    def _list_allowed_assets_sync(self, source_key: str, dedupe: int) -> list[CollectedAsset]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM assets WHERE source_key = ? ORDER BY created_at DESC", (source_key,)
        ).fetchall()
        disallowed = self._history_asset_keys_sync(
            source_key, dedupe, [str(row["asset_key"]) for row in rows]
        )
        assets: list[CollectedAsset] = []
        for row in rows:
            if row["asset_key"] in disallowed:
                continue
            path = Path(row["local_path"]) if row["local_path"] else None
            if not path or path.exists():
                assets.append(self._row_to_asset(row, cached=True))
        return assets

    def _list_assets_sync(self, source_key: str) -> list[CollectedAsset]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM assets WHERE source_key = ? ORDER BY created_at DESC", (source_key,)
        ).fetchall()
        assets = []
        for row in rows:
            path = Path(row["local_path"]) if row["local_path"] else None
            if not path or path.exists():
                assets.append(self._row_to_asset(row, cached=True))
        return assets

    async def save_asset(self, asset: CollectedAsset) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_asset_sync, asset)

    def _save_asset_sync(self, asset: CollectedAsset) -> None:
        assert self._conn is not None
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO assets(asset_key, source_key, source_name, content_type, origin_url,
                               title, text_content, mime_type, local_path, created_at, last_accessed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_key) DO UPDATE SET
                source_key=excluded.source_key, source_name=excluded.source_name,
                title=excluded.title, text_content=excluded.text_content,
                mime_type=excluded.mime_type, local_path=excluded.local_path,
                last_accessed=excluded.last_accessed
            """,
            (
                asset.asset_key,
                asset.source_key,
                asset.source_name,
                asset.content_type.value,
                asset.origin_url,
                asset.title,
                asset.text,
                asset.mime_type,
                str(asset.local_path or ""),
                now,
                now,
            ),
        )
        self._conn.commit()

    async def is_allowed(self, source_key: str, asset_key: str, dedupe: int) -> bool:
        if dedupe < 0:
            return True
        async with self._lock:
            return await asyncio.to_thread(self._is_allowed_sync, source_key, asset_key, dedupe)

    def _is_allowed_sync(self, source_key: str, asset_key: str, dedupe: int) -> bool:
        assert self._conn is not None
        if dedupe == 0:
            row = self._conn.execute(
                "SELECT 1 FROM history WHERE source_key = ? AND asset_key = ? LIMIT 1",
                (source_key, asset_key),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM (SELECT asset_key FROM history WHERE source_key = ? ORDER BY id DESC LIMIT ?) "
                "WHERE asset_key = ? LIMIT 1",
                (source_key, dedupe, asset_key),
            ).fetchone()
        return row is None

    async def mark_sent(self, source_key: str, asset_key: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._mark_sent_sync, source_key, asset_key)

    def _mark_sent_sync(self, source_key: str, asset_key: str) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO history(source_key, asset_key, sent_at) VALUES (?, ?, ?)",
            (source_key, asset_key, time.time()),
        )
        self._conn.commit()

    async def get_profile(self, source_key: str) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._get_profile_sync, source_key)

    def _get_profile_sync(self, source_key: str) -> dict[str, Any]:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT profile_json FROM profiles WHERE source_key = ?", (source_key,)
        ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["profile_json"])
        except (TypeError, json.JSONDecodeError):
            return {}

    async def save_profile(self, source_key: str, profile: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_profile_sync, source_key, profile)

    def _save_profile_sync(self, source_key: str, profile: dict[str, Any]) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO profiles(source_key, profile_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(source_key) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at",
            (source_key, json.dumps(profile, ensure_ascii=False), time.time()),
        )
        self._conn.commit()

    async def subscribe(self, source_key: str, umo: str, sender_id: str, sender_name: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._subscribe_sync, source_key, umo, sender_id, sender_name)

    def _subscribe_sync(self, source_key: str, umo: str, sender_id: str, sender_name: str) -> None:
        assert self._conn is not None
        self._conn.execute(
            """INSERT INTO subscriptions(source_key, umo, sender_id, sender_name, subscribed_at)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_key, umo) DO UPDATE SET
               sender_id=excluded.sender_id, sender_name=excluded.sender_name""",
            (source_key, umo, sender_id, sender_name, time.time()),
        )
        self._conn.commit()

    async def subscriptions(self) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._subscriptions_sync)

    def _subscriptions_sync(self) -> list[dict[str, Any]]:
        assert self._conn is not None
        return [dict(row) for row in self._conn.execute("SELECT * FROM subscriptions").fetchall()]

    async def mark_slot(self, source_key: str, umo: str, slot: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._mark_slot_sync, source_key, umo, slot)

    def _mark_slot_sync(self, source_key: str, umo: str, slot: str) -> None:
        assert self._conn is not None
        self._conn.execute(
            "UPDATE subscriptions SET last_slot = ? WHERE source_key = ? AND umo = ?",
            (slot, source_key, umo),
        )
        self._conn.commit()

    async def schedule_state(self, source_key: str, umo: str) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._schedule_state_sync, source_key, umo)

    def _schedule_state_sync(self, source_key: str, umo: str) -> dict[str, Any]:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT * FROM schedule_state WHERE source_key = ? AND umo = ?",
            (source_key, umo),
        ).fetchone()
        if row:
            return dict(row)
        first_seen = time.time()
        self._conn.execute(
            "INSERT INTO schedule_state(source_key, umo, first_seen) VALUES (?, ?, ?)",
            (source_key, umo, first_seen),
        )
        self._conn.commit()
        return {
            "source_key": source_key,
            "umo": umo,
            "first_seen": first_seen,
            "last_slot": "",
        }

    async def mark_schedule_slot(self, source_key: str, umo: str, slot: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._mark_schedule_slot_sync, source_key, umo, slot)

    def _mark_schedule_slot_sync(self, source_key: str, umo: str, slot: str) -> None:
        assert self._conn is not None
        self._conn.execute(
            "UPDATE schedule_state SET last_slot = ? WHERE source_key = ? AND umo = ?",
            (slot, source_key, umo),
        )
        self._conn.commit()

    async def cleanup(self, source_days: dict[str, int]) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_sync, source_days)

    async def cleanup_all(self, days: int) -> int:
        if days < 0:
            return 0
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_all_sync, days)

    def _cleanup_all_sync(self, days: int) -> int:
        assert self._conn is not None
        rows = self._conn.execute("SELECT DISTINCT source_key FROM assets").fetchall()
        return self._cleanup_sync({str(row["source_key"]): days for row in rows})

    def _cleanup_sync(self, source_days: dict[str, int]) -> int:
        assert self._conn is not None
        removed = 0
        now = time.time()
        output_root = (self.data_dir / "output").resolve()
        files_root = self.files_dir.resolve()
        for source_key, days in source_days.items():
            if days < 0:
                continue
            cutoff = now if days == 0 else now - days * 86400
            rows = self._conn.execute(
                "SELECT asset_key, local_path FROM assets WHERE source_key = ? AND created_at <= ?",
                (source_key, cutoff),
            ).fetchall()
            for row in rows:
                asset_key = str(row["asset_key"])
                derived_name = safe_output_name(asset_key)
                expected_derived_names = {derived_name, f"{derived_name}.zip"}
                for derived in output_root.iterdir() if output_root.exists() else ():
                    if derived.name not in expected_derived_names:
                        continue
                    try:
                        derived.resolve().relative_to(output_root)
                    except ValueError:
                        continue
                    with suppress(OSError):
                        if derived.is_dir():
                            import shutil

                            shutil.rmtree(derived, ignore_errors=True)
                        else:
                            derived.unlink(missing_ok=True)
                self._conn.execute("DELETE FROM assets WHERE asset_key = ?", (row["asset_key"],))
                if row["local_path"]:
                    still_referenced = self._conn.execute(
                        "SELECT 1 FROM assets WHERE local_path = ? LIMIT 1",
                        (row["local_path"],),
                    ).fetchone()
                    if not still_referenced:
                        with suppress(OSError):
                            local_path = Path(row["local_path"])
                            try:
                                local_path.resolve().relative_to(files_root)
                            except ValueError:
                                pass
                            else:
                                local_path.unlink(missing_ok=True)
                                if local_path.parent not in {self.data_dir, files_root}:
                                    local_path.parent.rmdir()
                removed += 1
        self._conn.commit()
        return removed

    @staticmethod
    def _row_to_asset(row: sqlite3.Row, cached: bool) -> CollectedAsset:
        return CollectedAsset(
            asset_key=row["asset_key"],
            source_key=row["source_key"],
            source_name=row["source_name"],
            content_type=ContentType(row["content_type"]),
            origin_url=row["origin_url"],
            title=row["title"],
            text=row["text_content"],
            mime_type=row["mime_type"],
            local_path=Path(row["local_path"]) if row["local_path"] else None,
            cached=cached,
        )
