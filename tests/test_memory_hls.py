import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import urlsplit

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from memory_hls import CheckpointWriter, FragmentTooLarge, MemoryHlsFD, MIB, OrderedMemoryPipeline, ResumeError
import download_worker


ARTIFACTS = Path(__file__).resolve().parents[1] / ".test-artifacts"
ARTIFACTS.mkdir(exist_ok=True)


class MemoryCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ARTIFACTS)
        self.directory = Path(self.temp.name)
        self.output = self.directory / "video.mp4"

    def tearDown(self):
        self.temp.cleanup()

    def test_out_of_order_is_bounded_and_written_in_order(self):
        pipeline = OrderedMemoryPipeline(workers=2, cache_bytes=32 * MIB, batch_bytes=MIB)
        writer = CheckpointWriter(self.output, "test", 40, MIB)
        started = []
        release_first = threading.Event()
        window_reached = threading.Event()
        errors = []

        def fetch(fragment, limit, cancel):
            started.append(fragment)
            if len(started) == pipeline.window:
                window_reached.set()
            if fragment == 0:
                if not release_first.wait(5):
                    raise RuntimeError("test timeout")
            return str(fragment).encode() + b"|"

        def run():
            try:
                pipeline.run(list(range(40)), writer, fetch)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(window_reached.wait(5))
            time.sleep(0.1)
            self.assertEqual(len(started), pipeline.window)
            self.assertEqual(writer.index, 0)
        finally:
            release_first.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        writer.finish()
        self.assertEqual(self.output.read_bytes(), b"".join(str(i).encode() + b"|" for i in range(40)))
        self.assertLessEqual(pipeline.max_pending, pipeline.window)
        self.assertEqual(list(self.directory.iterdir()), [self.output])

    def test_missing_fragment_stops_and_resumes_contiguous_prefix(self):
        writer = CheckpointWriter(self.output, "same", 8, MIB)
        def fail(fragment, *_):
            if fragment == 3:
                raise DownloadError("missing")
            return bytes([fragment])
        with self.assertRaises(DownloadError):
            OrderedMemoryPipeline(2, 32 * MIB, MIB).run(list(range(1, 9)), writer, fail)
        writer.close()
        self.assertFalse(self.output.exists())
        self.assertEqual(json.loads(writer.state.read_text())["index"], 2)
        self.assertEqual(writer.part.read_bytes(), b"\x01\x02")
        resumed = CheckpointWriter(self.output, "same", 8, MIB)
        fetched = []
        def fetch(fragment, *_):
            fetched.append(fragment)
            return bytes([fragment])
        OrderedMemoryPipeline(2, 32 * MIB, MIB).run(list(range(1, 9)), resumed, fetch)
        resumed.finish()
        self.assertEqual(min(fetched), 3)
        self.assertEqual(self.output.read_bytes(), bytes(range(1, 9)))

    def test_crash_tail_is_truncated_to_committed_offset(self):
        writer = CheckpointWriter(self.output, "same", 3, MIB)
        writer.append(1, b"first")
        writer.commit()
        writer.append(2, b"uncommitted")
        writer.close()  # 模拟文件写出，但提交记录尚未更新。
        resumed = CheckpointWriter(self.output, "same", 3, MIB)
        self.assertEqual(resumed.index, 1)
        self.assertEqual(resumed.part.read_bytes(), b"first")
        resumed.append(2, b"second")
        resumed.append(3, b"third")
        resumed.finish()
        self.assertEqual(self.output.read_bytes(), b"firstsecondthird")

    def test_checkpoint_replace_failure_does_not_advance_progress(self):
        writer = CheckpointWriter(self.output, "same", 2, MIB)
        writer.append(1, b"first")
        with patch("memory_hls.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                writer.commit()
        writer.close()
        resumed = CheckpointWriter(self.output, "same", 2, MIB)
        self.assertEqual(resumed.index, 0)
        self.assertEqual(resumed.part.stat().st_size, 0)
        resumed.close()

    def test_changed_manifest_and_corrupt_checkpoint_preserve_data(self):
        writer = CheckpointWriter(self.output, "original", 2, MIB)
        writer.append(1, b"keep")
        writer.commit()
        writer.close()
        with self.assertRaises(ResumeError):
            CheckpointWriter(self.output, "changed", 2, MIB)
        self.assertEqual(writer.part.read_bytes(), b"keep")
        writer.state.write_text("invalid json")
        with self.assertRaises(ResumeError):
            CheckpointWriter(self.output, "original", 2, MIB)
        self.assertEqual(writer.part.read_bytes(), b"keep")

    def test_short_part_is_not_silently_appended(self):
        writer = CheckpointWriter(self.output, "original", 2, MIB)
        writer.append(1, b"keep")
        writer.commit()
        writer.close()
        writer.part.write_bytes(b"k")
        with self.assertRaises(ResumeError):
            CheckpointWriter(self.output, "original", 2, MIB)
        self.assertEqual(writer.part.read_bytes(), b"k")

    def test_timer_commits_while_next_fragment_is_missing(self):
        writer = CheckpointWriter(self.output, "same", 2, MIB, interval=0.01)
        writer.append(1, b"first")
        with patch("memory_hls.time.monotonic", return_value=writer.last_commit + 1):
            writer.commit_if_due()
        self.assertEqual(json.loads(writer.state.read_text())["index"], 1)
        writer.close()


class HlsNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ARTIFACTS)
        self.directory = Path(self.temp.name)
        self.resources = {}
        self.failures = Counter()
        self.requests = Counter()
        self.ignore_range = False
        self.no_length = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                fixture.requests[path] += 1
                if fixture.failures[path] > 0:
                    fixture.failures[path] -= 1
                    self.send_response(503)
                    self.end_headers()
                    return
                data = fixture.resources.get(path, b"")
                status, content_range = 200, None
                if self.headers.get("Range") and not fixture.ignore_range:
                    start, end = map(int, self.headers["Range"].removeprefix("bytes=").split("-"))
                    content_range = f"bytes {start}-{end}/{len(data)}"
                    data, status = data[start:end + 1], 206
                self.send_response(status)
                if not fixture.no_length:
                    self.send_header("Content-Length", str(len(data)))
                if content_range:
                    self.send_header("Content-Range", content_range)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def download(self, retries=0, workers=2, memory_mib=32):
        params = dict(quiet=True, noprogress=True, socket_timeout=2, fragment_retries=retries,
                      skip_unavailable_fragments=False, concurrent_fragment_downloads=workers,
                      memory_hls=dict(cache_bytes=memory_mib * MIB, batch_bytes=MIB, interval=0.1))
        with YoutubeDL(params) as ydl:
            return MemoryHlsFD(ydl, params).real_download(str(self.directory / "video.mp4"),
                dict(url=self.base + "/playlist.m3u8", ext="mp4", id="fixture", protocol="m3u8_native"))

    def playlist(self, body):
        self.resources["/playlist.m3u8"] = ("#EXTM3U\n#EXT-X-TARGETDURATION:1\n" + body + "#EXT-X-ENDLIST\n").encode()

    def test_aes_implicit_iv_resume_and_signature_refresh(self):
        key = b"1234567890abcdef"
        first, second = b"first decrypted payload", b"second decrypted payload"
        self.resources["/key.bin"] = key
        self.resources["/first.ts"] = AES.new(key, AES.MODE_CBC, (12).to_bytes(16, "big")).encrypt(pad(first, 16))
        self.resources["/second.ts"] = AES.new(key, AES.MODE_CBC, (13).to_bytes(16, "big")).encrypt(pad(second, 16))
        body = '#EXT-X-MEDIA-SEQUENCE:12\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:1,\nfirst.ts?sign=old\n#EXTINF:1,\nsecond.ts?sign=old\n'
        self.playlist(body)
        self.failures["/second.ts"] = 1
        with self.assertRaises(DownloadError):
            self.download()
        self.assertEqual((self.directory / "video.mp4.mem.part").read_bytes(), first)
        self.playlist(body.replace("sign=old", "sign=new"))
        self.assertTrue(self.download())
        self.assertEqual((self.directory / "video.mp4").read_bytes(), first + second)
        self.assertEqual(self.requests["/first.ts"], 1)

    def test_byte_ranges_and_init_segment_resume(self):
        self.resources["/media.bin"] = b"INITfirstsecond"
        self.resources["/last.ts"] = b"last"
        self.playlist('#EXT-X-MAP:URI="media.bin",BYTERANGE="4@0"\n#EXTINF:1,\n#EXT-X-BYTERANGE:5@4\nmedia.bin\n#EXTINF:1,\n#EXT-X-BYTERANGE:6\nmedia.bin\n#EXTINF:1,\nlast.ts\n')
        self.failures["/last.ts"] = 1
        with self.assertRaises(DownloadError):
            self.download()
        self.assertTrue(self.download())
        self.assertEqual((self.directory / "video.mp4").read_bytes(), b"INITfirstsecondlast")
        self.assertEqual(self.requests["/media.bin"], 3)

    def test_ignored_range_is_rejected(self):
        self.ignore_range = True
        self.resources["/media.bin"] = b"INITfirstsecond"
        self.playlist('#EXTINF:1,\n#EXT-X-BYTERANGE:5@4\nmedia.bin\n')
        with self.assertRaises(DownloadError):
            self.download()
        self.assertFalse((self.directory / "video.mp4").exists())

    def test_transient_failure_retries_without_skipping(self):
        self.resources["/first.ts"] = b"complete"
        self.failures["/first.ts"] = 1
        self.playlist('#EXTINF:1,\nfirst.ts\n')
        self.assertTrue(self.download(retries=1))
        self.assertEqual((self.directory / "video.mp4").read_bytes(), b"complete")
        self.assertEqual(self.requests["/first.ts"], 2)

    def test_large_fragment_falls_back_without_unbounded_memory(self):
        self.resources["/first.ts"] = b"x" * MIB
        self.playlist('#EXTINF:1,\nfirst.ts\n')
        self.assertTrue(self.download(workers=32))
        self.assertEqual((self.directory / "video.mp4").read_bytes(), b"x" * MIB)
        self.assertFalse(list(self.directory.glob("*.mem.*")))

    def test_unknown_length_still_enforces_memory_limit(self):
        self.resources["/first.ts"] = b"x" * MIB
        self.no_length = True
        params = dict(socket_timeout=2, fragment_retries=0)
        with YoutubeDL(params) as ydl:
            fd = MemoryHlsFD(ydl, params)
            with self.assertRaises(FragmentTooLarge):
                fd.fetch_fragment(dict(url=self.base + "/first.ts", frag_index=1),
                                  128 * 1024, threading.Event(), {})


class WorkerSettingsTests(unittest.TestCase):
    def test_low_memory_and_explicit_disk_mode_keep_strict_fragments(self):
        for cache_mib, available in ((256, 100 * MIB), (0, 16 * 1024 * MIB)):
            with self.subTest(cache_mib=cache_mib):
                args = download_worker.build_parser().parse_args([
                    "https://example.test/a.m3u8", "-o", "unused.mp4", "--memory-mib", str(cache_mib)])
                with patch("download_worker.psutil.virtual_memory", return_value=SimpleNamespace(available=available)), \
                        patch("download_worker.ffmpeg_executable", return_value="unused"), \
                        patch("download_worker.yt_dlp.YoutubeDL") as ydl:
                    ydl.return_value.__enter__.return_value.download.return_value = 0
                    self.assertEqual(download_worker.run(args), 0)
                settings = ydl.call_args.args[0]
                self.assertNotIn("memory_hls", settings)
                self.assertFalse(settings["skip_unavailable_fragments"])

    def test_memory_budget_is_capped_and_downloader_registration_restored(self):
        original = download_worker.downloader.HlsFD
        args = download_worker.build_parser().parse_args([
            "https://example.test/a.m3u8", "-o", "unused.mp4", "--memory-mib", "1024"])
        with patch("download_worker.psutil.virtual_memory", return_value=SimpleNamespace(available=1024 * MIB)), \
                patch("download_worker.ffmpeg_executable", return_value="unused"), \
                patch("download_worker.yt_dlp.YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.download.side_effect = DownloadError("fixture")
            with self.assertRaises(DownloadError):
                download_worker.run(args)
        self.assertEqual(ydl.call_args.args[0]["memory_hls"]["cache_bytes"], 128 * MIB)
        self.assertIs(download_worker.downloader.HlsFD, original)


if __name__ == "__main__":
    unittest.main()
