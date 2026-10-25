"""有界内存 HLS 下载：有限领先窗口、连续写入、持久化提交点。

仅在 download_worker 子进程中接入固定版本的 yt-dlp HlsFD。
播放列表、字节范围和 AES-128 元数据由 yt-dlp 解析；分片不创建临时文件。
"""

import hashlib
import errno
import json
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from yt_dlp.downloader.hls import HlsFD
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError, RequestError
from yt_dlp.utils import DownloadError


MIB = 1024 * 1024
SIGNATURE_KEYS = {"sign", "signature", "expires", "txsecret", "txtime", "auth_key"}


class FragmentTooLarge(Exception):
    pass


class ResumeError(Exception):
    pass


def stable_url(url):
    parts = urlsplit(url)
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if k.lower() not in SIGNATURE_KEYS)
    # 与采集去重不同，恢复时必须保留清晰度路径，防止混入另一种转码。
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def manifest_fingerprint(fragments):
    entries = []
    for fragment in fragments:
        decrypt = fragment.get("decrypt_info", {})
        entries.append({
            "url": stable_url(fragment["url"]),
            "range": fragment.get("byte_range"),
            "sequence": fragment.get("media_sequence"),
            "method": decrypt.get("METHOD"),
            "key_url": stable_url(decrypt.get("URI") or ""),
            "iv": (decrypt.get("IV") or b"").hex(),
            "key_hash": hashlib.sha256(decrypt["KEY"]).hexdigest() if decrypt.get("KEY") else None,
        })
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def sync_directory(directory):
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            try:
                os.fsync(descriptor)
            except OSError as error:
                # 部分网络文件系统不支持目录 fsync；数据文件和记录仍先后同步。
                if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
                    raise
        finally:
            os.close(descriptor)


class CheckpointWriter:
    """先同步数据，再原子保存索引和偏移；恢复时截去未提交尾部。"""

    def __init__(self, filename, fingerprint, total, batch_bytes=8 * MIB, interval=3):
        self.filename = Path(filename)
        self.part = Path(str(filename) + ".mem.part")
        self.state = Path(str(filename) + ".mem.json")
        self.fingerprint = fingerprint
        self.total = total
        self.batch_bytes = batch_bytes
        self.interval = interval
        self.index = self.offset = self.committed_offset = 0
        self.last_commit = time.monotonic()
        self.stream = None
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        if self.state.exists():
            try:
                state = json.loads(self.state.read_text(encoding="utf-8"))
                if state["version"] != 1 or state["manifest"] != fingerprint or state["total"] != total:
                    raise ResumeError("播放列表已变化，不能将新内容追加到旧进度；请更换输出目录重新下载")
                self.index, self.offset = state["index"], state["offset"]
                if (type(self.index) is not int or type(self.offset) is not int
                        or not 0 <= self.index <= total or self.offset < self.index
                        or (self.index == 0 and self.offset != 0)):
                    raise ValueError("invalid checkpoint")
                if not self.part.is_file() or self.part.stat().st_size < self.offset:
                    raise ResumeError("缓存数据短于提交记录，已保留文件；请更换输出目录重新下载")
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ResumeError("续传记录无法读取，已保留文件；请更换输出目录重新下载") from error
            self.stream = self.part.open("r+b", buffering=batch_bytes)
            self.stream.truncate(self.offset)
            self.stream.seek(self.offset)
            self.committed_offset = self.offset
        else:
            if self.part.exists():
                raise ResumeError("发现无提交记录的内存模式临时文件，已保留；请更换输出目录重新下载")
            self.stream = self.part.open("x+b", buffering=batch_bytes)
            try:
                self.commit(force=True)
            except BaseException:
                self.close()
                raise

    def append(self, index, data):
        if index != self.index + 1:
            raise ValueError("不能越过缺片写入")
        self.stream.write(data)
        self.index = index
        self.offset += len(data)
        self.commit_if_due()

    def commit_if_due(self):
        if (self.offset - self.committed_offset >= self.batch_bytes
                or time.monotonic() - self.last_commit >= self.interval):
            self.commit()

    def commit(self, force=False):
        if not force and self.offset == self.committed_offset:
            return
        self.stream.flush()
        os.fsync(self.stream.fileno())
        state = dict(version=1, manifest=self.fingerprint, total=self.total,
                     index=self.index, offset=self.offset)
        temporary = self.state.with_name(self.state.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.state)
        sync_directory(self.state.parent)
        self.committed_offset = self.offset
        self.last_commit = time.monotonic()

    def finish(self):
        if self.index != self.total or self.offset == 0:
            raise ValueError("分片不完整，不能生成完成文件")
        self.commit()
        self.close()
        self.part.replace(self.filename)
        sync_directory(self.filename.parent)
        self.state.unlink()

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


class OrderedMemoryPipeline:
    """限制未写入任务数，同时限制每片字节数，缺片只占自己的预留槽位。"""

    def __init__(self, workers=10, cache_bytes=256 * MIB, batch_bytes=8 * MIB):
        self.workers = workers
        self.window = 2 * workers
        self.cache_bytes = cache_bytes
        # 预留文件写缓冲、读取增长空间、解密副本；不将进程总 RSS 称为缓存大小。
        self.fragment_limit = (cache_bytes - 2 * batch_bytes) // (2 * self.window + 6)
        if self.fragment_limit < 64 * 1024:
            raise ValueError("缓存过小，请增加缓存或降低并发数")
        self.cancel = threading.Event()
        self.max_pending = 0

    def run(self, fragments, writer, fetch, transform=lambda fragment, data: data, progress=lambda: None):
        pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="hls-memory")
        pending = deque()
        next_submit = writer.index
        try:
            while writer.index < len(fragments):
                while next_submit < len(fragments) and len(pending) < self.window:
                    fragment = fragments[next_submit]
                    pending.append((next_submit + 1, fragment,
                                    pool.submit(fetch, fragment, self.fragment_limit, self.cancel)))
                    next_submit += 1
                self.max_pending = max(self.max_pending, len(pending))
                index, fragment, future = pending[0]
                try:
                    data = future.result(timeout=0.2)
                except TimeoutError:
                    writer.commit_if_due()
                    continue
                payload = transform(fragment, data)
                if not payload:
                    raise ValueError("解密后的分片为空")
                if len(payload) > self.fragment_limit:
                    raise FragmentTooLarge("解密后分片超过缓存槽位")
                writer.append(index, payload)
                pending.popleft()
                # Future 本身持有结果，释放引用后才能调度下一片。
                del future, data, payload
                progress()
            writer.commit()
        except BaseException:
            # 正常中断也只提交完整连续前缀。突然退出时依靠上次提交点恢复。
            writer.commit()
            raise
        finally:
            self.cancel.set()
            for _, _, future in pending:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
            pending.clear()


class MemoryHlsFD(HlsFD):
    # yt-dlp 根据这个名称启用 MPEG-TS → MP4 容器修复，不能改成自定义名称。
    FD_NAME = "hlsnative"

    def real_download(self, filename, info_dict):
        # 尊重已有原生下载的续传状态；不修改旧任务的 .part/.ytdl。
        if (info_dict.get("ext") == "vtt" or filename == "-"
                or Path(str(filename) + ".part").exists()):
            self.to_screen("[hls-memory] 使用原生下载器兼容现有进度/输出格式")
            return HlsFD(self.ydl, self.params).real_download(filename, info_dict)
        try:
            return super().real_download(filename, info_dict)
        except FragmentTooLarge:
            self.report_warning("单片超过内存槽位，改用原生磁盘分片模式；不扩大内存占用")
            result = HlsFD(self.ydl, self.params).real_download(filename, info_dict)
            if result:
                for suffix in (".mem.part", ".mem.json"):
                    Path(str(filename) + suffix).unlink(missing_ok=True)
            return result

    def _prepare_and_start_frag_download(self, ctx, info_dict):
        # 让 yt-dlp 完整解析列表，再按 checkpoint 跳过已提交的片段。
        # 这也避免恢复时跳过解析导致隐式 AES IV/字节范围偏移错误。
        ctx.update(fragment_index=0, extra_state={})
        self.report_destination(ctx["filename"])

    def fetch_fragment(self, fragment, limit, cancel, info_dict):
        attempts = int(self.params.get("fragment_retries", 5)) + 1
        headers = dict(info_dict.get("http_headers") or {})
        headers["Accept-Encoding"] = "identity"
        byte_range = fragment.get("byte_range")
        expected_range = None
        if byte_range:
            expected_range = byte_range["end"] - byte_range["start"]
            if expected_range > limit:
                raise FragmentTooLarge()
            headers["Range"] = f"bytes={byte_range['start']}-{byte_range['end'] - 1}"
        for attempt in range(attempts):
            if cancel.is_set():
                raise DownloadError("下载已停止")
            try:
                with self.ydl.urlopen(Request(fragment["url"], headers=headers)) as response:
                    if byte_range:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(?:\d+|\*)", response.headers.get("Content-Range", ""))
                        if (response.status != 206 or not match
                                or int(match[1]) != byte_range["start"] or int(match[2]) + 1 != byte_range["end"]):
                            raise ValueError("服务器未返回请求的字节范围")
                    length = response.headers.get("Content-Length", "")
                    expected = int(length) if length.isdigit() else None
                    if expected is not None and expected > limit:
                        raise FragmentTooLarge()
                    data = bytearray()
                    while not cancel.is_set():
                        chunk = response.read(min(64 * 1024, limit - len(data) + 1))
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > limit:
                            raise FragmentTooLarge()
                    if cancel.is_set():
                        raise DownloadError("下载已停止")
                    if not data or (expected_range is not None and len(data) != expected_range):
                        raise ValueError("空分片或字节范围不完整")
                    if response.headers.get("Content-Encoding", "identity") == "identity" and expected is not None and len(data) != expected:
                        raise ValueError("分片长度不完整")
                    return data
            except (RequestError, OSError, ValueError) as error:
                if isinstance(error, HTTPError):
                    error.close()
                if attempt + 1 == attempts:
                    raise DownloadError(f"分片 {fragment['frag_index']} 重试失败，已保留连续进度，未跳过缺片") from error
                self.to_screen(f"[hls-memory] 分片 {fragment['frag_index']} 重试 {attempt + 1}/{attempts - 1}")
                if cancel.wait(min(2 ** attempt, 8)):
                    raise DownloadError("下载已停止")

    def download_and_append_fragments(self, ctx, fragments, info_dict, **kwargs):
        fragments = list(fragments)
        settings = self.params["memory_hls"]
        writer = None
        try:
            writer = CheckpointWriter(ctx["filename"], manifest_fingerprint(fragments), len(fragments),
                                      settings["batch_bytes"], settings["interval"])
            pipeline = OrderedMemoryPipeline(self.params.get("concurrent_fragment_downloads", 10),
                                             settings["cache_bytes"], settings["batch_bytes"])
            self.to_screen(f"[hls-memory] 缓存预算 {settings['cache_bytes'] // MIB} MiB，"
                           f"领先窗口 {pipeline.window} 片，已提交 {writer.index}/{len(fragments)} 片")
            started = time.monotonic()
            initial_offset = writer.offset

            def progress():
                elapsed = max(time.monotonic() - started, 0.001)
                speed = (writer.offset - initial_offset) / elapsed
                estimate = writer.offset * len(fragments) / max(writer.index, 1)
                self._hook_progress(dict(status="downloading", filename=str(writer.filename),
                    tmpfilename=str(writer.part), downloaded_bytes=writer.offset,
                    total_bytes_estimate=estimate, elapsed=elapsed, speed=speed,
                    eta=(estimate - writer.offset) / speed if speed else None,
                    fragment_index=writer.index, fragment_count=len(fragments)), info_dict)

            decrypt = self.decrypter(info_dict)
            pipeline.run(fragments, writer,
                         lambda fragment, limit, cancel: self.fetch_fragment(fragment, limit, cancel, info_dict),
                         decrypt, progress)
            writer.finish()
            self._hook_progress(dict(status="finished", filename=str(writer.filename),
                downloaded_bytes=writer.offset, total_bytes=writer.offset,
                elapsed=time.monotonic() - started), info_dict)
            return True
        except (ResumeError, OSError, ValueError) as error:
            raise DownloadError(str(error)) from error
        finally:
            if writer is not None:
                writer.close()
