#!/usr/bin/env python3
"""
小鹅通视频批量下载工具
使用 Playwright 捕获 m3u8 地址，并通过 yt-dlp 下载视频
"""

import subprocess
import re
import sys
import json
import time
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime
from uuid import uuid4
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, Error as PlaywrightError
from capture import BrowserCapture, StopInput, media_key, video_id, save_records
from runtime_tools import ffmpeg_executable
from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.table import Table

console = Console()


class VideoDownloader:
    """视频下载器主类"""
    
    def __init__(self, user_data_dir: str = "./browser_session", output_dir: str = "./downloads",
                 fragment_concurrency: int = 10, memory_cache_mib: int = 256):
        self.user_data_dir = Path(user_data_dir)
        self.output_dir = Path(output_dir).expanduser()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.captured_urls: List[Dict[str, Any]] = []
        self.last_capture_file = None
        if not 1 <= fragment_concurrency <= 32 or memory_cache_mib < 0:
            raise ValueError("并发数必须为 1～32，缓存 MiB 必须大于等于 0")
        self.fragment_concurrency = fragment_concurrency
        self.memory_cache_mib = memory_cache_mib
        
    def check_dependencies(self) -> bool:
        """检查系统依赖是否安装"""
        console.print("\n[bold cyan]🔍 检查系统依赖...[/bold cyan]")
        
        try:
            from yt_dlp.version import __version__
            console.print(f"[green]✓[/green] uv 环境中的 yt-dlp: {__version__}")
            executable = ffmpeg_executable()
            result = subprocess.run([executable, "-version"], capture_output=True, text=True, check=True)
            console.print(f"[green]✓[/green] FFmpeg: {result.stdout.splitlines()[0]}")
        except (ImportError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
            console.print(f"依赖不可用: {error}", style="red", markup=False)
            console.print("[yellow]请先执行 uv sync，再用 uv run python main.py 启动。[/yellow]")
            return False
        return True
    
    def _launch_browser(self, playwright, headless=False):
        options = dict(user_data_dir=str(self.user_data_dir), headless=headless,
                       viewport={"width": 1280, "height": 720})
        try:
            return playwright.chromium.launch_persistent_context(channel="msedge", **options)
        except PlaywrightError:
            console.print("[yellow]无法启动 Edge，尝试 Chromium。[/yellow]")
            return playwright.chromium.launch_persistent_context(**options)

    def _start_capture(self, context):
        self.captured_urls.clear()
        session = Path("capture_sessions") / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8])
        self.last_capture_file = session.resolve() / "videos.json"
        capture = BrowserCapture(context, self.captured_urls, session,
                                 lambda message: console.print(message, style="green", markup=False))
        capture.persist()
        console.print(f"[dim]采集记录自动保存到: {self.last_capture_file}[/dim]")
        return capture

    def _finish_capture(self, context, capture):
        try:
            capture.drain()
            capture.persist()
        finally:
            capture.detach()
            try:
                context.close()
            except PlaywrightError:
                pass

    def _wait_for_capture(self, page, capture, seconds):
        deadline = time.monotonic() + seconds
        while not capture.closed and time.monotonic() < deadline:
            page.wait_for_timeout(min(200, max(0, (deadline - time.monotonic()) * 1000)))
            capture.drain()

    def capture_video_urls(self, urls: List[str], headless: bool = False, wait_time: int = 10):
        """按预先提供的页面列表采集，同样监听该会话内的新标签页。"""
        console.print(f"\n[bold cyan]🎬 开始捕获 {len(urls)} 个页面的视频...[/bold cyan]")
        with sync_playwright() as playwright:
            context = self._launch_browser(playwright, headless)
            capture = self._start_capture(context)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                for index, url in enumerate(urls, 1):
                    if capture.closed:
                        break
                    console.print(f"处理 [{index}/{len(urls)}]: {url}", markup=False)
                    try:
                        # 长连接和视频分片会使 networkidle 一直无法满足。
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        self._wait_for_capture(page, capture, max(0, wait_time))
                        capture.drain()
                        button = page.locator('button[class*="play"], div[class*="play"]').first
                        if button.is_visible():
                            button.click(timeout=2000)
                            self._wait_for_capture(page, capture, 5)
                        capture.drain()
                    except PlaywrightError as error:
                        console.print(f"页面处理未完成: {error}", style="yellow", markup=False)
            except KeyboardInterrupt:
                console.print("\n[yellow]已结束采集，保留已捕获的视频。[/yellow]")
            finally:
                self._finish_capture(context, capture)
        return self.captured_urls

    def capture_interactive(self, start_url: str = ""):
        """用户自由浏览；回车、Ctrl+C 或关闭全部浏览器窗口结束采集。"""
        if start_url and urlsplit(start_url).scheme not in {"http", "https"}:
            raise ValueError("起始地址必须是 http:// 或 https:// URL")
        console.print("\n[bold cyan]在浏览器中登录、浏览课程并点击播放，可打开多个标签页。[/bold cyan]")
        console.print("[cyan]终端按回车、Ctrl+C 或关闭全部浏览器窗口结束采集，然后选择是否下载。[/cyan]")
        stop_input = StopInput()
        with sync_playwright() as playwright:
            context = self._launch_browser(playwright, headless=False)
            capture = self._start_capture(context)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if start_url:
                    try:
                        page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
                    except PlaywrightError:
                        console.print("[yellow]起始页未加载完成，可在浏览器中继续手动访问。[/yellow]")
                while not capture.closed and context.pages:
                    capture.drain()
                    if stop_input.requested():
                        break
                    if capture.closed or not context.pages:
                        break
                    try:
                        # 必须让 Playwright 继续派发事件，不能用 input()/time.sleep() 阻塞。
                        context.pages[0].wait_for_timeout(200)
                    except PlaywrightError:
                        if capture.closed or not context.pages:
                            break
            except KeyboardInterrupt:
                console.print("\n[yellow]已结束采集，保留已捕获的视频。[/yellow]")
            finally:
                self._finish_capture(context, capture)
        return self.captured_urls

    def load_captured_info(self, filename: str):
        """复用自动快照或旧版导出的 JSON（签名链接仍需有效）。"""
        path = Path(filename)
        with path.open(encoding="utf-8") as stream:
            records = json.load(stream)
        if not isinstance(records, list):
            raise ValueError("采集记录必须是 JSON 数组")
        for record in records:
            if not isinstance(record, dict) or not all(isinstance(record.get(k), str)
                    for k in ("media_url", "title", "referer", "page_url")):
                raise ValueError("采集记录缺少 media_url/title/referer/page_url 字段")
            if urlsplit(record["media_url"]).scheme not in {"http", "https"}:
                raise ValueError("媒体地址必须是 HTTP(S) URL")
            if not isinstance(record.get("headers", {}), dict):
                raise ValueError("headers 必须是对象")
            record["video_id"] = video_id(media_key(record["media_url"])) if not re.fullmatch(
                r"[a-f0-9]{12}", str(record.get("video_id", ""))) else record["video_id"]
        self.captured_urls = records
        return records

    def download_video(self, video_info: Dict[str, Any], index: int) -> bool:
        """
        下载单个视频
        
        Args:
            video_info: 视频信息字典
            index: 视频序号
            
        Returns:
            下载是否成功
        """
        media_url = video_info['media_url']
        referer = video_info['referer']
        title = video_info['title']
        
        # 清理文件名，移除非法字符
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)
        safe_title = re.sub(r'[\x00-\x1f]', '_', safe_title).strip(" .")
        safe_title = safe_title.encode("utf-8")[:180].decode("utf-8", errors="ignore") or "未知标题"
        identity = video_info.get("video_id") or video_id(media_key(media_url))
        # 字面量 % 必须转义，避免标题被 yt-dlp 当作输出模板字段。
        basename = f"{safe_title}_{identity}"
        output_template = str(self.output_dir / basename).replace("%", "%%") + ".%(ext)s"
        
        console.print(f"\n[bold cyan]⬇️  下载视频:[/bold cyan] {title}")
        console.print(f"[dim]输出路径: {output_template}[/dim]")
        
        cmd = [
            sys.executable, str(Path(__file__).with_name("download_worker.py")),
            "--referer", referer,
            "--concurrent-fragments", str(self.fragment_concurrency),
            "--memory-mib", str(self.memory_cache_mib),
            "-o", output_template,
            media_url
        ]
        
        cookie_file = video_info.get("cookies_file")
        if cookie_file and Path(cookie_file).is_file():
            cmd[2:2] = ["--cookies", str(cookie_file)]
        for header, value in video_info.get("headers", {}).items():
            if header.lower() in {"user-agent", "origin"} and isinstance(value, str):
                cmd[2:2] = ["--add-headers", f"{header}:{value}"]
        try:
            subprocess.run(cmd, check=True)
            console.print(f"[green]✓[/green] 下载完成: {title}")
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            console.print(f"下载失败: {title}", style="red", markup=False)
            console.print("[yellow]采集记录和已提交的下载进度已保留。若签名过期，请重新采集来源页面。[/yellow]")
            return False
    
    def download_all(self) -> tuple[int, int]:
        """
        下载所有捕获的视频
        
        Returns:
            (成功数量, 失败数量)
        """
        if not self.captured_urls:
            console.print("[yellow]⚠[/yellow] 没有捕获到任何视频地址")
            return 0, 0
        
        console.print(f"\n[bold green]📥 开始批量下载 {len(self.captured_urls)} 个视频...[/bold green]")
        
        success_count = 0
        fail_count = 0
        
        for idx, video_info in enumerate(self.captured_urls, 1):
            if self.download_video(video_info, idx):
                success_count += 1
            else:
                fail_count += 1
        
        return success_count, fail_count
    
    def show_captured_videos(self):
        """显示捕获到的视频列表"""
        if not self.captured_urls:
            console.print("[yellow]⚠[/yellow] 没有捕获到任何视频")
            return
        
        table = Table(title="捕获到的视频列表", show_header=True, header_style="bold magenta")
        table.add_column("序号", style="dim", width=6)
        table.add_column("标题", style="cyan")
        table.add_column("页面URL", style="dim", overflow="fold")
        
        for idx, video in enumerate(self.captured_urls, 1):
            table.add_row(
                str(idx),
                video['title'],
                video['page_url']
            )
        
        console.print("\n")
        console.print(table)
    
    def save_captured_info(self, filename: str = "captured_videos.json"):
        """保存捕获的视频信息到JSON文件"""
        filepath = Path(filename)
        save_records(filepath, self.captured_urls)
        console.print(f"[green]✓[/green] 视频信息已保存到: {filepath.absolute()}")


def load_urls_from_file(filepath: str) -> List[str]:
    """从文件加载URL列表"""
    urls = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                urls.append(line)
    return urls


def main():
    """主函数"""
    console.print("[bold magenta]" + "=" * 60 + "[/bold magenta]")
    console.print("[bold magenta]  小鹅通视频批量下载工具 (XET Video Downloader)[/bold magenta]")
    console.print("[bold magenta]  支持自动捕获 m3u8 地址并批量下载[/bold magenta]")
    console.print("[bold magenta]" + "=" * 60 + "[/bold magenta]\n")
    
    while True:
        output_dir = Prompt.ask("视频输出目录", default="./downloads").strip() or "./downloads"
        try:
            downloader = VideoDownloader(output_dir=output_dir)
            break
        except (OSError, ValueError, RuntimeError) as error:
            console.print(f"无法创建输出目录，请重新输入: {error}", style="red", markup=False)
    console.print(f"视频将保存到: {downloader.output_dir.resolve()}", style="cyan", markup=False)
    
    # 检查依赖
    if not downloader.check_dependencies():
        console.print("\n[red]请先安装必要的依赖，然后重新运行程序[/red]")
        sys.exit(1)
    
    console.print("\n[bold cyan]📝 选择视频来源[/bold cyan]")
    console.print("  1. 手动输入 URL（多个用逗号分隔）")
    console.print("  2. 从文件读取 URL 列表")
    console.print("  3. 打开浏览器，自由浏览并收集视频")
    console.print("  4. 读取已保存的采集记录，继续下载")
    choice = Prompt.ask("请选择", choices=["1", "2", "3", "4"], default="1")

    if choice == "3":
        start_url = Prompt.ask("起始页面 URL（留空后在浏览器中手动访问）", default="").strip()
        captured = downloader.capture_interactive(start_url)
    elif choice == "4":
        filepath = Prompt.ask("采集 JSON 文件路径", default="captured_videos.json")
        try:
            captured = downloader.load_captured_info(filepath)
        except (OSError, ValueError) as error:
            console.print(f"无法读取采集记录: {error}", style="red", markup=False)
            return
        console.print("[dim]复用保存的地址；若签名已过期，需要重新浏览并采集。[/dim]")
    else:
        if choice == "1":
            url_input = Prompt.ask("请输入 URL")
            urls = [u.strip() for u in re.split(r'[,\n]+', url_input) if u.strip()]
        else:
            filepath = Prompt.ask("请输入文件路径", default="urls.txt")
            try:
                urls = load_urls_from_file(filepath)
            except OSError as error:
                console.print(f"无法读取 URL 文件: {error}", style="red", markup=False)
                return
        if not urls:
            console.print("[red]没有输入任何 URL[/red]")
            return
        headless = not Confirm.ask("是否显示浏览器窗口？（首次运行建议显示以完成登录）", default=True)
        default_wait = 10 if headless else 30
        wait_time_str = Prompt.ask("每个页面等待时间（秒）", default=str(default_wait))
        try:
            wait_time = int(wait_time_str)
            if wait_time < 0:
                raise ValueError
        except ValueError:
            wait_time = default_wait
            console.print(f"[yellow]输入无效，使用默认值: {wait_time} 秒[/yellow]")
        captured = downloader.capture_video_urls(urls, headless=headless, wait_time=wait_time)

    if not captured:
        console.print("\n[yellow]⚠[/yellow] 未能捕获到任何视频地址")
        console.print("可能的原因:")
        console.print("  - 需要登录（请使用显示浏览器模式并手动登录）")
        console.print("  - 页面加载时间不足")
        console.print("  - URL格式不正确")
        sys.exit(1)
    
    # 显示捕获结果
    downloader.show_captured_videos()
    
    # 保存捕获信息
    if Confirm.ask("\n是否保存捕获的视频信息到JSON文件？", default=True):
        downloader.save_captured_info()
    
    # 下载设置同时适用于自由嗅探和历史记录复用。
    def ask_integer(label, default, minimum, maximum):
        while True:
            try:
                value = int(Prompt.ask(label, default=str(default)))
                if minimum <= value <= maximum:
                    return value
            except ValueError:
                pass
            console.print(f"[yellow]请输入 {minimum}～{maximum} 之间的整数。[/yellow]")

    # 询问是否开始下载
    if Confirm.ask("\n是否开始下载视频？", default=True):
        downloader.fragment_concurrency = ask_integer("分片并发数", 10, 1, 32)
        downloader.memory_cache_mib = ask_integer("分片缓存上限（MiB，0 使用磁盘分片）", 256, 0, 4096)
        success, fail = downloader.download_all()
        
        # 显示下载结果
        console.print("\n" + "=" * 60)
        console.print(f"[bold green]下载完成！[/bold green]")
        console.print(f"  成功: [green]{success}[/green] 个")
        console.print(f"  失败: [red]{fail}[/red] 个")
        console.print(f"  输出目录: [cyan]{downloader.output_dir.absolute()}[/cyan]")
        console.print("=" * 60)
    else:
        console.print("\n[yellow]已取消下载操作[/yellow]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n\n[yellow]用户中断操作[/yellow]")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[red]发生错误:[/red] {str(e)}")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
        sys.exit(1)

