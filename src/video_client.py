"""MJPEG 视频客户端：独立线程读取 ``http://host:port/?action=stream``。

设计约束（交接文档第 9 节）：

- 视频线程与运动控制线程完全隔离，视频失败/卡顿**绝不能**阻塞 STOP；
- 端口/URL 不硬编码，由界面配置；本模块不做任何电机相关操作。

纯解析逻辑（``iter_jpeg_frames``）不依赖 Qt，可单元测试。
"""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request
from typing import Callable, Iterator, Optional

logger = logging.getLogger(__name__)

#: mjpg-streamer output_http 的默认分隔符
DEFAULT_BOUNDARY = b"boundarydonotcross"
DEFAULT_PATH = "/?action=stream"


def extract_boundary(content_type: str) -> bytes:
    """从响应头 ``multipart/x-mixed-replace; boundary=xxx`` 中提取分隔符。"""
    if not content_type:
        return DEFAULT_BOUNDARY
    marker = "boundary="
    position = content_type.lower().find(marker)
    if position == -1:
        return DEFAULT_BOUNDARY
    boundary = content_type[position + len(marker) :].split(";")[0].strip().strip('"')
    return boundary.encode("utf-8", "replace") or DEFAULT_BOUNDARY


def iter_jpeg_frames(
    fileobj,
    boundary: bytes = DEFAULT_BOUNDARY,
    chunk_size: int = 4096,
    max_buffer: int = 8 * 1024 * 1024,
) -> Iterator[bytes]:
    """从 multipart/x-mixed-replace 流中逐个产出 JPEG 字节。

    优先使用 ``read1``（HTTPResponse 可用）：有多少读多少，避免等满缓冲区
    造成视频延迟；对普通 file-like 对象退回 ``read``。
    """
    read = getattr(fileobj, "read1", None) or fileobj.read
    delimiter = b"--" + boundary
    buffer = b""
    while True:
        chunk = read(chunk_size)
        if not chunk:
            return
        buffer += chunk
        if len(buffer) > max_buffer:
            buffer = buffer[-1024 * 1024 :]  # 防止异常流无限增长
        while True:
            start = buffer.find(delimiter)
            if start == -1:
                break
            header_end = buffer.find(b"\r\n\r\n", start + len(delimiter))
            if header_end == -1:
                break
            data_start = header_end + 4
            next_start = buffer.find(delimiter, data_start)
            if next_start == -1:
                break
            frame = buffer[data_start:next_start]
            if frame.endswith(b"\r\n"):
                frame = frame[:-2]
            buffer = buffer[next_start:]
            if frame:
                yield frame


class MJPEGReader:
    """后台线程读取 MJPEG 流，通过回调交付帧与状态（回调可能在工作线程执行）。"""

    def __init__(
        self,
        url: str,
        timeout: float = 3.0,
        on_frame: Optional[Callable[[bytes], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.on_frame = on_frame
        self.on_status = on_status
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._response = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="video-reader", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 1.0) -> None:
        self._stop_event.set()
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:  # pragma: no cover - 关闭异常忽略
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)
        self._thread = None

    def _emit_status(self, message: str) -> None:
        logger.info("视频: %s", message)
        if self.on_status is not None:
            try:
                self.on_status(message)
            except Exception:  # pragma: no cover
                logger.exception("视频状态回调异常")

    def _run(self) -> None:
        self._emit_status(f"连接 {self.url}")
        try:
            request = urllib.request.Request(self.url, headers={"User-Agent": "xiaor-remote/0.1"})
            response = urllib.request.urlopen(request, timeout=self.timeout)
            self._response = response
            boundary = extract_boundary(response.headers.get("Content-Type", ""))
            self._emit_status("视频流已连接")
            for frame in iter_jpeg_frames(response, boundary):
                if self._stop_event.is_set():
                    break
                if self.on_frame is not None:
                    try:
                        self.on_frame(frame)
                    except Exception:  # pragma: no cover
                        logger.exception("视频帧回调异常")
            if not self._stop_event.is_set():
                self._emit_status("视频流结束")
        except (OSError, urllib.error.URLError, ValueError) as exc:
            if not self._stop_event.is_set():
                self._emit_status(f"视频不可用: {exc}")
        finally:
            response = self._response
            self._response = None
            if response is not None:
                try:
                    response.close()
                except Exception:  # pragma: no cover
                    pass
