import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from capture import BrowserCapture, MediaRegistry, StopInput, media_key, media_kind, playlist_children
from main import VideoDownloader
from runtime_tools import ffmpeg_executable


ARTIFACTS = Path(__file__).resolve().parents[1] / ".test-artifacts"
ARTIFACTS.mkdir(exist_ok=True)


def info(url, title="同名课程"):
    return dict(media_url=url, title=title, referer="https://course.test/", page_url="https://course.test/lesson")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.registry = MediaRegistry(self.records)

    def test_signature_refresh_preserves_resource_parameters(self):
        self.registry.observe(info("https://cdn.test/play.m3u8?id=1&sign=old"))
        identity = self.records[0]["video_id"]
        self.registry.observe(info("https://cdn.test/play.m3u8?sign=new&id=1"))
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records[0]["video_id"], identity)
        self.assertIn("sign=new", self.records[0]["media_url"])
        self.registry.observe(info("https://cdn.test/play.m3u8?id=2&sign=new"))
        self.assertEqual(len(self.records), 2)

    def test_xet_quality_upgrade_and_no_downgrade(self):
        for variant in ("f100", "f421220", "f100"):
            self.registry.observe(info(f"https://cdn.test/video1/v.{variant}.m3u8?sign=1"))
        self.assertEqual(len(self.records), 1)
        self.assertIn("f421220", self.records[0]["media_url"])
        self.registry.observe(info("https://cdn.test/video2/v.f421220.m3u8?sign=1"))
        self.assertEqual(len(self.records), 2)

    def test_master_merges_already_seen_variants_and_audio(self):
        base = "https://cdn.test/"
        for child in ("low.m3u8", "high.m3u8", "audio.m3u8"):
            self.registry.observe(info(base + child))
        master = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=100\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=200\nhigh.m3u8\n'
        self.registry.observe(info(base + "master.m3u8"), master)
        self.registry.observe(info(base + "high.m3u8"))
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records[0]["media_url"], base + "master.m3u8")

    def test_nested_master_and_relative_urls(self):
        self.registry.observe(info("https://cdn.test/a/sub.m3u8"), '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\n../video.m3u8\n')
        self.registry.observe(info("https://cdn.test/master.m3u8"), '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\na/sub.m3u8\n')
        self.registry.observe(info("https://cdn.test/a/sub.m3u8"), '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\n../video.m3u8\n')
        self.registry.observe(info("https://cdn.test/video.m3u8"))
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records[0]["media_url"], "https://cdn.test/master.m3u8")

    def test_segments_are_not_playlists(self):
        self.assertEqual(playlist_children("https://cdn.test/a.m3u8", "#EXTM3U\n#EXTINF:2\npart.ts\n"), [])
        self.assertIsNone(media_kind("https://cdn.test/part.ts"))
        self.assertIsNone(media_kind("https://cdn.test/a.m3u8", "text/html"))
        self.assertEqual(media_kind("https://cdn.test/opaque", "application/vnd.apple.mpegurl"), "hls")
        self.assertEqual(media_kind("https://cdn.test/video.mp4?sign=1"), "mp4")

    def test_unknown_query_parameters_are_not_discarded(self):
        self.assertNotEqual(media_key("https://cdn.test/a.m3u8?token=one"),
                            media_key("https://cdn.test/a.m3u8?token=two"))

    def test_shared_audio_does_not_merge_distinct_videos(self):
        for name in ("one", "two"):
            self.registry.observe(info(f"https://cdn.test/{name}.m3u8"),
                f'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=100\n{name}-video.m3u8\n')
        self.assertEqual(len(self.records), 2)

    def test_mp4_hls_fragments_are_removed_and_ignored(self):
        self.registry.observe(info("https://cdn.test/init.mp4"))
        self.registry.observe(info("https://cdn.test/part.mp4"))
        self.registry.observe(info("https://cdn.test/media.m3u8"),
            '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:1,\npart.mp4\n')
        self.registry.observe(info("https://cdn.test/part.mp4"))
        self.assertEqual(len(self.records), 1)
        self.assertIsNone(media_kind("https://cdn.test/part.m4s", "video/mp4"))


class LocalFixture:
    def __init__(self, directory):
        self.directory = directory
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                status = 200
                kind = "text/html"
                body = b"<title>Shared title</title><h1>Shared lesson</h1>"
                if path == "/autoplay":
                    body += b"<script>fetch('/one.m3u8')</script>"
                if path == "/master.m3u8":
                    kind = "application/vnd.apple.mpegurl"
                    body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=32x32\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=64x64\nhigh.m3u8\n"
                elif path.endswith(".m3u8") or path == "/opaque":
                    kind = "application/vnd.apple.mpegurl"
                    generated = fixture.directory / "stream.m3u8"
                    body = generated.read_bytes() if generated.exists() else b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nstream0.ts\n#EXT-X-ENDLIST\n"
                elif path.endswith(".ts"):
                    kind = "video/mp2t"
                    body = (fixture.directory / path.lstrip("/")).read_bytes()
                elif path.endswith(".mp4"):
                    kind, body = "video/mp4", b"fixture"
                if path == "/denied.m3u8":
                    status = 403
                if path == "/login.m3u8":
                    kind, body = "text/html", b"Login required"
                if path in {"/master.m3u8", "/low.m3u8", "/high.m3u8"} or path.endswith(".ts"):
                    if ("session=fixture" not in self.headers.get("Cookie", "")
                            or not self.headers.get("Referer", "").startswith(fixture.base)
                            or self.headers.get("User-Agent") != "CaptureTest/1.0"):
                        status, body = 403, b"Missing playback authentication"
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ARTIFACTS)
        self.directory = Path(self.temp.name)
        self.fixture = LocalFixture(self.directory)
        self.context = self.browser.new_context(user_agent="CaptureTest/1.0")
        self.context.add_cookies([dict(name="session", value="fixture", url=self.fixture.base)])
        self.records = []
        self.capture = BrowserCapture(self.context, self.records, self.directory / "session", lambda _: None)
        self.page = self.context.new_page()
        self.page.goto(self.fixture.base + "/lesson")

    def tearDown(self):
        self.capture.detach()
        self.context.close()
        self.fixture.close()
        self.temp.cleanup()

    def fetch(self, page, path):
        page.evaluate("path => fetch(path).then(r => r.text())", path)
        page.wait_for_timeout(50)
        self.capture.drain()

    def test_tabs_popup_iframe_spa_and_close(self):
        self.fetch(self.page, "/one.m3u8?sign=old")
        self.fetch(self.page, "/one.m3u8?sign=new")
        with self.page.expect_popup() as opened:
            self.page.evaluate("url => window.open(url)", self.fixture.base + "/popup")
        popup = opened.value
        popup.wait_for_load_state()
        self.fetch(popup, "/two.m3u8")
        self.page.evaluate("() => { history.pushState({}, '', '/lesson/next'); document.body.innerHTML = '<h1>Next lesson</h1><iframe src=\"/frame\"></iframe>'; }")
        self.page.frame_locator("iframe").locator("h1").wait_for()
        frame = self.page.frames[1]
        self.fetch(frame, "/three.m3u8")
        self.fetch(self.page, "/four.mp4")
        self.fetch(self.page, "/opaque")
        self.fetch(self.page, "/denied.m3u8")
        self.fetch(self.page, "/login.m3u8")
        self.assertEqual(len(self.records), 5)
        self.assertEqual(self.records[0]["page_url"], self.fixture.base + "/lesson")
        self.assertIn("sign=new", self.records[0]["media_url"])
        self.assertEqual(self.records[1]["page_url"], self.fixture.base + "/popup")
        self.assertEqual(self.records[2]["page_url"], self.fixture.base + "/lesson/next")
        self.assertEqual(self.records[2]["title"], "Next lesson")
        self.context.close()
        self.capture.drain()
        self.capture.persist()
        self.assertTrue(self.capture.closed)
        self.assertEqual(len(json.loads((self.directory / "session/videos.json").read_text())), 5)

    def test_master_children_collapse_and_authenticated_download(self):
        subprocess.run([ffmpeg_executable(), "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=10",
                        "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "hls", "-hls_time", "1",
                        str(self.directory / "stream.m3u8")], check=True)
        self.fetch(self.page, "/low.m3u8")
        self.fetch(self.page, "/master.m3u8")
        self.fetch(self.page, "/high.m3u8")
        self.assertEqual(len(self.records), 1)
        self.assertTrue(self.records[0]["media_url"].endswith("/master.m3u8"))
        downloader = VideoDownloader(output_dir=str(self.directory / "downloads"))
        downloader.load_captured_info(str(self.directory / "session/videos.json"))
        self.context.close()
        self.assertEqual(downloader.download_all(), (1, 0))
        files = list((self.directory / "downloads").glob("*.mp4"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes()[4:8], b"ftyp")
        subprocess.run([ffmpeg_executable(), "-v", "error", "-xerror", "-i", str(files[0]), "-f", "null", "-"],
                       check=True, capture_output=True)
        self.assertFalse(list((self.directory / "downloads").glob("*Frag*")))
        self.assertFalse(list((self.directory / "downloads").glob("*.mem.*")))

    def run_interactive(self, finish):
        self.capture.detach()
        downloader = VideoDownloader(output_dir=str(self.directory / "downloads"))
        calls = 0

        def stop_requested():
            nonlocal calls
            calls += 1
            if calls < 3:
                return False
            if finish == "interrupt":
                raise KeyboardInterrupt
            if finish == "close":
                self.context.close()
                return False
            return True

        def start(context):
            return BrowserCapture(context, downloader.captured_urls, self.directory / "interactive", lambda _: None)

        with patch.object(downloader, "_launch_browser", return_value=self.context), \
                patch.object(downloader, "_start_capture", side_effect=start), \
                patch("main.StopInput.requested", side_effect=stop_requested), \
                patch("main.sync_playwright"):
            records = downloader.capture_interactive(self.fixture.base + "/autoplay")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(json.loads((self.directory / "interactive/videos.json").read_text())), 1)

    def test_interactive_enter_keeps_results(self):
        self.run_interactive("enter")

    def test_interactive_ctrl_c_keeps_results(self):
        self.run_interactive("interrupt")

    def test_interactive_browser_close_keeps_results(self):
        self.run_interactive("close")

    def test_existing_url_list_mode(self):
        self.capture.detach()
        downloader = VideoDownloader(output_dir=str(self.directory / "downloads"))
        def start(context):
            return BrowserCapture(context, downloader.captured_urls, self.directory / "batch", lambda _: None)
        with patch.object(downloader, "_launch_browser", return_value=self.context), \
                patch.object(downloader, "_start_capture", side_effect=start), patch("main.sync_playwright"):
            records = downloader.capture_video_urls([self.fixture.base + "/autoplay"], headless=True, wait_time=0.2)
        self.assertEqual(len(records), 1)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ARTIFACTS)
        self.directory = Path(self.temp.name)
        self.downloader = VideoDownloader(output_dir=str(self.directory / "downloads"))

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_json_and_collision_safe_output(self):
        path = self.directory / "legacy.json"
        path.write_text(json.dumps([info("https://cdn.test/1.m3u8", "100% %(id)s"), info("https://cdn.test/2.m3u8", "100% %(id)s")]))
        self.downloader.load_captured_info(str(path))
        with patch("main.subprocess.run") as run:
            self.assertEqual(self.downloader.download_all(), (2, 0))
        templates = [call.args[0][call.args[0].index("-o") + 1] for call in run.call_args_list]
        self.assertNotEqual(*templates)
        self.assertIn("100%% %%(id)s", templates[0])

    def test_invalid_record_does_not_replace_existing_results(self):
        self.downloader.captured_urls = [info("https://cdn.test/keep.m3u8")]
        path = self.directory / "invalid.json"
        path.write_text('[{"media_url":"file:///bad"}]')
        with self.assertRaises(ValueError):
            self.downloader.load_captured_info(str(path))
        self.assertEqual(len(self.downloader.captured_urls), 1)

    def test_enter_does_not_consume_download_confirmation(self):
        from io import StringIO
        stream = StringIO("\ny\n")
        with patch("capture.sys.stdin", stream), patch("capture.select.select", return_value=([stream], [], [])):
            self.assertTrue(StopInput().requested())
            self.assertEqual(stream.readline(), "y\n")


if __name__ == "__main__":
    unittest.main()
