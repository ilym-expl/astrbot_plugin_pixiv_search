"""Validated configuration; never include credentials in error messages."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class PixivError(Exception):
    """A message which is safe to show to the chat user."""


def integer(value, name: str, low: int, high: int) -> int:
    try:
        result = int(str(value))
    except (ValueError, TypeError):
        raise PixivError(f"{name} 必须是 {low}–{high} 之间的整数。") from None
    if not low <= result <= high:
        raise PixivError(f"{name} 必须在 {low}–{high} 之间。")
    return result


def base_url(value: str, name: str) -> str:
    value = value.strip().rstrip("/")
    p = urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname or p.query or p.fragment or p.username:
        raise PixivError(f"{name} 必须为无账号、查询参数及片段的 HTTP(S) 根地址。")
    return value


@dataclass(frozen=True, repr=False)
class Settings:
    cookie: str = ""
    proxy: str = ""
    api_base_url: str = "https://www.pixiv.net"
    image_base_url: str = ""
    auto_parse_links: bool = True
    auto_max_links: int = 3
    search_count: int = 10
    direct_send_limit: int = 5
    direct_send_mode: str = "file"
    download_concurrency: int = 2
    api_interval_ms: int = 1500
    image_interval_ms: int = 500
    rate_limit_cooldown_seconds: int = 180
    retry_backoff_seconds: int = 5
    metadata_cache_minutes: int = 10
    cache_max_mb: int = 2048
    log_max_mb: int = 10
    log_backups: int = 5
    max_active_jobs: int = 2
    timeout_seconds: int = 60
    retries: int = 2
    max_pages: int = 300
    max_file_mb: int = 50
    max_total_mb: int = 500
    cache_ttl_minutes: int = 60
    cooldown_seconds: int = 5
    tag_match: str = "s_tag_full"
    search_mode: str = "all"
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"

    @classmethod
    def from_mapping(cls, data):
        defaults = cls()
        values = {}
        bounds = {
            "auto_max_links": (1, 10),
            "search_count": (10, 30),
            "direct_send_limit": (0, 30),
            "download_concurrency": (1, 16),
            "api_interval_ms": (0, 60000),
            "image_interval_ms": (0, 60000),
            "rate_limit_cooldown_seconds": (1, 86400),
            "retry_backoff_seconds": (1, 60),
            "metadata_cache_minutes": (1, 1440),
            "cache_max_mb": (10, 65536),
            "log_max_mb": (1, 100),
            "log_backups": (1, 30),
            "max_active_jobs": (1, 8),
            "timeout_seconds": (5, 300),
            "retries": (0, 5),
            "max_pages": (1, 2000),
            "max_file_mb": (1, 1024),
            "max_total_mb": (1, 4096),
            "cache_ttl_minutes": (5, 1440),
            "cooldown_seconds": (0, 3600),
        }
        for key, (low, high) in bounds.items():
            values[key] = integer(data.get(key, getattr(defaults, key)), key, low, high)
        values["auto_parse_links"] = data.get("auto_parse_links", defaults.auto_parse_links)
        if not isinstance(values["auto_parse_links"], bool):
            raise PixivError("auto_parse_links 必须为布尔开关。")
        for key, allowed in {
            "direct_send_mode": ("file", "image"),
            "tag_match": ("s_tag_full", "s_tag"),
            "search_mode": ("all", "safe", "r18"),
        }.items():
            values[key] = str(data.get(key, getattr(defaults, key)))
            if values[key] not in allowed:
                raise PixivError(f"{key} 配置值不合法。")
        values["api_base_url"] = base_url(
            str(data.get("api_base_url", defaults.api_base_url)), "api_base_url"
        )
        image_base = str(data.get("image_base_url", "")).strip()
        values["image_base_url"] = base_url(image_base, "image_base_url") if image_base else ""
        proxy = str(data.get("proxy", "")).strip()
        if proxy:
            p = urlsplit(proxy)
            if p.scheme not in ("http", "https", "socks5", "socks5h") or not p.hostname:
                raise PixivError("proxy 必须是 HTTP(S) 或 SOCKS5 代理地址。")
        values["proxy"] = proxy
        cookie = os.environ.get("PIXIV_COOKIE", "").strip()
        cookie_file = str(data.get("cookie_file", "")).strip()
        if not cookie and cookie_file:
            try:
                cookie = Path(cookie_file).expanduser().read_text(encoding="utf-8-sig").strip()
            except OSError:
                raise PixivError("无法读取 cookie_file，请检查文件路径与读取权限。") from None
        if not cookie:
            cookie = str(data.get("cookie", "")).strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()
        if cookie and "=" not in cookie:
            cookie = "PHPSESSID=" + cookie
        if "\r" in cookie or "\n" in cookie:
            raise PixivError("Cookie 必须是一行文本。")
        values["cookie"] = cookie
        agent = str(data.get("user_agent", defaults.user_agent)).strip()
        if not agent or re.search(r"[\r\n]", agent):
            raise PixivError("user_agent 必须是一行非空文本。")
        values["user_agent"] = agent
        return cls(**values)
