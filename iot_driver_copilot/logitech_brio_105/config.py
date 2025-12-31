import os
from dataclasses import dataclass


def _get_env(name: str, default: str) -> str:
    val = os.getenv(name)
    return val if val is not None else default


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _get_str(name: str, default: str) -> str:
    return _get_env(name, default)


@dataclass
class Config:
    http_host: str
    http_port: int

    cam_device: str
    cam_width: int
    cam_height: int
    cam_fps: int
    jpeg_quality: int

    backoff_base_secs: float
    backoff_max_secs: float
    read_errors_before_reopen: int
    capture_timeout_secs: float

    log_level: str


def load_config() -> Config:
    return Config(
        http_host=_get_str("HTTP_HOST", "0.0.0.0"),
        http_port=_get_int("HTTP_PORT", 8000),
        cam_device=_get_str("CAM_DEVICE", "0"),
        cam_width=_get_int("CAM_WIDTH", 1280),
        cam_height=_get_int("CAM_HEIGHT", 720),
        cam_fps=_get_int("CAM_FPS", 30),
        jpeg_quality=_get_int("JPEG_QUALITY", 85),
        backoff_base_secs=_get_float("BACKOFF_BASE_SECS", 0.5),
        backoff_max_secs=_get_float("BACKOFF_MAX_SECS", 10.0),
        read_errors_before_reopen=_get_int("READ_ERRORS_BEFORE_REOPEN", 10),
        capture_timeout_secs=_get_float("CAPTURE_TIMEOUT_SECS", 3.0),
        log_level=_get_str("LOG_LEVEL", "INFO"),
    )
