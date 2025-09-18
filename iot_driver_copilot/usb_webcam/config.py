import os
from dataclasses import dataclass
from typing import Union


def _get_env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _get_env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _get_env_str(name: str, default: str) -> str:
    val = os.getenv(name)
    return default if val is None else val


def _parse_cam_device(value: str) -> Union[int, str]:
    try:
        return int(value)
    except ValueError:
        return value


@dataclass
class Config:
    http_host: str
    http_port: int

    cam_device: Union[int, str]
    cam_width: int
    cam_height: int
    cam_fps: int

    jpeg_quality: int

    read_timeout_sec: float
    reconnect_backoff_initial_ms: int
    reconnect_backoff_max_ms: int
    read_error_threshold: int

    log_level: str

    @staticmethod
    def from_env() -> "Config":
        return Config(
            http_host=_get_env_str("HTTP_HOST", "0.0.0.0"),
            http_port=_get_env_int("HTTP_PORT", 8000),

            cam_device=_parse_cam_device(_get_env_str("CAM_DEVICE", "0")),
            cam_width=_get_env_int("CAM_WIDTH", 640),
            cam_height=_get_env_int("CAM_HEIGHT", 480),
            cam_fps=_get_env_int("CAM_FPS", 30),

            jpeg_quality=_get_env_int("JPEG_QUALITY", 80),

            read_timeout_sec=_get_env_float("READ_TIMEOUT_SEC", 5.0),
            reconnect_backoff_initial_ms=_get_env_int("RECONNECT_BACKOFF_INITIAL_MS", 500),
            reconnect_backoff_max_ms=_get_env_int("RECONNECT_BACKOFF_MAX_MS", 10000),
            read_error_threshold=_get_env_int("READ_ERROR_THRESHOLD", 30),

            log_level=_get_env_str("LOG_LEVEL", "INFO"),
        )
