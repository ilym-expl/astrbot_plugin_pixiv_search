"""Pixiv AJAX reader. API cookies are isolated from image downloads."""

import asyncio
import json
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

from .audit import NullAudit, exception_info
from .commands import pixiv_id
from .scheduling import MetadataCache, RequestGate, retry_after
from .settings import PixivError, Settings

MB = 1024 * 1024


async def disk_call(function, *args):
    """Wait for an in-flight disk operation even when its coroutine is cancelled.

    Cancelling to_thread does not stop the worker; closing/deleting the file before
    that worker finishes can leak handles or leave .part files on Windows.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class Retryable(Exception):
    def __init__(self, status: int, delay: float = 0):
        self.status = status
        self.delay = delay


class DownloadBudget:
    """All network bytes, including retry attempts, count towards one job's limit."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def consume(self, size: int):
        # No await between read and update: atomic on this asyncio event loop.
        self.used += size
        if self.used > self.limit:
            raise PixivError("作品总下载量超过 max_total_mb；已停止，不发送不完整压缩包。")


class PixivClient:
    def __init__(self, settings: Settings, *, api_transport=None, image_transport=None, audit=None):
        self.settings = settings
        self.audit = audit or NullAudit()
        self.gate = RequestGate(settings, self.audit)
        self.metadata = MetadataCache(settings.metadata_cache_minutes, self.audit)
        headers = {"User-Agent": settings.user_agent, "Referer": "https://www.pixiv.net/"}
        options = dict(
            timeout=httpx.Timeout(
                settings.timeout_seconds, connect=min(20, settings.timeout_seconds)
            ),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=settings.download_concurrency + 4),
        )
        if settings.proxy:
            options["proxy"] = settings.proxy
        api_headers = {
            **headers,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if settings.cookie:
            api_headers["Cookie"] = settings.cookie
        self.api = httpx.AsyncClient(headers=api_headers, transport=api_transport, **options)
        self.images = httpx.AsyncClient(headers=headers, transport=image_transport, **options)
        self.api_slots = asyncio.Semaphore(1)
        self.download_slots = asyncio.Semaphore(settings.download_concurrency)

    async def close(self):
        await self.metadata.close()
        await asyncio.gather(self.api.aclose(), self.images.aclose())

    @staticmethod
    def check_status(response: httpx.Response):
        status = response.status_code
        if status == 429 or 500 <= status <= 599:
            raise Retryable(status, retry_after(response.headers.get("Retry-After", "")))
        if status in (401, 403):
            raise PixivError(
                f"Pixiv 拒绝访问（HTTP {status}）。请检查 Cookie、账号权限、代理和图片 Referer。"
            )
        if status == 404:
            raise PixivError("作品或用户不存在、已删除，或当前账号不可见（404）。")
        if 300 <= status < 400:
            raise PixivError("接口返回重定向；请检查登录 Cookie 或反向代理地址。")
        if status < 200 or status >= 300:
            raise PixivError(f"Pixiv 请求失败（HTTP {status}）。")

    @asynccontextmanager
    async def _request(self, lane, url, *, endpoint, params=None):
        slots = self.api_slots if lane == "api" else self.download_slots
        client = self.api if lane == "api" else self.images
        async with slots:
            await self.gate.wait(lane)
            request_id = uuid4().hex[:12]
            started = time.monotonic()
            self.audit.emit("request_start", request_id=request_id, lane=lane, endpoint=endpoint)
            try:
                async with client.stream("GET", url, params=params) as response:
                    self.audit.emit(
                        "request_status",
                        request_id=request_id,
                        lane=lane,
                        status=response.status_code,
                    )
                    try:
                        self.check_status(response)
                    except Retryable as exc:
                        if exc.status == 429:
                            exc.delay = max(exc.delay, self.settings.rate_limit_cooldown_seconds)
                            await self.gate.pause(exc.delay)
                        raise
                    yield response
            except BaseException as exc:
                self.audit.emit(
                    "request_error", request_id=request_id, lane=lane, **exception_info(exc)
                )
                raise
            finally:
                self.audit.emit(
                    "request_end",
                    request_id=request_id,
                    lane=lane,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )

    @staticmethod
    def transport_message(exc):
        if isinstance(exc, httpx.ProxyError):
            return "代理连接失败"
        if isinstance(exc, httpx.TimeoutException):
            return f"网络超时（{type(exc).__name__}）"
        if isinstance(exc, httpx.ConnectError):
            return "网络连接失败（请检查 DNS、TLS 和代理连接）"
        return f"网络传输失败（{type(exc).__name__}）"

    async def _retry(self, operation, lane="api"):
        for attempt in range(self.settings.retries + 1):
            self.audit.emit(
                "request_attempt",
                lane=lane,
                attempt=attempt + 1,
                max_attempts=self.settings.retries + 1,
            )
            try:
                return await operation()
            except (Retryable, httpx.TransportError) as exc:
                if attempt >= self.settings.retries:
                    if isinstance(exc, Retryable):
                        if exc.status == 429:
                            message = f"{lane} 服务返回 HTTP 429（频率受限），后续网络请求统一暂停约 {math.ceil(self.gate.remaining)} 秒。"
                        else:
                            message = (
                                f"{lane} 服务返回 HTTP {exc.status}（服务端错误），重试已用尽。"
                            )
                    else:
                        message = self.transport_message(exc) + "，重试已用尽。"
                    self.audit.emit(
                        "retries_exhausted",
                        lane=lane,
                        status=getattr(exc, "status", None),
                        **exception_info(exc),
                    )
                    raise PixivError(message) from None
                delay = (
                    0
                    if isinstance(exc, Retryable) and exc.status == 429
                    else max(
                        min(self.settings.retry_backoff_seconds * 2**attempt, 60),
                        getattr(exc, "delay", 0),
                    )
                )
                self.audit.emit(
                    "request_retry",
                    lane=lane,
                    attempt=attempt + 1,
                    backoff_seconds=delay,
                    status=getattr(exc, "status", None),
                    **exception_info(exc),
                )
                if delay:
                    await self.gate.sleep(delay)

    async def ajax(self, path: str, params=None):
        key = json.dumps([path, params], ensure_ascii=False, sort_keys=True)
        return await self.metadata.get(key, lambda: self._ajax(path, params))

    async def _ajax(self, path: str, params=None):
        url = self.settings.api_base_url + "/ajax/" + path.lstrip("/")
        endpoint = "search/artworks/<keyword>" if path.startswith("search/") else path

        async def operation():
            async with self._request("api", url, endpoint=endpoint, params=params) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 16 * MB:
                        raise PixivError("Pixiv 接口响应过大，已停止读取。")
                try:
                    payload = json.loads(content)
                except (ValueError, UnicodeError):
                    raise PixivError("接口未返回 JSON，可能遇到登录页或反爬验证。") from None
                if not isinstance(payload, dict) or "error" not in payload:
                    raise PixivError("接口返回格式不兼容，请检查 api_base_url。")
                if payload.get("error"):
                    self.audit.emit("api_payload_error", endpoint=endpoint)
                    # Never echo arbitrary upstream messages or HTML to a group.
                    raise PixivError(
                        "Pixiv 接口报告错误：内容不可见、Cookie 失效或请求参数被拒绝。"
                    )
                if payload.get("body") is None:
                    raise PixivError("Pixiv 接口返回空数据。")
                self.audit.emit("api_json_read", endpoint=endpoint, bytes=len(content))
                return payload["body"]

        return await self._retry(operation)

    async def artwork(self, pid: str) -> dict:
        pid = pixiv_id(pid)
        body = await self.ajax(f"illust/{pid}", {"full": "1", "lang": "zh"})
        if not isinstance(body, dict) or str(body.get("id", "")) != pid:
            raise PixivError("作品详情格式不兼容。")
        return body

    async def pages(self, pid: str) -> list[dict]:
        body = await self.ajax(f"illust/{pixiv_id(pid)}/pages", {"lang": "zh"})
        if not isinstance(body, list) or not body:
            raise PixivError("作品没有可下载的页面。")
        if len(body) > self.settings.max_pages:
            raise PixivError("作品页数超过 max_pages 配置，请调整限制后重试。")
        if any(not isinstance(p, dict) or not p.get("urls", {}).get("original") for p in body):
            raise PixivError("部分页面缺少原图地址，已停止，避免发送不完整作品。")
        return body

    async def search_tag(self, keyword: str, count: int, page: int) -> tuple[list[dict], int]:
        body = await self.ajax(
            "search/artworks/" + quote(keyword, safe=""),
            {
                "word": keyword,
                "p": str(page),
                "s_mode": self.settings.tag_match,
                "mode": self.settings.search_mode,
                "order": "date_d",
                "type": "all",
                "lang": "zh",
            },
        )
        block = body.get("illustManga", {})
        if not isinstance(block, dict) or not isinstance(block.get("data"), list):
            raise PixivError("Pixiv 搜索响应格式变化，请检查插件更新。")
        works = [
            w
            for w in block["data"]
            if isinstance(w, dict)
            and str(w.get("id", "")).isascii()
            and str(w.get("id", "")).isdigit()
        ]
        return works[:count], int(block.get("total", len(works)))

    async def user_works(self, uid: str, count: int, page: int) -> tuple[list[dict], int]:
        uid = pixiv_id(uid, "users")
        body = await self.ajax(f"user/{uid}/profile/all", {"lang": "zh"})
        ids = set()
        for category in ("illusts", "manga"):
            entries = body.get(category) or {}
            if isinstance(entries, dict):
                ids.update(k for k in entries if re_id(k))
        ordered = sorted(ids, key=int, reverse=True)
        selected = ordered[(page - 1) * count : page * count]
        if not selected:
            return [], len(ordered)
        params = [("ids[]", pid) for pid in selected]
        params.extend(
            [
                ("work_category", "illustManga"),
                ("is_first_page", "1" if page == 1 else "0"),
                ("lang", "zh"),
            ]
        )
        details = await self.ajax(f"user/{uid}/profile/illusts", params)
        works = details.get("works") or {}
        if not isinstance(works, dict):
            raise PixivError("画师作品列表格式不兼容。")
        return [works[pid] for pid in selected if isinstance(works.get(pid), dict)], len(ordered)

    def image_url(self, url: str) -> str:
        p = urlsplit(url)
        # Returned URLs may be remote-controlled. Accept only Pixiv's image CDN;
        # administrators can explicitly route it through their own image gateway.
        if (
            p.scheme != "https"
            or not p.hostname
            or not (p.hostname == "pximg.net" or p.hostname.endswith(".pximg.net"))
            or p.username
            or p.port not in (None, 443)
        ):
            raise PixivError("接口返回了非 Pixiv 图片地址，已拒绝下载。")
        if self.settings.image_base_url:
            return self.settings.image_base_url + p.path + ("?" + p.query if p.query else "")
        return url

    async def download(
        self, url: str, target: Path, budget: DownloadBudget, *, archive: bool = False
    ):
        url = self.image_url(url)
        partial = target.with_suffix(target.suffix + ".part")

        async def operation():
            try:
                async with self._request("image", url, endpoint=target.name) as response:
                    size_header = response.headers.get("Content-Length", "")
                    if size_header.isdigit() and int(size_header) > self.settings.max_file_mb * MB:
                        raise PixivError("单文件超过 max_file_mb，已停止下载。")
                    content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
                    allowed = (
                        (
                            "application/zip",
                            "application/x-zip-compressed",
                            "application/octet-stream",
                        )
                        if archive
                        else ("application/octet-stream",)
                    )
                    if (
                        not (content_type.startswith("image/") and not archive)
                        and content_type not in allowed
                    ):
                        raise PixivError("图片接口返回了非图片/ZIP 内容，请检查图片反向代理。")
                    total = 0
                    # Opening a local file is brief and synchronous: cancellation
                    # cannot lose a handle between the worker opening and returning it.
                    output = partial.open("wb")
                    try:
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            total += len(chunk)
                            budget.consume(len(chunk))
                            if total > self.settings.max_file_mb * MB:
                                raise PixivError("单文件超过 max_file_mb，已停止下载。")
                            await disk_call(output.write, chunk)
                    finally:
                        await disk_call(output.close)
                    if not total:
                        raise PixivError("图片接口返回空文件。")
                    await disk_call(partial.replace, target)
                    self.audit.emit("image_downloaded", file=target.name, bytes=total)
                    return target
            finally:
                partial.unlink(missing_ok=True)

        return await self._retry(operation, "image")

    async def download_all(self, pid: str, pages: list[dict], directory: Path) -> list[Path]:
        budget = DownloadBudget(self.settings.max_total_mb * MB)
        downloads = []
        for index, page in enumerate(pages):
            url = page["urls"]["original"]
            suffix = Path(urlsplit(url).path).suffix.lower()
            if suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"):
                raise PixivError("原图文件格式暂不支持。")
            self.image_url(url)
            downloads.append((url, directory / f"{pid}_p{index:04d}{suffix}"))
        tasks = [asyncio.create_task(self.download(url, path, budget)) for url, path in downloads]
        try:
            # gather preserves page order even when downloads finish out of order.
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise


def re_id(value) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit() and 0 < len(value) <= 16
