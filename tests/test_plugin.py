"""Contract tests for command routing and media types, using small AstrBot stubs.

These do not replace a real AstrBot/platform integration test.
"""

import importlib
import json
import logging
import sys
import tempfile
import types
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_pixiv_search.client import PixivClient


class FakeStar:
    def __init__(self, context):
        self.context = context


class FakeImage:
    @staticmethod
    def fromFileSystem(path):
        return {"type": "image", "path": path}


class FakeFile(dict):
    def __init__(self, name, file):
        super().__init__(type="file", path=file, name=name)


class FakeEvent:
    unified_msg_origin = "test:private:456"

    def __init__(self, text):
        self.text = text
        self.sent = []
        self.extras = {}
        self.sender_id = "456"

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return self.sender_id

    def get_self_id(self):
        return "bot123"

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def plain_result(self, text):
        return {"text": text}

    def chain_result(self, chain):
        return {"chain": chain}

    async def send(self, result):
        self.sent.append(result)
        for item in result.get("chain", []):
            if item["type"] in ("file", "image"):
                assert Path(item["path"]).is_file(), "media deleted before send"


def load_entrypoint():
    modules = {
        name: types.ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.star",
            "astrbot.api.message_components",
            "astrbot.core",
            "astrbot.core.utils",
            "astrbot.core.utils.astrbot_path",
        )
    }
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("pixiv_test")
    modules["astrbot.api.event"].AstrMessageEvent = FakeEvent
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=lambda _: lambda f: f,
        event_message_type=lambda _: lambda f: f,
        EventMessageType=types.SimpleNamespace(ALL="all"),
    )
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = FakeStar
    modules["astrbot.api.message_components"].Image = FakeImage
    modules["astrbot.api.message_components"].File = FakeFile
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: "."
    with patch.dict(sys.modules, modules):
        return importlib.import_module("astrbot_plugin_pixiv_search.main")


entrypoint = load_entrypoint()


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root_patch = patch.object(
            entrypoint, "get_astrbot_data_path", return_value=self.tmp.name
        )
        self.root_patch.start()
        self.plugin = entrypoint.PixivSearchPlugin(
            object(),
            {
                "direct_send_limit": 2,
                "cooldown_seconds": 0,
                "retries": 0,
                "api_interval_ms": 0,
                "image_interval_ms": 0,
            },
        )
        self.count = 1
        self.ugoira = False
        self.bad_page = False
        self.requested_ids = []
        self.image_calls = 0
        self.denied_ids = set()

        def api(request):
            path = request.url.path
            if path.endswith("/ugoira_meta"):
                data = {
                    "originalSrc": "https://i.pximg.net/frames.zip",
                    "frames": [{"file": "0.jpg", "delay": 100}],
                }
            elif path.endswith("/pages"):
                data = [
                    {"urls": {"original": f"https://i.pximg.net/{i}.png"}}
                    for i in range(self.count)
                ]
            else:
                pid = path.rsplit("/", 1)[-1]
                self.requested_ids.append(pid)
                if pid in self.denied_ids:
                    return httpx.Response(404)
                data = {
                    "id": pid,
                    "title": "title",
                    "userId": "456",
                    "userName": "author",
                    "pageCount": self.count,
                    "illustType": 2 if self.ugoira else 0,
                    "urls": {"original": "https://i.pximg.net/0.png"},
                }
            return httpx.Response(200, json={"error": False, "body": data})

        def image(request):
            self.image_calls += 1
            if self.bad_page and request.url.path == "/1.png":
                return httpx.Response(404)
            is_zip = request.url.path.endswith(".zip")
            return httpx.Response(
                200,
                content=b"source frames" if is_zip else request.url.path.encode(),
                headers={"Content-Type": "application/zip" if is_zip else "image/png"},
            )

        self.plugin.client = PixivClient(
            self.plugin.settings,
            api_transport=httpx.MockTransport(api),
            image_transport=httpx.MockTransport(image),
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def run_command(self, text):
        event = FakeEvent(text)
        async for result in self.plugin.pixiv(event):
            event.sent.append(result)
        return event

    def media(self, event):
        return [c for message in event.sent for c in message.get("chain", [])]

    async def run_auto(self, text, event=None):
        event = event or FakeEvent(text)
        async for result in self.plugin.auto_pixiv(event):
            event.sent.append(result)
        return event

    async def test_auto_links_use_same_direct_and_zip_threshold(self):
        self.count = 2
        event = await self.run_auto("看看这张 https://www.pixiv.net/artworks/123?foo=1")
        self.assertEqual([m["name"] for m in self.media(event)], ["123_p0000.png", "123_p0001.png"])
        self.count = 3
        event = await self.run_auto("https://www.pixiv.net/artworks/124")
        media = self.media(event)
        self.assertEqual([m["name"] for m in media], ["pixiv_124.zip"])
        with zipfile.ZipFile(media[0]["path"]) as z:
            self.assertEqual(len([n for n in z.namelist() if n.endswith(".png")]), 3)

    async def test_auto_no_double_processing_of_commands_in_either_order(self):
        event = FakeEvent("/p 搜 https://www.pixiv.net/artworks/123")
        await self.run_auto("", event)
        self.assertEqual(event.sent, [])
        async for result in self.plugin.pixiv(event):
            event.sent.append(result)
        await self.run_auto("", event)
        self.assertEqual([m["type"] for m in self.media(event)], ["image"])
        self.assertEqual(self.requested_ids, ["123"])

    async def test_auto_duplicates_limits_and_message_cooldown(self):
        self.plugin.settings = replace(self.plugin.settings, auto_max_links=2, cooldown_seconds=60)
        event = await self.run_auto(
            "https://www.pixiv.net/artworks/123 https://pixiv.net/artworks/123 "
            "https://www.pixiv.net/artworks/124 https://www.pixiv.net/artworks/125"
        )
        self.assertEqual(self.requested_ids, ["123", "124"])
        self.assertEqual(len(self.media(event)), 2)
        self.assertTrue(any("只处理前 2 个" in m.get("text", "") for m in event.sent))
        second = await self.run_auto("https://www.pixiv.net/artworks/126")
        self.assertTrue(any("请求过于频繁" in m.get("text", "") for m in second.sent))
        # The same event cannot be downloaded twice, even with cooldown disabled.
        await self.run_auto("", event)
        self.assertEqual(self.requested_ids, ["123", "124"])

    async def test_auto_disabled_unrelated_and_self_messages_are_ignored(self):
        event = FakeEvent("https://www.pixiv.net/artworks/123")
        event.sender_id = event.get_self_id()
        await self.run_auto("", event)
        self.assertEqual(event.sent, [])
        for text in (
            "今天的消息",
            "https://www.pixiv.net/users/456",
            "https://pixiv.net.evil.com/artworks/123",
        ):
            self.assertEqual((await self.run_auto(text)).sent, [])
        self.plugin.settings = replace(self.plugin.settings, auto_parse_links=False)
        self.assertEqual((await self.run_auto("https://www.pixiv.net/artworks/123")).sent, [])
        self.assertEqual(self.requested_ids, [])

    async def test_auto_one_bad_link_does_not_skip_remaining_works(self):
        self.denied_ids.add("999")
        event = await self.run_auto("https://pixiv.net/artworks/999 https://pixiv.net/artworks/123")
        self.assertEqual(self.requested_ids, ["999", "123"])
        self.assertTrue(any("404" in m.get("text", "") for m in event.sent))
        self.assertEqual([m["name"] for m in self.media(event)], ["123_p0000.png"])
        self.assertEqual(self.plugin.running, 0)
        self.assertFalse(self.plugin.store.active)

    async def test_info_sends_metadata_and_only_first_picture(self):
        self.count = 20
        event = await self.run_command("/p 搜 123")
        media = self.media(event)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["type"], "image")
        self.assertTrue(any("PID：123" in m.get("text", "") for m in event.sent))
        self.assertEqual(self.plugin.running, 0)
        self.assertFalse(self.plugin.store.active)

    async def test_repeated_full_work_uses_metadata_original_and_zip_cache(self):
        self.count = 3
        first = await self.run_command("/p 123")
        second = await self.run_command("/p 123")
        self.assertEqual(self.requested_ids, ["123"])
        self.assertEqual(self.image_calls, 3)
        self.assertEqual(self.media(first)[0]["path"], self.media(second)[0]["path"])

    async def test_each_run_has_start_end_and_requests_have_correlated_task_ids(self):
        await self.run_command("/p 帮助")
        await self.run_command("/p 搜")
        await self.run_command("/p 123")
        self.denied_ids.add("999")
        await self.run_command("/p 999")
        self.plugin.running = self.plugin.settings.max_active_jobs
        await self.run_command("/p 124")
        self.plugin.running = 0
        await self.plugin.terminate()  # Flush the background logger before inspection.
        log_path = Path(self.tmp.name) / "plugin_data/astrbot_plugin_pixiv_search/logs/pixiv.jsonl"
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        starts = {r["run_id"] for r in records if r["event"] == "run_start"}
        ends = [r for r in records if r["event"] == "run_end"]
        self.assertEqual(len(starts), 5)
        self.assertEqual({r["run_id"] for r in ends}, starts)
        self.assertEqual(sorted(r["outcome"] for r in ends), ["busy", "error", "error", "ok", "ok"])
        for record in records:
            if record["event"] in ("request_start", "request_status", "zip_end", "send_end"):
                self.assertIn(record["run_id"], starts)
        self.assertTrue(any(r["event"] == "request_status" and r["status"] == 404 for r in records))

    async def test_n_and_n_plus_one_boundary(self):
        self.count = 2
        event = await self.run_command("/p 123")
        self.assertEqual([m["name"] for m in self.media(event)], ["123_p0000.png", "123_p0001.png"])
        self.count = 3
        event = await self.run_command("/p 124")
        media = self.media(event)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["name"], "pixiv_124.zip")
        with zipfile.ZipFile(media[0]["path"]) as z:
            self.assertEqual(len([n for n in z.namelist() if n.endswith(".png")]), 3)

    async def test_failed_page_sends_no_incomplete_archive(self):
        self.count = 3
        self.bad_page = True
        event = await self.run_command("/p 123")
        self.assertEqual(self.media(event), [])
        self.assertTrue(any("404" in m.get("text", "") for m in event.sent))
        self.assertFalse(self.plugin.store.active)
        self.assertEqual(self.plugin.running, 0)

    async def test_ugoira_contains_original_zip_and_frame_delays(self):
        self.ugoira = True
        event = await self.run_command("/p 123")
        with zipfile.ZipFile(self.media(event)[0]["path"]) as z:
            self.assertEqual(z.read("123_ugoira_frames.zip"), b"source frames")
            self.assertIn("ugoira.json", z.namelist())

    async def test_busy_and_help(self):
        self.plugin.running = self.plugin.settings.max_active_jobs
        event = await self.run_command("/p 123")
        self.assertTrue(any("任务已满" in m.get("text", "") for m in event.sent))
        event = await self.run_command("/p 帮助")
        self.assertTrue(any("Pixiv 搜索与解析" in m.get("text", "") for m in event.sent))
        self.plugin.running = 0


if __name__ == "__main__":
    unittest.main()
