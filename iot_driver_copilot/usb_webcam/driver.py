import os
import sys
import time
import threading
import logging
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

import cv2  # Requires opencv-python

from config import Config


class FrameGrabber:
    def __init__(self, cfg: Config, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cap: Optional[cv2.VideoCapture] = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._latest_frame_bgr = None  # numpy ndarray
        self._latest_jpeg: Optional[bytes] = None
        self._last_update_ts: float = 0.0
        self._seq: int = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="FrameGrabber", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._release_cap()

    def _open_cap(self) -> bool:
        src = self.cfg.cam_device
        try:
            if isinstance(src, int):
                cap = cv2.VideoCapture(src, cv2.CAP_ANY)
            else:
                cap = cv2.VideoCapture(src)
            if not cap or not cap.isOpened():
                self.log.warning("Failed to open camera source: %s", src)
                if cap:
                    cap.release()
                return False
            # Apply settings
            if self.cfg.cam_width:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.cam_width)
            if self.cfg.cam_height:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.cam_height)
            if self.cfg.cam_fps:
                cap.set(cv2.CAP_PROP_FPS, self.cfg.cam_fps)
            self._cap = cap
            self.log.info("Camera opened: %s (requested %dx%d@%s FPS)", src, self.cfg.cam_width, self.cfg.cam_height, self.cfg.cam_fps)
            return True
        except Exception as e:
            self.log.exception("Exception opening camera: %s", e)
            return False

    def _release_cap(self):
        try:
            if self._cap is not None:
                self._cap.release()
                self.log.info("Camera released")
        except Exception as e:
            self.log.warning("Error releasing camera: %s", e)
        finally:
            self._cap = None

    def _run(self):
        backoff_ms = self.cfg.reconnect_backoff_initial_ms
        while not self._stop.is_set():
            if not self._open_cap():
                if self._stop.is_set():
                    break
                self.log.info("Retrying open in %d ms", backoff_ms)
                self._stop.wait(backoff_ms / 1000.0)
                backoff_ms = min(backoff_ms * 2, self.cfg.reconnect_backoff_max_ms)
                continue

            # Open success resets backoff
            backoff_ms = self.cfg.reconnect_backoff_initial_ms
            consecutive_errors = 0
            last_good_ts = time.monotonic()

            while not self._stop.is_set() and self._cap and self._cap.isOpened():
                ret, frame = self._cap.read()
                now = time.monotonic()
                if not ret or frame is None:
                    consecutive_errors += 1
                    # If we haven't received good frame for too long or too many errors, reconnect
                    if (now - last_good_ts) > self.cfg.read_timeout_sec or consecutive_errors >= self.cfg.read_error_threshold:
                        self.log.warning(
                            "Camera read timeout or too many errors (errors=%d, dt=%.2fs). Reconnecting...",
                            consecutive_errors, now - last_good_ts,
                        )
                        break
                    time.sleep(0.02)
                    continue

                consecutive_errors = 0
                last_good_ts = now
                # Encode JPEG once here for MJPEG streaming and snapshot default
                try:
                    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.jpeg_quality]
                    ok, jpg = cv2.imencode('.jpg', frame, encode_params)
                    if not ok:
                        self.log.debug("JPEG encode failed for a frame")
                        continue
                    jpg_bytes = jpg.tobytes()
                except Exception as e:
                    self.log.debug("JPEG encode exception: %s", e)
                    continue

                with self._cond:
                    self._latest_frame_bgr = frame
                    self._latest_jpeg = jpg_bytes
                    self._last_update_ts = time.time()
                    self._seq += 1
                    self._cond.notify_all()

            # Exited inner loop; release and backoff
            self._release_cap()
            if self._stop.is_set():
                break
            self.log.info("Reconnecting after %d ms", backoff_ms)
            self._stop.wait(backoff_ms / 1000.0)
            backoff_ms = min(backoff_ms * 2, self.cfg.reconnect_backoff_max_ms)

    def wait_for_new_frame(self, last_seq: int, timeout: float) -> bool:
        with self._cond:
            if self._seq != last_seq and self._latest_jpeg is not None:
                return True
            self._cond.wait(timeout)
            return self._seq != last_seq and self._latest_jpeg is not None

    def get_latest_jpeg(self) -> Optional[Tuple[bytes, int, float]]:
        with self._lock:
            if self._latest_jpeg is None:
                return None
            return self._latest_jpeg, self._seq, self._last_update_ts

    def get_latest_png(self) -> Optional[Tuple[bytes, int, float]]:
        with self._lock:
            if self._latest_frame_bgr is None:
                return None
            try:
                ok, png = cv2.imencode('.png', self._latest_frame_bgr)
                if not ok:
                    return None
                return png.tobytes(), self._seq, self._last_update_ts
            except Exception:
                return None


grabber: Optional[FrameGrabber] = None
cfg: Optional[Config] = None
logger = logging.getLogger("usb-webcam-driver")


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "USBWebcamHTTP/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)

    def do_GET(self):
        if self.path == "/frame":
            self.handle_frame()
            return
        if self.path == "/stream":
            self.handle_stream()
            return
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Not Found")

    def handle_frame(self):
        global grabber
        if grabber is None:
            self._service_unavailable("grabber not initialized")
            return

        accept = self.headers.get("Accept", "image/jpeg").lower()
        prefer_png = "image/png" in accept and "image/jpeg" not in accept

        # Wait briefly if no frame yet
        data_tuple = grabber.get_latest_png() if prefer_png else grabber.get_latest_jpeg()
        start_wait = time.time()
        while data_tuple is None and (time.time() - start_wait) < cfg.read_timeout_sec:
            # Wait for frame
            if grabber.wait_for_new_frame(-1, timeout=0.2):
                data_tuple = grabber.get_latest_png() if prefer_png else grabber.get_latest_jpeg()

        if data_tuple is None:
            self._service_unavailable("no frame available")
            return

        data, seq, ts = data_tuple
        content_type = "image/png" if prefer_png else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def handle_stream(self):
        global grabber
        if grabber is None:
            self._service_unavailable("grabber not initialized")
            return

        boundary = "frame"
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        last_seq = -1
        try:
            while True:
                # Wait for a new frame or timeout
                got_new = grabber.wait_for_new_frame(last_seq, timeout=1.0)
                if not got_new:
                    # No new frame; still send nothing, loop continues
                    continue
                tup = grabber.get_latest_jpeg()
                if tup is None:
                    continue
                data, last_seq, ts = tup
                headers = (
                    f"--{boundary}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(data)}\r\n\r\n"
                ).encode("ascii")
                self.wfile.write(headers)
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            # Client disconnected
            pass
        except Exception as e:
            logger.warning("Stream error: %s", e)
        finally:
            try:
                self.wfile.write(f"--{boundary}--\r\n".encode("ascii"))
            except Exception:
                pass

    def _service_unavailable(self, reason: str):
        self.send_response(503)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"Service Unavailable: {reason}".encode("utf-8"))


def _setup_logging(level: str):
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )


def main():
    global grabber, cfg, logger

    cfg = Config.from_env()
    _setup_logging(cfg.log_level)
    logger.info("Starting USB webcam driver HTTP server on %s:%d", cfg.http_host, cfg.http_port)

    grabber = FrameGrabber(cfg, logger)
    grabber.start()

    httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port), RequestHandler)

    stop_event = threading.Event()

    def handle_signal(signum, frame):
        logger.info("Signal %s received, shutting down...", signum)
        stop_event.set()
        # Shutdown HTTP server
        try:
            httpd.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        handle_signal("KeyboardInterrupt", None)
    finally:
        logger.info("HTTP server stopped")
        if grabber:
            grabber.stop()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logging.getLogger("usb-webcam-driver").exception("Fatal error: %s", e)
        sys.exit(1)
