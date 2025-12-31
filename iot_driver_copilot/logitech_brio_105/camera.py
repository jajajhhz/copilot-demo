import logging
import threading
import time
from typing import Optional, Tuple

import cv2

from config import Config


class CameraManager:
    def __init__(self, config: Config):
        self.config = config
        self.capture: Optional[cv2.VideoCapture] = None
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.latest_jpeg: Optional[bytes] = None
        self.latest_ts: Optional[float] = None
        self.streams_active: int = 0

        self._consecutive_read_errors = 0
        self._backoff_secs = self.config.backoff_base_secs

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="CameraManagerThread", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.capture is not None:
            try:
                self.capture.release()
            except Exception:
                pass
            self.capture = None
        with self.cond:
            self.cond.notify_all()

    def join(self):
        if self.thread:
            self.thread.join(timeout=5)

    def _open_device(self) -> bool:
        # Parse device: index or path
        device_str = self.config.cam_device
        cap = None
        try:
            if device_str.isdigit():
                cap = cv2.VideoCapture(int(device_str))
            else:
                cap = cv2.VideoCapture(device_str)
        except Exception as e:
            logging.error(f"VideoCapture open error: {e}")
            return False

        if not cap or not cap.isOpened():
            logging.warning("Unable to open camera device. Will retry with backoff.")
            if cap:
                cap.release()
            return False

        # Apply settings
        width_ok = cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.config.cam_width))
        height_ok = cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.config.cam_height))
        fps_ok = cap.set(cv2.CAP_PROP_FPS, float(self.config.cam_fps))
        logging.info(
            f"Camera opened. Set width={self.config.cam_width} ({width_ok}), height={self.config.cam_height} ({height_ok}), fps={self.config.cam_fps} ({fps_ok})."
        )

        self.capture = cap
        self._consecutive_read_errors = 0
        self._backoff_secs = self.config.backoff_base_secs
        return True

    def _encode_jpeg(self, frame) -> Optional[bytes]:
        try:
            params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.config.jpeg_quality)]
            ok, buf = cv2.imencode('.jpg', frame, params)
            if not ok:
                return None
            return buf.tobytes()
        except Exception as e:
            logging.error(f"JPEG encode error: {e}")
            return None

    def _run(self):
        logging.info("Camera manager loop started.")
        while not self.stop_event.is_set():
            if self.capture is None or not self.capture.isOpened():
                if not self._open_device():
                    # Exponential backoff
                    sleep_s = self._backoff_secs
                    self._backoff_secs = min(self._backoff_secs * 2, self.config.backoff_max_secs)
                    logging.info(f"Retrying camera open in {sleep_s:.2f}s (backoff up to {self.config.backoff_max_secs}s).")
                    self._sleep_interruptible(sleep_s)
                    continue

            # Read frame
            ret, frame = self.capture.read()
            if not ret or frame is None:
                self._consecutive_read_errors += 1
                logging.warning(
                    f"Camera read failed ({self._consecutive_read_errors}/{self.config.read_errors_before_reopen})."
                )
                if self._consecutive_read_errors >= self.config.read_errors_before_reopen:
                    logging.warning("Too many read errors; reopening camera.")
                    try:
                        self.capture.release()
                    except Exception:
                        pass
                    self.capture = None
                    self._consecutive_read_errors = 0
                    # immediate reopen attempt on next loop
                else:
                    self._sleep_interruptible(0.01)
                continue

            self._consecutive_read_errors = 0
            jpeg = self._encode_jpeg(frame)
            if jpeg is None:
                # Treat as read error
                self._sleep_interruptible(0.01)
                continue

            now = time.time()
            with self.cond:
                self.latest_jpeg = jpeg
                self.latest_ts = now
                self.cond.notify_all()
            # Throttle loop roughly to desired FPS
            self._sleep_interruptible(max(0.0, (1.0 / max(1, self.config.cam_fps)) - 0.0005))

        logging.info("Camera manager loop exiting.")

    def _sleep_interruptible(self, seconds: float):
        end = time.time() + seconds
        while not self.stop_event.is_set() and time.time() < end:
            time.sleep(min(0.05, max(0.0, end - time.time())))

    # Public API for HTTP handlers

    def is_ready(self) -> bool:
        with self.lock:
            return self.latest_jpeg is not None

    def streaming_active(self) -> bool:
        with self.lock:
            return self.streams_active > 0

    def get_last_update(self) -> Optional[float]:
        with self.lock:
            return self.latest_ts

    def get_latest_frame(self) -> Optional[Tuple[bytes, float]]:
        with self.lock:
            if self.latest_jpeg is None:
                return None
            return self.latest_jpeg, self.latest_ts if self.latest_ts else time.time()

    def wait_for_next_frame(self, timeout: float) -> Optional[bytes]:
        deadline = time.time() + timeout
        with self.cond:
            start_ts = self.latest_ts
            while True:
                if self.stop_event.is_set():
                    return None
                if self.latest_jpeg is not None and self.latest_ts is not None and self.latest_ts != start_ts:
                    return self.latest_jpeg
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self.cond.wait(timeout=min(remaining, 0.5))

    def wait_for_frame_after(self, ts: Optional[float], timeout: float) -> Optional[Tuple[bytes, float]]:
        deadline = time.time() + timeout
        with self.cond:
            while True:
                if self.stop_event.is_set():
                    return None
                if self.latest_jpeg is not None and self.latest_ts is not None:
                    if ts is None or self.latest_ts > ts:
                        return self.latest_jpeg, self.latest_ts
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self.cond.wait(timeout=min(remaining, 0.5))

    def increment_streams(self):
        with self.lock:
            self.streams_active += 1
        logging.info(f"Stream client connected. Active streams: {self.streams_active}")

    def decrement_streams(self):
        with self.lock:
            self.streams_active = max(0, self.streams_active - 1)
        logging.info(f"Stream client disconnected. Active streams: {self.streams_active}")
