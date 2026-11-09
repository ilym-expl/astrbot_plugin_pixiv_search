"""AstrBot entry point. Network, parsing and storage remain independently testable."""

import asyncio
import hashlib
import math
import re
import time
from pathlib import Path
from uuid import uuid4

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .audit import RUN_ID, Audit, exception_info
from .client import PixivClient
from .commands import HELP, Command, artwork_links, parse_command
from .formatting import detail, search_chunks
from .resources import Resources
from .settings import PixivError, Settings
from .storage import JobStore


class PixivSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = Settings.from_mapping(config)
        root = (
            Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_pixiv_search" / "cache"
        )
        self.store = JobStore(root, self.settings.cache_ttl_minutes)
        self.audit = Audit(root.parent / "logs", self.settings)
        self.audit.emit(
            "plugin_start",
            version="1.2.0",
            cookie_configured=bool(self.settings.cookie),
            proxy_configured=bool(self.settings.proxy),
            api_interval_ms=self.settings.api_interval_ms,
            image_interval_ms=self.settings.image_interval_ms,
            concurrency=self.settings.download_concurrency,
        )
        self.resources = None
        self.client = None
        self.cleaner = None
        self.jobs: set[asyncio.Task] = set()
        self.last_request: dict[str, float] = {}
        self.running = 0

    async def initialize(self):
        await asyncio.to_thread(self.store.clean)
        self._ensure_client()
        await self.resources.clean()

    def _ensure_client(self):
        if self.client is None:
            self.client = PixivClient(self.settings, audit=self.audit)
        self.client.audit = self.audit
        self.client.gate.audit = self.audit
        self.client.metadata.audit = self.audit
        if self.resources is None:
            self.resources = Resources(
                self.store.root / "assets", self.client, self.settings, self.audit
            )
        if self.cleaner is None:
            self.cleaner = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(300)
            try:
                await asyncio.to_thread(self.store.clean)
                await self.resources.clean()
                cutoff = time.monotonic() - max(self.settings.cooldown_seconds, 60)
                self.last_request = {k: v for k, v in self.last_request.items() if v > cutoff}
            except Exception as exc:
                self.audit.emit("cleanup_error", **exception_info(exc))
                logger.warning(f"Pixiv 缓存清理失败（{type(exc).__name__}）")

    async def _send_file(self, event, path: Path):
        started = time.monotonic()
        self.audit.emit("send_start", kind="file", file=path.name, bytes=path.stat().st_size)
        try:
            await event.send(event.chain_result([Comp.File(name=path.name, file=str(path))]))
            self.audit.emit(
                "send_end",
                kind="file",
                file=path.name,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            self.audit.emit("send_error", kind="file", file=path.name, **exception_info(exc))
            raise PixivError(
                "文件已生成，但平台上传失败。请检查适配器是否支持文件、上传大小限制及容器共享路径；缓存保留到 TTL 到期。"
            ) from None

    async def _send_image(self, event, path: Path):
        started = time.monotonic()
        self.audit.emit("send_start", kind="image", file=path.name, bytes=path.stat().st_size)
        try:
            await event.send(event.chain_result([Comp.Image.fromFileSystem(str(path))]))
            self.audit.emit(
                "send_end",
                kind="image",
                file=path.name,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            self.audit.emit("send_error", kind="image", file=path.name, **exception_info(exc))
            raise PixivError(
                "图片已下载，但平台发送失败。请检查图片大小限制及协议端共享路径，或将 direct_send_mode 改为 file。"
            ) from None

    async def _preview(self, event, work: dict, directory=None):
        async with self.resources.lease(work, "preview") as bundle:
            await self._send_image(event, bundle.images[0])

    async def _download_work(self, event, work: dict, directory=None):
        pid = str(work["id"])
        await event.send(event.plain_result(detail(work) + "\n正在读取缓存或按全局队列下载原图…"))
        async with self.resources.lease(work) as bundle:
            if (
                int(work.get("illustType", 0)) == 2
                or len(bundle.images) > self.settings.direct_send_limit
            ):
                self.audit.emit("send_plan", pid=pid, mode="zip", files=len(bundle.images))
                await self._send_file(event, bundle.archive)
            else:
                self.audit.emit(
                    "send_plan",
                    pid=pid,
                    mode=self.settings.direct_send_mode,
                    files=len(bundle.images),
                )
                for image in bundle.images:
                    if self.settings.direct_send_mode == "image":
                        await self._send_image(event, image)
                    else:
                        await self._send_file(event, image)

    @filter.command("p")
    async def pixiv(self, event: AstrMessageEvent):
        """Pixiv tag / UID 搜索、PID 详情与全作品原图下载。使用 /p 帮助。"""
        if event.get_extra("pixiv_search.handled", False):
            return
        event.set_extra("pixiv_search.handled", True)
        async for result in self._execute(event):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_pixiv(self, event: AstrMessageEvent):
        """Detect Pixiv artwork links in ordinary text and download complete works."""
        if not self.settings.auto_parse_links or event.get_extra("pixiv_search.handled", False):
            return
        self_id = str(event.get_self_id() or "")
        if self_id and str(event.get_sender_id()) == self_id:
            return
        text = event.get_message_str()
        # Explicit commands keep their semantics regardless of handler ordering:
        # /p 搜 URL must remain a first-picture preview, not a full download.
        if re.match(r"^/?p(?:\s|$)", text.strip(), re.IGNORECASE):
            return
        links = artwork_links(text)
        if not links:
            return
        event.set_extra("pixiv_search.handled", True)
        limit = self.settings.auto_max_links
        if len(links) > limit:
            yield event.plain_result(
                f"检测到 {len(links)} 个不同作品链接，本条消息只处理前 {limit} 个。"
            )
        async for result in self._execute(
            event, Command("download"), links=links[:limit], source="auto"
        ):
            yield result

    async def _execute(
        self, event: AstrMessageEvent, command=None, *, links=None, source="command"
    ):
        run_id = uuid4().hex[:12]
        token = RUN_ID.set(run_id)
        started = time.monotonic()
        event.set_extra("pixiv_search.outcome", "ok")
        self.audit.emit(
            "run_start",
            source=source,
            links=len(links or []),
            session_hash=hashlib.sha256(event.unified_msg_origin.encode()).hexdigest()[:12],
            sender_hash=hashlib.sha256(str(event.get_sender_id()).encode()).hexdigest()[:12],
        )
        try:
            async for result in self._execute_inner(event, command, links=links):
                self.audit.emit("text_result")
                yield result
        except BaseException as exc:
            event.set_extra(
                "pixiv_search.outcome",
                "cancelled"
                if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                else "error",
            )
            self.audit.emit("run_interrupted", **exception_info(exc))
            raise
        finally:
            self.audit.emit(
                "run_end",
                outcome=event.get_extra("pixiv_search.outcome"),
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            RUN_ID.reset(token)

    async def _execute_inner(self, event: AstrMessageEvent, command=None, *, links=None):
        """One admission/cooldown check per message, shared by both entry points."""
        directory = None
        admitted = False
        current_task = asyncio.current_task()
        try:
            if command is None:
                command = parse_command(event.get_message_str(), self.settings.search_count)
            self.audit.emit(
                "command_parsed",
                action=command.action,
                count=command.count,
                page=command.page,
                pid=command.query if command.action in ("info", "download", "user") else None,
            )
            if command.action == "help":
                yield event.plain_result(HELP)
                return
            # Admission check has no await: simultaneous events cannot bypass it.
            if self.running >= self.settings.max_active_jobs:
                event.set_extra("pixiv_search.outcome", "busy")
                self.audit.emit("admission_busy", active=self.running)
                yield event.plain_result("Pixiv 当前任务已满，请稍后重试。")
                return
            sender_key = f"{event.unified_msg_origin}:{event.get_sender_id()}"
            now = time.monotonic()
            remaining = self.settings.cooldown_seconds - (
                now - self.last_request.get(sender_key, -1e9)
            )
            if remaining > 0:
                event.set_extra("pixiv_search.outcome", "cooldown")
                self.audit.emit("user_cooldown", seconds=round(remaining, 3))
                yield event.plain_result(f"请求过于频繁，请 {int(remaining) + 1} 秒后重试。")
                return
            self.running += 1
            admitted = True
            if current_task:
                self.jobs.add(current_task)
            self.last_request[sender_key] = now
            self._ensure_client()
            if self.client.gate.remaining > 0:
                yield event.plain_result(
                    f"上游 HTTP 429 冷却中，新网络请求预计需等待 {math.ceil(self.client.gate.remaining)} 秒；命中缓存的内容可直接复用。"
                )
            if command.action in ("tag", "user"):
                yield event.plain_result("正在搜索 Pixiv 作品…")
                if command.action == "tag":
                    works, total = await self.client.search_tag(
                        command.query, command.count, command.page
                    )
                    title = f"Tag：{command.query}"
                else:
                    works, total = await self.client.user_works(
                        command.query, command.count, command.page
                    )
                    title = f"画师 UID：{command.query}"
                if not works:
                    yield event.plain_result(
                        "本页没有可显示的作品。请检查关键词、UID、页码及账号可见权限。"
                    )
                    return
                self.audit.emit("search_results", count=len(works), total=total, page=command.page)
                for text in search_chunks(works, title, total, command.page):
                    yield event.plain_result(text)
                return
            for pid in links if links is not None else [command.query]:
                if directory is not None:
                    self.store.finish(directory)
                    directory = None
                try:
                    self.audit.emit("work_start", pid=pid, action=command.action)
                    yield event.plain_result(f"正在获取 Pixiv 作品 {pid}…")
                    work = await self.client.artwork(pid)
                    directory = self.store.create()
                    if command.action == "info":
                        yield event.plain_result(detail(work))
                        await self._preview(event, work, directory)
                    else:
                        await self._download_work(event, work, directory)
                    self.audit.emit("work_end", pid=pid, outcome="ok")
                except PixivError as exc:
                    event.set_extra("pixiv_search.outcome", "error")
                    self.audit.emit(
                        "work_end", pid=pid, outcome="error", reason=str(exc), **exception_info(exc)
                    )
                    yield event.plain_result(f"作品 {pid}：{exc}（日志任务 {RUN_ID.get()}）")
        except PixivError as exc:
            event.set_extra("pixiv_search.outcome", "error")
            self.audit.emit("run_error", reason=str(exc), **exception_info(exc))
            yield event.plain_result(f"{exc}（日志任务 {RUN_ID.get()}）")
        except Exception as exc:
            event.set_extra("pixiv_search.outcome", "error")
            self.audit.emit("run_error", **exception_info(exc))
            # HTTP exceptions can contain credential-bearing proxy URLs. Log type only.
            logger.error(f"Pixiv 插件处理失败（{type(exc).__name__}），未记录 Cookie 或请求地址。")
            yield event.plain_result(
                "Pixiv 处理失败，请检查插件配置、磁盘空间与网络连接；日志提供异常类型。"
            )
        finally:
            if directory is not None:
                self.store.finish(directory)
            if admitted:
                self.running -= 1
                self.jobs.discard(current_task)

    async def terminate(self):
        tasks = list(self.jobs)
        if self.cleaner:
            tasks.append(self.cleaner)
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(
            *(t for t in tasks if t is not asyncio.current_task()), return_exceptions=True
        )
        if self.client:
            if self.resources:
                await self.resources.close()
            await self.client.close()
        self.audit.emit("plugin_stop")
        await asyncio.to_thread(self.audit.close)
