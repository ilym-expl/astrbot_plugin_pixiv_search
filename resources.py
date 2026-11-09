"""Persistent, leased original-file cache and coalesced work downloads."""

import asyncio
import hashlib
import json
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .audit import RUN_ID
from .client import MB, DownloadBudget, disk_call
from .settings import PixivError
from .storage import pack_zip


@dataclass
class Bundle:
    images: list[Path]
    archive: Path | None = None


class Resources:
    def __init__(self, root: Path, client, settings, audit):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.client, self.settings, self.audit = client, settings, audit
        self.inflight = {}
        self.origins = {}
        self.pins = {}
        # Prevent account/proxy changes from accidentally reusing another login's data.
        self.namespace = hashlib.sha256(
            json.dumps(
                [
                    settings.cookie,
                    settings.api_base_url,
                    settings.image_base_url,
                    settings.max_pages,
                    settings.max_file_mb,
                    settings.max_total_mb,
                ]
            ).encode()
        ).hexdigest()

    def _key(self, work, mode):
        identity = [
            self.namespace,
            mode,
            work["id"],
            work.get("updateDate"),
            work.get("pageCount"),
            work.get("illustType"),
            work.get("urls"),
        ]
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def _read(self, directory):
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            return None
        try:
            manifest = json.loads((directory / "complete.json").read_text(encoding="utf-8"))
            if time.time() - directory.stat().st_mtime > self.settings.cache_ttl_minutes * 60:
                return None
            paths = {}
            for name, size in manifest["files"].items():
                path = directory / name
                if (
                    Path(name).name != name
                    or path.is_symlink()
                    or path.resolve().parent != directory
                ):
                    return None
                if not path.is_file() or path.stat().st_size != size:
                    return None
                paths[name] = path
            images = [paths[name] for name in manifest["images"]]
            if not images:
                return None
            archive = paths[manifest["archive"]] if manifest.get("archive") else None
            return Bundle(images, archive)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _remove(self, directory):
        # Only delete direct children of the dedicated asset root; never follow links.
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            return
        if directory.resolve().parent != self.root:
            raise ValueError("Asset cleanup escaped cache root")
        if directory.exists():
            shutil.rmtree(directory)

    async def _build(self, directory, work, mode):
        pid = str(work["id"])
        try:
            await disk_call(self._remove, directory)
            directory.mkdir()
            self.audit.emit("asset_build_start", pid=pid, mode=mode)
            if mode == "preview":
                url = (work.get("urls") or {}).get("original")
                if not url:
                    url = (await self.client.pages(pid))[0]["urls"]["original"]
                suffix = Path(urlsplit(url).path).suffix.lower()
                if suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"):
                    raise PixivError("首图格式暂不支持。")
                images = [directory / f"{pid}_p0000{suffix}"]
                await self.client.download(
                    url, images[0], DownloadBudget(self.settings.max_total_mb * MB)
                )
                archive = None
            elif int(work.get("illustType", 0)) == 2:
                meta = await self.client.ajax(f"illust/{pid}/ugoira_meta", {"lang": "zh"})
                url = meta.get("originalSrc") or meta.get("src")
                if not url:
                    raise PixivError("动图没有可下载的帧 ZIP 地址。")
                if len(meta.get("frames") or []) > self.settings.max_pages:
                    raise PixivError("动图帧数超过 max_pages 配置。")
                images = [directory / f"{pid}_ugoira_frames.zip", directory / "ugoira.json"]
                await self.client.download(
                    url, images[0], DownloadBudget(self.settings.max_total_mb * MB), archive=True
                )
                await disk_call(
                    images[1].write_text, json.dumps(meta, ensure_ascii=False, indent=2), "utf-8"
                )
                archive = await self._pack(directory, work, images)
            else:
                if int(work.get("pageCount", 1)) > self.settings.max_pages:
                    raise PixivError("作品页数超过 max_pages 配置，请调整限制后重试。")
                pages = await self.client.pages(pid)
                if len(pages) != int(work.get("pageCount", len(pages))):
                    raise PixivError("详情与页面接口的页数不一致，已停止；请稍后重试。")
                images = await self.client.download_all(pid, pages, directory)
                archive = await self._pack(directory, work, images)
            all_files = [p for p in directory.iterdir() if p.is_file()]
            manifest = {
                "images": [p.name for p in images],
                "archive": archive.name if archive else None,
                "files": {p.name: p.stat().st_size for p in all_files},
            }
            marker = directory / "complete.tmp"
            await disk_call(marker.write_text, json.dumps(manifest), "utf-8")
            await disk_call(marker.replace, directory / "complete.json")
            self.audit.emit(
                "asset_build_end",
                pid=pid,
                mode=mode,
                files=len(all_files),
                bytes=sum(manifest["files"].values()),
            )
            return Bundle(images, archive)
        except BaseException:
            # A failed or cancelled build must never become a cache hit.
            await disk_call(self._remove, directory)
            raise

    async def _pack(self, directory, work, images):
        from .formatting import detail

        started = time.monotonic()
        self.audit.emit("zip_start", pid=str(work["id"]), files=len(images))
        archive = await disk_call(pack_zip, directory, str(work["id"]), images, detail(work), work)
        self.audit.emit(
            "zip_end",
            pid=str(work["id"]),
            bytes=archive.stat().st_size,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        return archive

    @asynccontextmanager
    async def lease(self, work: dict, mode="full"):
        key = self._key(work, mode)
        directory = self.root / ("asset_" + key)
        self.pins[key] = self.pins.get(key, 0) + 1
        try:
            task = self.inflight.get(key)
            if task is not None:
                self.audit.emit(
                    "asset_task_join",
                    pid=str(work["id"]),
                    mode=mode,
                    producer_run_id=self.origins[key],
                )
                bundle = await asyncio.shield(task)
            else:
                # Read synchronously before the first await: only one caller can
                # create a producer for a key, even when messages arrive together.
                bundle = self._read(directory)
                if bundle is None:
                    self.audit.emit("asset_cache_miss", pid=str(work["id"]), mode=mode)
                    task = asyncio.create_task(self._build(directory, work, mode))
                    self.inflight[key] = task
                    self.origins[key] = RUN_ID.get()

                    def done(completed):
                        self.inflight.pop(key, None)
                        self.origins.pop(key, None)
                        if not completed.cancelled():
                            completed.exception()

                    task.add_done_callback(done)
                    bundle = await asyncio.shield(task)
                else:
                    self.audit.emit("asset_cache_hit", pid=str(work["id"]), mode=mode)
            yield bundle
        finally:
            if directory.exists():
                directory.touch()
            self.pins[key] -= 1
            if not self.pins[key]:
                del self.pins[key]

    async def clean(self):
        # Run on the event loop while deciding pins; await only after removing a
        # candidate from consideration. The deletion itself must exclude new leases.
        for trash in self.root.glob("trash_*"):
            try:
                await disk_call(self._remove, trash)
            except OSError:
                continue
        now = time.time()
        entries = []
        for directory in self.root.glob("asset_*"):
            key = directory.name[6:]
            if key in self.pins or key in self.inflight:
                continue
            if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                continue
            try:
                size = sum(p.stat().st_size for p in directory.iterdir() if p.is_file())
                entries.append((directory.stat().st_mtime, size, directory, key))
            except OSError:
                continue
        total = sum(size for _, size, _, _ in entries)
        removed = 0
        for modified, size, directory, key in sorted(entries):
            if (
                now - modified <= self.settings.cache_ttl_minutes * 60
                and total <= self.settings.cache_max_mb * MB
            ):
                continue
            if key in self.pins or key in self.inflight:
                continue
            # Rename atomically on the event loop before dispatching deletion, so
            # a newly arriving reader sees a miss rather than a disappearing file.
            trash = directory.with_name("trash_" + key)
            try:
                if trash.exists():
                    await disk_call(self._remove, trash)
                if key in self.pins or key in self.inflight:
                    continue
                directory.replace(trash)
                await disk_call(self._remove, trash)
                total -= size
                removed += 1
            except OSError:
                continue
        self.audit.emit("asset_cleanup", removed=removed)

    async def close(self):
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
