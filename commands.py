"""Command parsing independent of AstrBot, including Pixiv links."""

import re
import shlex
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, urlsplit

from .settings import PixivError, integer

HELP = """Pixiv 搜索与解析
/p tag 初音ミク [--count 10-30] [--page 1]
/p 用户 画师UID [--count 10-30] [--page 1]
/p 搜 作品PID   详情 + 第一张图片
/p 作品PID      下载整个作品；按配置逐张发送或 ZIP
/p https://www.pixiv.net/artworks/作品PID
/p 帮助
搜索默认显示配置的数量；可用 --count 临时指定。
用户支持 UID 或 /users/ 链接。
启用自动解析时，直接发送 Pixiv 作品链接即可下载。"""

LINK_PATTERN = re.compile(
    r"(?<![\w./@-])(?:https?://|(?:www\.)?pixiv\.net/)"
    r"[^\s<>\"'()\[\]{},，。！？；、【】（）《》]+",
    re.IGNORECASE,
)


def artwork_links(message: str) -> list[str]:
    """Extract unique PIDs in message order; never follow arbitrary URL redirects."""
    result = []
    seen = set()
    for match in LINK_PATTERN.finditer(unescape(message)):
        url = match.group().rstrip(".;:!?…")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        try:
            pid = pixiv_id(url)
        except (PixivError, ValueError):
            continue
        if pid not in seen:
            seen.add(pid)
            result.append(pid)
    return result


@dataclass(frozen=True)
class Command:
    action: str
    query: str = ""
    count: int = 10
    page: int = 1


def pixiv_id(value: str, kind: str = "artworks") -> str:
    if re.fullmatch(r"[0-9]{1,16}", value) and int(value) > 0:
        return str(int(value))
    p = urlsplit(value)
    if p.scheme in ("http", "https") and p.hostname in ("www.pixiv.net", "pixiv.net"):
        match = re.fullmatch(rf"/(?:[a-z]{{2}}/)?{kind}/([0-9]{{1,16}})/?", p.path)
        if match:
            return pixiv_id(match.group(1), kind)
        if kind == "artworks" and p.path == "/member_illust.php":
            return pixiv_id(parse_qs(p.query).get("illust_id", [""])[0], kind)
    raise PixivError(
        "请输入有效的画师 UID / Pixiv 用户链接。"
        if kind == "users"
        else "请输入有效的作品 PID / Pixiv 作品链接。"
    )


def parse_command(message: str, default_count: int = 10) -> Command:
    # AstrBot normally removes the wake prefix before invoking command handlers.
    text = message.strip()
    text = re.sub(r"^/?p(?:\s+|$)", "", text, count=1, flags=re.I).strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        raise PixivError("引号未闭合，请检查命令。") from None
    if not tokens or tokens[0] in ("help", "帮助", "菜单"):
        return Command("help")
    action = tokens.pop(0)
    if action in ("搜", "搜索", "info", "id", "作品"):
        if len(tokens) != 1:
            raise PixivError("用法：/p 搜 作品PID")
        return Command("info", pixiv_id(tokens[0]))
    if action in ("tag", "标签", "用户", "user", "画师", "uid"):
        count, page = default_count, 1
        query = []
        seen = set()
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.startswith("--"):
                key, sep, val = token.partition("=")
                if key not in ("--count", "--page") or key in seen:
                    raise PixivError("选项仅支持 --count、--page，且不可重复。")
                seen.add(key)
                if not sep:
                    i += 1
                    if i >= len(tokens):
                        raise PixivError(f"{key} 缺少数值。")
                    val = tokens[i]
                if key == "--count":
                    count = integer(val, "作品数量", 10, 30)
                else:
                    page = integer(val, "页码", 1, 1000)
            else:
                query.append(token)
            i += 1
        if not query:
            raise PixivError("请输入 tag 或画师 UID。")
        if action in ("tag", "标签"):
            keyword = " ".join(query)
            if len(keyword) > 200:
                raise PixivError("Tag 关键词不能超过 200 个字符。")
            return Command("tag", keyword, count, page)
        if len(query) != 1:
            raise PixivError("用户搜索请输入一个 UID 或 Pixiv 用户链接。")
        return Command("user", pixiv_id(query[0], "users"), count, page)
    if tokens:
        raise PixivError("无法识别命令，请使用 /p 帮助。")
    return Command("download", pixiv_id(action))
