"""通过当前 uv Python 启动的独立下载进程。"""

import argparse
import sys

import psutil
import yt_dlp
import yt_dlp.downloader as downloader
from yt_dlp.utils import DownloadError

from memory_hls import MIB, MemoryHlsFD
from runtime_tools import ffmpeg_executable


def build_parser():
    parser = argparse.ArgumentParser(description="有界内存 HLS 下载")
    parser.add_argument("url")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--referer", default="")
    parser.add_argument("--cookies")
    parser.add_argument("--add-headers", action="append", default=[])
    parser.add_argument("--concurrent-fragments", type=int, default=10)
    parser.add_argument("--memory-mib", type=int, default=256, help="0 使用原生磁盘分片")
    parser.add_argument("--flush-mib", type=int, default=8)
    parser.add_argument("--flush-seconds", type=float, default=3)
    parser.add_argument("--fragment-retries", type=int, default=5)
    return parser


def run(args):
    if not 1 <= args.concurrent_fragments <= 32:
        raise ValueError("分片并发数必须为 1～32")
    if args.memory_mib < 0 or args.flush_mib <= 0 or args.flush_seconds <= 0 or args.fragment_retries < 0:
        raise ValueError("缓存、刷新和重试参数无效")
    headers = {"Referer": args.referer} if args.referer else {}
    for header in args.add_headers:
        key, value = header.split(":", 1)
        if key.lower() in {"user-agent", "origin"}:
            headers[key] = value
    options = dict(outtmpl=args.output, http_headers=headers, cookiefile=args.cookies,
                   concurrent_fragment_downloads=args.concurrent_fragments,
                   fragment_retries=args.fragment_retries, retries=5,
                   socket_timeout=15, skip_unavailable_fragments=False,
                   continuedl=True, noprogress=False, fixup="detect_or_warn",
                   ffmpeg_location=ffmpeg_executable())
    # 按启动时实际可用内存保守分配，文件大小的估算误差不会使缓存无界增长。
    budget = min(args.memory_mib * MIB, psutil.virtual_memory().available // 8)
    old_hls = downloader.HlsFD
    old_protocol = downloader.PROTOCOL_MAP["m3u8_native"]
    if args.memory_mib and budget >= 32 * MIB:
        batch = min(args.flush_mib * MIB, budget // 8)
        options["memory_hls"] = dict(cache_bytes=budget, batch_bytes=batch, interval=args.flush_seconds)
        downloader.HlsFD = MemoryHlsFD
        downloader.PROTOCOL_MAP["m3u8_native"] = MemoryHlsFD
    elif args.memory_mib:
        print("可用内存或配置预算不足 32 MiB，使用原生磁盘分片模式。", flush=True)
    try:
        # 嵌入 API 不读取用户全局 CLI 配置，避免 keep-fragments 等设置意外生效。
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.download([args.url])
    finally:
        downloader.HlsFD = old_hls
        downloader.PROTOCOL_MAP["m3u8_native"] = old_protocol


def main():
    try:
        return run(build_parser().parse_args())
    except KeyboardInterrupt:
        print("\n已中断下载。连续分片进度已保留，下次可继续。", file=sys.stderr)
        return 130
    except (DownloadError, ValueError, RuntimeError, OSError) as error:
        print(f"下载失败: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
