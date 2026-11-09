"""Pacing, cooldown, cache, cancellation and logging regression tests."""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_pixiv_search.audit import RUN_ID, Audit, NullAudit, exception_info
from astrbot_plugin_pixiv_search.client import MB, DownloadBudget, PixivClient
from astrbot_plugin_pixiv_search.resources import Resources
from astrbot_plugin_pixiv_search.scheduling import MetadataCache, RequestGate, retry_after
from astrbot_plugin_pixiv_search.settings import PixivError, Settings


def settings(**overrides):
    return Settings(**({"api_interval_ms": 0, "image_interval_ms": 0, "retries": 0} | overrides))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_and_images_have_independent_spacing_and_shared_pause(self):
        clock = Clock()
        gate = RequestGate(
            settings(api_interval_ms=1500, image_interval_ms=500), clock=clock, sleep=clock.sleep
        )
        await gate.wait("api")
        await gate.wait("image")
        self.assertEqual(clock.now, 0)
        await gate.wait("image")
        self.assertEqual(clock.now, 0.5)
        await gate.wait("api")
        self.assertEqual(clock.now, 1.5)
        await gate.pause(100)
        await gate.wait("image")
        self.assertEqual(clock.now, 101.5)
        await gate.wait("api")
        self.assertEqual(clock.now, 101.5)
        await gate.pause(20)
        await gate.pause(5)
        self.assertEqual(gate.remaining, 20)

    async def test_retry_after_supports_dates_long_values_and_invalid_values(self):
        self.assertEqual(retry_after("120"), 120)
        self.assertEqual(retry_after("Thu, 01 Jan 1970 00:02:00 GMT", now=0), 120)
        for value in ("invalid", "NaN", "inf", "-1", ""):
            self.assertEqual(retry_after(value), 0)

    async def test_429_from_api_blocks_new_image_requests_and_reports_real_status(self):
        clock = Clock()
        calls = []

        def api(request):
            calls.append(("api", clock.now))
            return httpx.Response(429, headers={"Retry-After": "240"})

        def image(request):
            calls.append(("image", clock.now))
            return httpx.Response(200, content=b"png bytes", headers={"Content-Type": "image/png"})

        client = PixivClient(
            settings(),
            api_transport=httpx.MockTransport(api),
            image_transport=httpx.MockTransport(image),
        )
        client.gate.clock, client.gate.sleep = clock, clock.sleep
        try:
            with self.assertRaises(PixivError) as error:
                await client.artwork("123")
            self.assertIn("HTTP 429", str(error.exception))
            self.assertEqual(client.gate.remaining, 240)
            with tempfile.TemporaryDirectory() as tmp:
                await client.download(
                    "https://i.pximg.net/a.png", Path(tmp) / "a.png", DownloadBudget(MB)
                )
            self.assertEqual(calls, [("api", 0), ("image", 240)])
        finally:
            await client.close()

    async def test_network_proxy_and_5xx_errors_are_not_reported_as_rate_limits(self):
        for failure, expected in (
            (httpx.ReadTimeout("private"), "ReadTimeout"),
            (httpx.ProxyError("private"), "代理连接失败"),
            (httpx.ConnectError("private"), "连接失败"),
            (503, "HTTP 503"),
        ):

            def handle(request):
                if isinstance(failure, int):
                    return httpx.Response(failure)
                raise failure

            client = PixivClient(settings(), api_transport=httpx.MockTransport(handle))
            try:
                with self.assertRaises(PixivError) as error:
                    await client.artwork("123")
                message = str(error.exception)
                self.assertIn(expected, message)
                self.assertNotIn("频率受限", message)
                self.assertNotIn("private", message)
            finally:
                await client.close()

    async def test_metadata_join_survives_one_waiter_cancel_and_ttl_expiry(self):
        clock = Clock()
        cache = MetadataCache(1, clock=clock)
        release = asyncio.Event()
        calls = 0

        async def load():
            nonlocal calls
            calls += 1
            await release.wait()
            return {"value": [1]}

        first = asyncio.create_task(cache.get("same", load))
        second = asyncio.create_task(cache.get("same", load))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        value = await second
        value["value"].append(2)
        self.assertEqual(await cache.get("same", load), {"value": [1]})
        self.assertEqual(calls, 1)
        clock.now = 61
        await cache.get("same", load)
        self.assertEqual(calls, 2)
        await cache.close()


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api_calls = 0
        self.image_calls = 0
        self.fail = False
        self.release = None
        self.work = {
            "id": "123",
            "pageCount": 2,
            "illustType": 0,
            "title": "test",
            "urls": {"original": "https://i.pximg.net/0.png"},
        }

        def api(request):
            self.api_calls += 1
            return httpx.Response(
                200,
                json={
                    "error": False,
                    "body": [
                        {"urls": {"original": f"https://i.pximg.net/{i}.png"}} for i in range(2)
                    ],
                },
            )

        async def image(request):
            self.image_calls += 1
            if self.release:
                await self.release.wait()
            if self.fail:
                return httpx.Response(404)
            return httpx.Response(
                200, content=request.url.path.encode(), headers={"Content-Type": "image/png"}
            )

        self.client = PixivClient(
            settings(),
            api_transport=httpx.MockTransport(api),
            image_transport=httpx.MockTransport(image),
        )
        self.cache = Resources(Path(self.tmp.name) / "assets", self.client, settings(), NullAudit())

    async def asyncTearDown(self):
        await self.cache.close()
        await self.client.close()
        self.tmp.cleanup()

    async def test_originals_and_zip_reused_and_survive_new_cache_instance(self):
        async with self.cache.lease(self.work) as first:
            original = first.images[0].read_bytes()
            archive = first.archive.read_bytes()
        replacement = Resources(self.cache.root, self.client, settings(), NullAudit())
        async with replacement.lease(self.work) as second:
            self.assertEqual(second.images[0].read_bytes(), original)
            self.assertEqual(second.archive.read_bytes(), archive)
        self.assertEqual((self.api_calls, self.image_calls), (1, 2))
        await replacement.close()

    async def test_same_work_producer_is_shared_and_cancelled_waiter_does_not_break_it(self):
        self.release = asyncio.Event()

        async def consume():
            async with self.cache.lease(self.work) as bundle:
                return bundle.archive

        first = asyncio.create_task(consume())
        second = asyncio.create_task(consume())
        while self.image_calls == 0:
            await asyncio.sleep(0.001)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.release.set()
        self.assertTrue((await second).is_file())
        self.assertEqual((self.api_calls, self.image_calls), (1, 2))
        self.assertEqual(self.cache.pins, {})

    async def test_expired_pinned_assets_are_not_deleted_and_corruption_rebuilds(self):
        async with self.cache.lease(self.work) as bundle:
            directory = bundle.archive.parent
            old = time.time() - 100000
            os.utime(directory, (old, old))
            await self.cache.clean()
            self.assertTrue(bundle.archive.exists())
        bundle.images[0].write_bytes(b"corrupt size changed")
        async with self.cache.lease(self.work) as repaired:
            self.assertEqual(repaired.images[0].read_bytes(), b"/0.png")
        self.assertEqual(self.image_calls, 4)
        os.utime(directory, (old, old))
        await self.cache.clean()
        self.assertFalse(directory.exists())

    async def test_failed_build_has_no_complete_marker_and_can_retry(self):
        self.fail = True
        with self.assertRaises(PixivError):
            async with self.cache.lease(self.work):
                pass
        self.assertFalse(list(self.cache.root.rglob("complete.json")))
        self.fail = False
        async with self.cache.lease(self.work) as bundle:
            self.assertTrue(bundle.archive.exists())

    async def test_lower_page_limit_cannot_be_bypassed_by_an_existing_cache(self):
        async with self.cache.lease(self.work):
            pass
        restricted = Resources(self.cache.root, self.client, settings(max_pages=1), NullAudit())
        try:
            with self.assertRaises(PixivError):
                async with restricted.lease(self.work):
                    pass
        finally:
            await restricted.close()


class AuditTests(unittest.TestCase):
    def test_logs_are_json_correlated_and_do_not_expose_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings(
                cookie="PHPSESSID=private-session; token=private-token",
                proxy="http://my-user:my-password@proxy.example:7890",
            )
            audit = Audit(Path(tmp), cfg)
            token = RUN_ID.set("test-run-id")
            try:
                audit.emit("run_start")
                audit.emit(
                    "test_redaction", message="private-session my-password my-user private-token"
                )
                try:
                    raise httpx.ProxyError("http://my-user:my-password@proxy.example:7890")
                except httpx.ProxyError as exc:
                    audit.emit("request_error", **exception_info(exc))
                audit.emit("run_end", outcome="ok")
            finally:
                RUN_ID.reset(token)
                audit.close()
            content = (Path(tmp) / "pixiv.jsonl").read_text(encoding="utf-8")
            for secret in ("private-session", "private-token", "my-password", "my-user"):
                self.assertNotIn(secret, content)
            records = [json.loads(line) for line in content.splitlines()]
            self.assertTrue(all(r["run_id"] == "test-run-id" for r in records))
            self.assertIn("frames", records[2])
            self.assertEqual(records[-1]["event"], "run_end")

    def test_log_rotation_has_bounded_backup_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Audit(Path(tmp), settings(log_backups=2))
            audit.handler.maxBytes = 400
            for index in range(30):
                audit.emit("rotation_test", index=index, padding="x" * 100)
            audit.close()
            files = list(Path(tmp).glob("pixiv.jsonl*"))
            self.assertEqual(len(files), 3)
            for path in files:
                for line in path.read_text(encoding="utf-8").splitlines():
                    json.loads(line)


if __name__ == "__main__":
    unittest.main()
