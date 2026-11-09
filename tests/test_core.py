import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_pixiv_search.client import MB, DownloadBudget, PixivClient
from astrbot_plugin_pixiv_search.commands import artwork_links, parse_command
from astrbot_plugin_pixiv_search.formatting import caption, detail, search_chunks
from astrbot_plugin_pixiv_search.settings import PixivError, Settings
from astrbot_plugin_pixiv_search.storage import JobStore, pack_zip


def quick_settings(**values):
    return Settings(**({"api_interval_ms": 0, "image_interval_ms": 0} | values))


def response(body):
    return httpx.Response(200, json={"error": False, "body": body})


def work(pid="123", pages=1):
    return {
        "id": pid,
        "title": "测试作品",
        "description": "<p>简介 &amp; 内容</p>",
        "userId": "456",
        "userName": "画师",
        "pageCount": pages,
        "illustType": 0,
        "tags": {"tags": [{"tag": "测试"}]},
        "urls": {"original": "https://i.pximg.net/123_p0.png"},
    }


class CommandTests(unittest.TestCase):
    def test_auto_link_extraction_handles_wrappers_queries_and_duplicates(self):
        text = (
            "看看（https://www.pixiv.net/artworks/123?utm_source=share），"
            "[作品](https://pixiv.net/en/artworks/124)。\n"
            "www.pixiv.net/artworks/123 "
            "https://www.pixiv.net/member_illust.php?mode=medium&amp;illust_id=125 "
            "pixiv.net/artworks/126!"
        )
        self.assertEqual(artwork_links(text), ["123", "124", "125", "126"])

    def test_auto_links_reject_fake_hosts_invalid_paths_and_non_artwork_pages(self):
        text = (
            "https://www.pixiv.net.evil.com/artworks/123 "
            "https://evil.example/?next=https://pixiv.net/artworks/456 "
            "https://www.pixiv.net/users/456 https://pixiv.net/artworks/123/more "
            "https://pixiv.net/artworks/0 https://pixiv.net/artworks/１２３ "
            "fakepixiv.net/artworks/789"
        )
        self.assertEqual(artwork_links(text), [])

    def test_auto_settings_defaults_and_bounds(self):
        with patch.dict(os.environ, {"PIXIV_COOKIE": ""}):
            self.assertTrue(Settings.from_mapping({}).auto_parse_links)
            self.assertEqual(Settings.from_mapping({}).auto_max_links, 3)
            self.assertFalse(Settings.from_mapping({"auto_parse_links": False}).auto_parse_links)
            for config in (
                {"auto_parse_links": "false"},
                {"auto_max_links": 0},
                {"auto_max_links": 11},
            ):
                with self.assertRaises(PixivError):
                    Settings.from_mapping(config)

    def test_required_commands_and_links(self):
        self.assertEqual(parse_command("/p 搜 123").action, "info")
        self.assertEqual(parse_command("p 123").action, "download")
        self.assertEqual(parse_command("/p https://www.pixiv.net/en/artworks/123").query, "123")
        self.assertEqual(
            parse_command(
                "p 搜 https://www.pixiv.net/member_illust.php?mode=medium&illust_id=123"
            ).query,
            "123",
        )
        c = parse_command('/p tag "初音 ミク" --count=30 --page 2')
        self.assertEqual((c.query, c.count, c.page), ("初音 ミク", 30, 2))
        c = parse_command("/p 用户 https://www.pixiv.net/users/456 --count 20")
        self.assertEqual((c.action, c.query, c.count), ("user", "456", 20))
        self.assertEqual(parse_command("/p tag foo", 25).count, 25)
        self.assertEqual(parse_command("/p").action, "help")

    def test_invalid_input(self):
        for text in (
            "/p 0",
            "/p -1",
            "/p 搜",
            "/p tag x --count 9",
            "/p tag x --count 31",
            "/p tag x --page 0",
            "/p tag x --count",
            "/p tag x --unknown 1",
            "/p tag x --count 10 --count 20",
            '/p tag "broken',
            "/p 用户 nickname",
            "/p https://evil.example/artworks/123",
            "/p １２３",
        ):
            with self.subTest(text=text), self.assertRaises(PixivError):
                parse_command(text)

    def test_cookie_priority_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PIXIV_COOKIE": ""}):
            file = Path(tmp) / "cookie.txt"
            file.write_text("file_session", encoding="utf-8")
            self.assertEqual(
                Settings.from_mapping(
                    {"cookie_file": str(file), "cookie": "config_session"}
                ).cookie,
                "PHPSESSID=file_session",
            )
            with patch.dict(os.environ, {"PIXIV_COOKIE": "env_session"}):
                self.assertEqual(
                    Settings.from_mapping({"cookie_file": str(file)}).cookie,
                    "PHPSESSID=env_session",
                )
            self.assertEqual(
                Settings.from_mapping({"cookie": "Cookie: PHPSESSID=abc; foo=bar"}).cookie,
                "PHPSESSID=abc; foo=bar",
            )
            with self.assertRaises(PixivError):
                Settings.from_mapping({"cookie": "secret\nvalue"})
            with self.assertRaises(PixivError):
                Settings.from_mapping({"direct_send_limit": -1})

    def test_caption_and_search_output(self):
        self.assertEqual(caption("<p>A&amp;B<br>C</p><script>secret</script>"), "A&B\nC")
        self.assertIn("简介 & 内容", detail(work()))
        self.assertEqual(len(search_chunks([work(str(i)) for i in range(30)], "tag", 30, 1)), 3)


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, data=b"\x89PNG\r\n\x1a\ntest", delay=0.002):
        self.data = data
        self.delay = delay

    async def __aiter__(self):
        await asyncio.sleep(self.delay)
        yield self.data


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_encoding_ads_and_user_pagination(self):
        requests = []

        def handle(request):
            requests.append(request)
            path = request.url.path
            if "/search/" in path:
                return response(
                    {
                        "illustManga": {
                            "data": [{"isAdContainer": True}] + [work(str(i)) for i in range(30)],
                            "total": 100,
                        }
                    }
                )
            if path.endswith("/profile/all"):
                return response(
                    {"illusts": {str(i): None for i in range(1, 26)}, "manga": {"26": None}}
                )
            if path.endswith("/profile/illusts"):
                ids = request.url.params.get_list("ids[]")
                return response({"works": {pid: work(pid) for pid in ids}})
            return httpx.Response(404)

        c = PixivClient(quick_settings(retries=0), api_transport=httpx.MockTransport(handle))
        try:
            works, total = await c.search_tag("初音 / #?", 20, 2)
            self.assertEqual((len(works), total), (20, 100))
            self.assertEqual(requests[0].url.params["word"], "初音 / #?")
            self.assertIn("%2F", requests[0].url.raw_path.decode())
            works, total = await c.user_works("456", 10, 2)
            self.assertEqual([w["id"] for w in works], [str(i) for i in range(16, 6, -1)])
            self.assertEqual(total, 26)
            self.assertEqual(requests[-1].url.params["work_category"], "illustManga")
            self.assertEqual(await c.user_works("456", 30, 2), ([], 26))
        finally:
            await c.close()

    async def test_credentials_isolation_and_image_gateway(self):
        seen = []

        def api(request):
            seen.append(request)
            return response(work())

        def image(request):
            seen.append(request)
            return httpx.Response(
                200, content=b"original bytes", headers={"Content-Type": "image/png"}
            )

        settings = quick_settings(
            cookie="PHPSESSID=private", image_base_url="https://gateway.example/pixiv"
        )
        c = PixivClient(
            settings,
            api_transport=httpx.MockTransport(api),
            image_transport=httpx.MockTransport(image),
        )
        try:
            await c.artwork("123")
            with tempfile.TemporaryDirectory() as tmp:
                await c.download(
                    "https://i.pximg.net/img/a.png?x=1", Path(tmp) / "a.png", DownloadBudget(MB)
                )
            self.assertEqual(seen[0].headers["cookie"], "PHPSESSID=private")
            self.assertNotIn("cookie", seen[1].headers)
            self.assertEqual(str(seen[1].url), "https://gateway.example/pixiv/img/a.png?x=1")
            self.assertEqual(seen[1].headers["referer"], "https://www.pixiv.net/")
            for url in (
                "http://i.pximg.net/a.png",
                "https://pximg.net.evil.com/a.png",
                "https://127.0.0.1/a.png",
                "https://user@i.pximg.net/a.png",
            ):
                with self.assertRaises(PixivError):
                    c.image_url(url)
        finally:
            await c.close()

    async def test_parallel_download_is_bounded_ordered_and_zip_is_lossless(self):
        active, peak = 0, 0

        async def handle(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(
                200,
                stream=SlowStream(request.url.path.encode()),
                headers={"Content-Type": "image/png"},
            )

        c = PixivClient(
            quick_settings(download_concurrency=3), image_transport=httpx.MockTransport(handle)
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                pages = [{"urls": {"original": f"https://i.pximg.net/{i}.png"}} for i in range(8)]
                images = await c.download_all("123", pages, directory)
                self.assertGreater(peak, 1)
                self.assertLessEqual(peak, 3)
                self.assertEqual(
                    [p.read_bytes() for p in images], [f"/{i}.png".encode() for i in range(8)]
                )
                archive = pack_zip(directory, "123", images, "作品说明", work(pages=8))
                with zipfile.ZipFile(archive) as z:
                    self.assertEqual(len(z.namelist()), 10)
                    self.assertIsNone(z.testzip())
                    for path in images:
                        self.assertEqual(z.read(path.name), path.read_bytes())
                    self.assertEqual(json.loads(z.read("artwork.json"))["id"], "123")
        finally:
            await c.close()

    async def test_retry_429_then_success_and_403_no_retry(self):
        calls = 0

        def handle(request):
            nonlocal calls
            calls += 1
            return httpx.Response(429) if calls == 1 else response(work())

        c = PixivClient(quick_settings(retries=2), api_transport=httpx.MockTransport(handle))
        try:
            clock = [0.0]

            async def advance(seconds):
                clock[0] += seconds

            c.gate.clock = lambda: clock[0]
            c.gate.sleep = advance
            self.assertEqual((await c.artwork("123"))["id"], "123")
            self.assertEqual(calls, 2)
        finally:
            await c.close()
        calls = 0

        def denied(request):
            nonlocal calls
            calls += 1
            return httpx.Response(403, text="secret cookie")

        c = PixivClient(quick_settings(retries=2), api_transport=httpx.MockTransport(denied))
        try:
            with self.assertRaises(PixivError) as error:
                await c.artwork("123")
            self.assertEqual(calls, 1)
            self.assertNotIn("secret", str(error.exception))
        finally:
            await c.close()

    async def test_size_limits_and_no_partial_files(self):
        for headers, data, budget in (
            ({"Content-Type": "image/png", "Content-Length": str(2 * MB)}, b"x", 10 * MB),
            ({"Content-Type": "image/png"}, b"x" * (MB + 1), 10 * MB),
            ({"Content-Type": "image/png"}, b"x" * 100, 50),
            ({"Content-Type": "text/html"}, b"<html>login</html>", 10 * MB),
        ):
            with self.subTest(headers=headers, size=len(data)):
                c = PixivClient(
                    quick_settings(max_file_mb=1, retries=0),
                    image_transport=httpx.MockTransport(
                        lambda r: httpx.Response(200, stream=SlowStream(data), headers=headers)
                    ),
                )
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp) / "image.png"
                        with self.assertRaises(PixivError):
                            await c.download(
                                "https://i.pximg.net/x.png", path, DownloadBudget(budget)
                            )
                        self.assertFalse(path.exists())
                        self.assertFalse(list(Path(tmp).glob("*.part")))
                finally:
                    await c.close()

    async def test_peer_download_failure_cancels_all_workers(self):
        async def handle(request):
            if request.url.path == "/bad.png":
                return httpx.Response(404)
            await asyncio.sleep(0.05)
            return httpx.Response(200, content=b"data", headers={"Content-Type": "image/png"})

        c = PixivClient(quick_settings(retries=0), image_transport=httpx.MockTransport(handle))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pages = [
                    {"urls": {"original": f"https://i.pximg.net/{x}.png"}}
                    for x in ("slow", "bad", "slow2")
                ]
                with self.assertRaises(PixivError):
                    await c.download_all("123", pages, Path(tmp))
                await asyncio.sleep(0.06)
                self.assertEqual(list(Path(tmp).iterdir()), [])
        finally:
            await c.close()

    async def test_non_json_and_upstream_error(self):
        for result in (
            httpx.Response(200, text="<html>login</html>"),
            response(None),
            httpx.Response(200, json={"error": True, "message": "secret"}),
        ):
            c = PixivClient(
                quick_settings(retries=0), api_transport=httpx.MockTransport(lambda r: result)
            )
            try:
                with self.assertRaises(PixivError) as error:
                    await c.artwork("123")
                self.assertNotIn("secret", str(error.exception))
            finally:
                await c.close()


class StorageTests(unittest.TestCase):
    def test_cleanup_ignores_active_jobs_and_removes_expired_finished_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp), 5)
            active = store.create()
            expired = store.create()
            store.finish(expired)
            old = time.time() - 1000
            os.utime(active, (old, old))
            os.utime(expired, (old, old))
            unrelated = Path(tmp) / "unrelated"
            unrelated.mkdir()
            store.clean()
            self.assertTrue(active.exists())
            self.assertFalse(expired.exists())
            self.assertTrue(unrelated.exists())
            store.finish(active)
            self.assertGreater(active.stat().st_mtime, old)


if __name__ == "__main__":
    unittest.main()
