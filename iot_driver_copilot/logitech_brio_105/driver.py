import os
import signal
import sys
import threading
import time
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config import Config, load_config
from camera import CameraManager


class CameraHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "BrioDriverHTTP/1.0"
    protocol_version = "HTTP/1.1"
    manager: CameraManager = None  # will be injected

    def log_message(self, format, *args):
        logging.info("%s - - [%s] " + format,
                     self.client_address[0],
                     time.strftime("%d/%b/%Y %H:%M:%S"),
                     *args)

    def _send_json(self, status_code: int, payload: str):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    def do_GET(self):
        if self.path == "/status":
            ready = self.manager.is_ready()
            streaming = self.manager.streaming_active()
            last_update = self.manager.get_last_update()
            payload = {
                "ready": ready,
                "streaming": streaming,
                "last_update_epoch_ms": int(last_update * 1000) if last_update else None
            }
            import json
            self._send_json(200, json.dumps(payload))
            return

        if self.path == "/stream":
            self._handle_stream()
            return

        self._send_json(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path == "/capture":
            self._handle_capture()
            return
        self._send_json(404, '{"error":"not found"}')

    def _handle_capture(self):
        if not self.manager.is_ready():
            self._send_json(503, '{"error":"device not ready"}')
            return
        # Wait for next frame (fresh) to honor "next available frame"
        jpeg_bytes = self.manager.wait_for_next_frame(timeout=self.manager.config.capture_timeout_secs)
        if jpeg_bytes is None:
            self._send_json(504, '{"error":"timeout waiting for frame"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg_bytes)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(jpeg_bytes)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_stream(self):
        # Multipart MJPEG stream
        self.manager.increment_streams()
        boundary = "frame"
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.end_headers()

        last_ts = None
        try:
            while True:
                if self.manager.stop_event.is_set():
                    break
                jpeg_bytes, ts = self.manager.wait_for_frame_after(last_ts, timeout=5.0)
                if jpeg_bytes is None:
                    # If not ready, just continue waiting
                    continue
                last_ts = ts
                try:
                    part_headers = (
                        f"--{boundary}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg_bytes)}\r\n\r\n"
                    ).encode("utf-8")
                    self.wfile.write(part_headers)
                    self.wfile.write(jpeg_bytes)
                    self.wfile.write(b"\r\n")
                    try:
                        self.wfile.flush()
                    except Exception:
                        pass
                except (BrokenPipeError, ConnectionResetError):
                    break
        finally:
            self.manager.decrement_streams()


def run_server(cfg: Config, manager: CameraManager):
    CameraHTTPRequestHandler.manager = manager

    httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port), CameraHTTPRequestHandler)
    # Ensure spawned threads don't block shutdown
    httpd.daemon_threads = True

    stop_event = threading.Event()

    def handle_signal(sig, frame):
        logging.info(f"Received signal {sig}, initiating graceful shutdown...")
        stop_event.set()
        try:
            httpd.shutdown()
        except Exception:
            pass
        manager.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logging.info(f"HTTP server listening on {cfg.http_host}:{cfg.http_port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        handle_signal(signal.SIGINT, None)
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass


def main():
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    logging.info("Starting Logitech Brio 105 driver...")
    manager = CameraManager(cfg)
    manager.start()
    try:
        run_server(cfg, manager)
    finally:
        manager.stop()
        manager.join()
        logging.info("Driver shutdown complete.")


if __name__ == "__main__":
    main()
