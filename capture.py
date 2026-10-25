"""媒体记录、HLS 主/子列表归并以及本地采集快照。"""

import hashlib
import json
import os
import re
import select
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError


# 仅去掉已知的临时签名参数；保留 id、quality 等可能决定资源身份的参数。
SIGNATURE_KEYS = {"sign", "signature", "expires", "txsecret", "txtime", "auth_key"}
HLS_TYPES = {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl"}


def media_key(url):
    parts = urlsplit(url)
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if k.lower() not in SIGNATURE_KEYS)
    # 小鹅通已知转码命名：同一目录下 v.fXXXXXX 是同一个视频的清晰度。
    path = re.sub(r"/v\.f\d+(?:\.mp4)?\.m3u8$", "/v.variant.m3u8", parts.path)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def video_id(key):
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def media_kind(url, content_type=""):
    content_type = content_type.split(";", 1)[0].strip().lower()
    path = urlsplit(url).path.lower()
    if path.endswith((".ts", ".m4s", ".aac", ".m4a", ".vtt", ".key")):
        return None
    if content_type in {"text/html", "application/json"}:
        return None
    if content_type in HLS_TYPES or path.endswith(".m3u8"):
        return "hls"
    if content_type == "video/mp4" or (path.endswith(".mp4") and not content_type.startswith("audio/")):
        return "mp4"
    return None


def playlist_children(url, body):
    """只解析主列表引用的播放列表，不把 ts/m4s 分片当作视频。"""
    if not body.lstrip("\ufeff\r\n ").startswith("#EXTM3U"):
        return []
    children = []
    variant_next = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            variant_next = True
        elif line.startswith(("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:")):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                children.append(urljoin(url, match.group(1)))
        elif line and not line.startswith("#"):
            if variant_next:
                children.append(urljoin(url, line))
            variant_next = False
    return children


def playlist_fragments(url, body):
    """媒体列表中的分片和初始化文件，即便后缀是 mp4 也不是独立视频。"""
    if not body.lstrip("\ufeff\r\n ").startswith("#EXTM3U") or playlist_children(url, body):
        return []
    fragments = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                fragments.append(urljoin(url, match.group(1)))
        elif line and not line.startswith("#"):
            fragments.append(urljoin(url, line))
    return fragments


def atomic_write(path, text):
    """快照包含签名地址；使用私有权限并原子替换，防止中断写坏记录。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    temporary.replace(path)


def save_records(path, records):
    atomic_write(path, json.dumps(records, ensure_ascii=False, indent=2))


def save_cookies(path, cookies):
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        # Netscape 格式无法表达分区 cookie，不能将其误导出为全局 cookie。
        if cookie.get("partitionKey"):
            continue
        domain = cookie["domain"]
        fields = [domain, "TRUE" if domain.startswith(".") else "FALSE",
                  cookie.get("path", "/"), "TRUE" if cookie.get("secure") else "FALSE",
                  str(max(0, int(cookie.get("expires", 0)))), cookie["name"], cookie["value"]]
        if not any("\n" in value or "\r" in value or "\t" in value for value in fields):
            lines.append("\t".join(fields))
    atomic_write(path, "\n".join(lines) + "\n")


class MediaRegistry:
    """通过资源身份和主列表关系归并，绝不按页面 URL 或标题合并视频。"""

    def __init__(self, records):
        self.records = records
        self.aliases = {}
        self.entries = {}
        self.priorities = {}
        self.masters = set()
        self.fragments = set()

    def root(self, key):
        while key in self.aliases:
            key = self.aliases[key]
        return key

    def observe(self, info, body=""):
        key = media_key(info["media_url"])
        if key in self.fragments:
            return False
        root = self.root(key)
        children = playlist_children(info["media_url"], body)
        structural_change = False
        for fragment in playlist_fragments(info["media_url"], body):
            fragment_key = media_key(fragment)
            if fragment_key == key:
                continue
            self.fragments.add(fragment_key)
            old = self.entries.pop(fragment_key, None)
            self.priorities.pop(fragment_key, None)
            if old is not None:
                self.records.remove(old)
                structural_change = True
        if children:
            self.masters.add(key)
        # 主列表优先，让 yt-dlp 根据实际播放列表选择最高可用清晰度。
        priority = 3 if key in self.masters else (2 if "v.f421220" in urlsplit(info["media_url"]).path else 1)
        if children:
            for child in children:
                child_key = media_key(child)
                child_root = self.root(child_key)
                if child_root == root:
                    continue
                if child_root != child_key:
                    # 两个主列表可能共用音轨，不能因此将两个视频合并。
                    continue
                old = self.entries.pop(child_root, None)
                self.priorities.pop(child_root, None)
                if old is not None:
                    self.records.remove(old)
                    structural_change = True
                self.aliases[child_root] = root

        existing = self.entries.get(root)
        if existing is not None and key != root:
            # 已归属于上层主列表的嵌套主列表，不能覆盖上层地址。
            return bool(children) or structural_change
        if existing is not None and priority < self.priorities[root]:
            return structural_change
        record = dict(info, video_id=video_id(root))
        record["captured_at"] = datetime.now(timezone.utc).isoformat()
        if existing is None:
            self.records.append(record)
            self.entries[root] = record
        else:
            # 同一地址重复请求无须反复落盘；签名变化时更新完整下载 URL。
            if all(existing.get(k) == v for k, v in info.items()) and priority == self.priorities[root]:
                return bool(children) or structural_change
            existing.update(record)
        self.priorities[root] = priority
        return True


class StopInput:
    """非阻塞检测回车，不留下后台 input 线程吞掉后续下载确认。"""

    def __init__(self):
        self.enabled = True

    def requested(self):
        if not self.enabled:
            return False
        try:
            if os.name == "nt":
                if not sys.stdin.isatty():
                    self.enabled = False
                    return False
                import msvcrt
                while msvcrt.kbhit():
                    char = msvcrt.getwch()
                    if char == "\x03":
                        raise KeyboardInterrupt
                    if char in {"\r", "\n"}:
                        return True
                return False
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if ready:
                sys.stdin.readline()
                return True
        except (OSError, ValueError, AttributeError):
            # IDE 无真实 stdin 时仍可通过关闭浏览器/Ctrl+C 结束。
            self.enabled = False
        return False


class BrowserCapture:
    """同一 BrowserContext 内所有标签页、弹窗和 iframe 的媒体采集器。"""

    def __init__(self, context, records, session_dir, report):
        self.context = context
        self.registry = MediaRegistry(records)
        self.session_dir = Path(session_dir).resolve()
        self.report = report
        self.sources = {}
        self.pending = {}
        self.parsed = set()
        self.events = deque()
        self.closed = False
        context.on("request", self.on_request)
        context.on("response", self.on_response)
        context.on("requestfinished", self.on_finished)
        context.on("requestfailed", self.on_failed)
        context.on("close", self.on_close)

    def on_close(self, *_):
        self.closed = True

    def on_request(self, request):
        if not media_kind(request.url) and request.resource_type not in {"fetch", "xhr", "media"}:
            return
        try:
            page = request.frame.page
            source = {"page": page, "page_url": page.url, "title": ""}
            self.sources[request] = source
        except PlaywrightError:
            # Service Worker 请求可能没有 frame，不能让一次失败中止监听。
            pass

    def describe(self, response, kind):
        request = response.request
        source = self.sources.get(request, {})
        page = source.get("page")
        headers = request.headers
        page_url = source.get("page_url") or headers.get("referer", "")
        title = source.get("title", "")
        if page is not None:
            try:
                if not page.is_closed() and page.url == page_url:
                    # 优先使用页面中的章节标题；不等待不存在的元素。
                    title = page.evaluate("""() => {
                        const heading = document.querySelector('h1');
                        return (heading && heading.innerText.trim()) || document.title;
                    }""") or title
            except PlaywrightError:
                pass
        return {
            "media_url": response.url,
            "referer": headers.get("referer", ""),
            "page_url": page_url,
            "title": title or "未知标题",
            "kind": kind,
            "headers": {k: headers[k] for k in ("user-agent", "origin") if k in headers},
            "cookies_file": str(self.session_dir / "cookies.txt"),
        }

    def persist(self):
        save_records(self.session_dir / "videos.json", self.registry.records)
        if not self.closed:
            try:
                save_cookies(self.session_dir / "cookies.txt", self.context.cookies())
            except PlaywrightError:
                # 用户关闭浏览器时保留上一次成功导出的 cookie。
                pass

    def observe(self, info, body=""):
        count = len(self.registry.records)
        if self.registry.observe(info, body):
            self.persist()
            action = "新增" if len(self.registry.records) > count else "更新/归并"
            self.report(f"{action}视频（共 {len(self.registry.records)} 个）: {info['title']}")

    def on_response(self, response):
        self.events.append((self.process_response, response))

    def on_finished(self, request):
        self.events.append((self.process_finished, request))

    def on_failed(self, request):
        self.events.append((self.process_failed, request))

    def drain(self):
        # 回调只入队。在主循环读标题/响应体，避免同步 API 嵌套派发导致完成事件丢失。
        while self.events:
            handler, value = self.events.popleft()
            handler(value)

    def process_response(self, response):
        if not 200 <= response.status < 300:
            return
        kind = media_kind(response.url, response.headers.get("content-type", ""))
        if not kind:
            return
        try:
            info = self.describe(response, kind)
            # MP4 可使用范围请求，不等待整个响应体下载完才记入列表。
            self.observe(info)
            if kind == "hls" and response.url not in self.parsed:
                self.pending[response.request] = (response, info)
        except PlaywrightError:
            pass

    def process_finished(self, request):
        self.sources.pop(request, None)
        pending = self.pending.pop(request, None)
        if pending is None:
            return
        response, info = pending
        try:
            length = response.headers.get("content-length", "0")
            if length.isdigit() and int(length) > 1024 * 1024:
                return
            body = response.text()
            if len(body) <= 1024 * 1024:
                self.observe(info, body)
                self.parsed.add(response.url)
        except PlaywrightError:
            pass

    def process_failed(self, request):
        self.sources.pop(request, None)
        self.pending.pop(request, None)

    def detach(self):
        for event, handler in (("request", self.on_request), ("response", self.on_response),
                               ("requestfinished", self.on_finished), ("requestfailed", self.on_failed),
                               ("close", self.on_close)):
            self.context.remove_listener(event, handler)
