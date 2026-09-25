"""MJPEG 解析测试：boundary 提取与分片流解析。"""

from __future__ import annotations

import io
import sys
import unittest
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import video_client  # noqa: E402

FAKE_JPEG_A = b"\xff\xd8FAKE-A\xff\xd9"
FAKE_JPEG_B = b"\xff\xd8FAKE-BB\xff\xd9"


def make_multipart(frames, boundary: bytes = b"boundarydonotcross") -> bytes:
    parts = []
    for frame in frames:
        parts.append(b"--" + boundary + b"\r\n")
        parts.append(b"Content-Type: image/jpeg\r\n")
        parts.append(b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n")
        parts.append(frame + b"\r\n")
    parts.append(b"--" + boundary + b"\r\n")  # 下一帧的分隔符（流式解析依赖它判定前一帧结束）
    return b"".join(parts)


class ChunkedReader(io.BytesIO):
    """限制单次 read/read1 大小，模拟网络分片。"""

    def __init__(self, data: bytes, chunk: int) -> None:
        super().__init__(data)
        self._chunk = chunk

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._chunk
        return super().read(min(size, self._chunk))

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)


class ExtractBoundaryTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(
            video_client.extract_boundary("multipart/x-mixed-replace; boundary=myboundary"),
            b"myboundary",
        )

    def test_quoted(self):
        self.assertEqual(
            video_client.extract_boundary('multipart/x-mixed-replace; boundary="abc123"'),
            b"abc123",
        )

    def test_missing_uses_default(self):
        self.assertEqual(video_client.extract_boundary(""), video_client.DEFAULT_BOUNDARY)
        self.assertEqual(video_client.extract_boundary("text/html"), video_client.DEFAULT_BOUNDARY)


class JpegFrameIteratorTests(unittest.TestCase):
    def test_extracts_all_frames(self):
        stream = make_multipart([FAKE_JPEG_A, FAKE_JPEG_B])
        frames = list(video_client.iter_jpeg_frames(ChunkedReader(stream, 4096)))
        self.assertEqual(frames, [FAKE_JPEG_A, FAKE_JPEG_B])

    def test_chunked_reads_boundary_spanning(self):
        stream = make_multipart([FAKE_JPEG_A, FAKE_JPEG_B])
        frames = list(video_client.iter_jpeg_frames(ChunkedReader(stream, 7), b"boundarydonotcross"))
        self.assertEqual(frames, [FAKE_JPEG_A, FAKE_JPEG_B])

    def test_single_byte_chunks(self):
        stream = make_multipart([FAKE_JPEG_A])
        frames = list(video_client.iter_jpeg_frames(ChunkedReader(stream, 1), b"boundarydonotcross"))
        self.assertEqual(frames, [FAKE_JPEG_A])

    def test_partial_trailing_frame_is_ignored(self):
        stream = make_multipart([FAKE_JPEG_A]) + b"--boundarydonotcross\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8PART"
        frames = list(video_client.iter_jpeg_frames(ChunkedReader(stream, 64), b"boundarydonotcross"))
        self.assertEqual(frames, [FAKE_JPEG_A])

    def test_empty_stream(self):
        self.assertEqual(list(video_client.iter_jpeg_frames(io.BytesIO(b""))), [])


class SnapshotTests(unittest.TestCase):
    def test_fetch_snapshot_reads_jpeg(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = FAKE_JPEG_A
        with mock.patch("src.video_client.urllib.request.urlopen", return_value=response):
            self.assertEqual(video_client.fetch_snapshot("http://127.0.0.1:8080/?action=snapshot"), FAKE_JPEG_A)

    def test_fetch_snapshot_rejects_non_jpeg(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"not an image"
        with mock.patch("src.video_client.urllib.request.urlopen", return_value=response):
            with self.assertRaises(ValueError):
                video_client.fetch_snapshot("http://127.0.0.1:8080/?action=snapshot")


if __name__ == "__main__":
    unittest.main()
