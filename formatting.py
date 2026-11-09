"""Plain-text formatting and safe HTML caption conversion."""

import re
from html.parser import HTMLParser


class CaptionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "li") and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("p", "div", "li") and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def caption(value: str, limit: int = 1800) -> str:
    parser = CaptionParser()
    parser.feed(str(value))
    text = re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def tags_of(work: dict) -> list[str]:
    tags = work.get("tags") or []
    if isinstance(tags, dict):
        tags = tags.get("tags", [])
    return [str(t.get("tag", "")) if isinstance(t, dict) else str(t) for t in tags]


def detail(work: dict) -> str:
    pid = work.get("id", "?")
    return (
        f"{work.get('title', '无标题')}\n"
        f"PID：{pid}\n作者：{work.get('userName', '未知')}（UID {work.get('userId', '?')}）\n"
        f"页数：{work.get('pageCount', 1)} | 尺寸：{work.get('width', '?')}×{work.get('height', '?')}\n"
        f"收藏：{work.get('bookmarkCount', '?')} | 浏览：{work.get('viewCount', '?')}\n"
        f"发布时间：{work.get('createDate', '未知')}\n"
        f"Tag：{' / '.join(tags_of(work)) or '无'}\n"
        f"简介：{caption(work.get('description', '')) or '无'}\n"
        f"https://www.pixiv.net/artworks/{pid}"
    )


def search_chunks(works: list[dict], title: str, total: int, page: int) -> list[str]:
    chunks = []
    for offset in range(0, len(works), 10):
        lines = [f"{title} | 第 {page} 页 | 返回 {len(works)} 项 / 上游共 {total} 项"]
        for index, work in enumerate(works[offset : offset + 10], offset + 1):
            pid = work.get("id", "?")
            lines.append(
                f"\n{index}. {str(work.get('title', '无标题'))[:120]}\n"
                f"作者：{str(work.get('userName', '未知'))[:80]} | UID {work.get('userId', '?')}\n"
                f"PID {pid} | {work.get('pageCount', 1)} 张\n"
                f"Tag：{' / '.join(tags_of(work))[:200] or '未提供'}\n"
                f"https://www.pixiv.net/artworks/{pid}"
            )
        lines.append("\n/p 搜 PID 查看详情；/p PID 下载整个作品。")
        chunks.append("\n".join(lines))
    return chunks
