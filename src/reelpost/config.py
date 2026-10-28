"""Config file in ~/.config/reelpost, overridable with environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from reelpost.constants import DEFAULT_GRAPH_VERSION
from reelpost.errors import ReelpostError
from reelpost.graph import normalize_version

_FILE_KEYS = (
    "graph_version",
    "app_id",
    "config_id",
    "user_access_token",
    "user_token_expires_at",
    "page_id",
    "page_name",
    "page_access_token",
    "ig_user_id",
    "ig_username",
    "youtube_client_id",
    "youtube_refresh_token",
    "youtube_access_token",
    "youtube_access_expires_at",
    "youtube_channel_id",
    "youtube_channel_title",
)

_YOUTUBE_KEYS = (
    "youtube_client_id",
    "youtube_refresh_token",
    "youtube_access_token",
    "youtube_access_expires_at",
    "youtube_channel_id",
    "youtube_channel_title",
)


@dataclass
class Settings:
    graph_version: str = DEFAULT_GRAPH_VERSION
    app_id: str | None = None
    config_id: str | None = None
    user_access_token: str | None = None
    user_token_expires_at: str | None = None
    page_id: str | None = None
    page_name: str | None = None
    page_access_token: str | None = None
    ig_user_id: str | None = None
    ig_username: str | None = None
    youtube_client_id: str | None = None
    youtube_refresh_token: str | None = None
    youtube_access_token: str | None = None
    youtube_access_expires_at: str | None = None
    youtube_channel_id: str | None = None
    youtube_channel_title: str | None = None


def config_dir() -> Path:
    override = os.environ.get("REELPOST_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "reelpost"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings() -> Settings:
    data = _read_file()
    settings = Settings(
        graph_version=normalize_version(str(data.get("graph_version") or DEFAULT_GRAPH_VERSION)),
        app_id=_clean(data.get("app_id")),
        config_id=_clean(data.get("config_id")),
        user_access_token=_clean(data.get("user_access_token")),
        user_token_expires_at=_clean(data.get("user_token_expires_at")),
        page_id=_clean(data.get("page_id")),
        page_name=_clean(data.get("page_name")),
        page_access_token=_clean(data.get("page_access_token")),
        ig_user_id=_clean(data.get("ig_user_id")),
        ig_username=_clean(data.get("ig_username")),
        youtube_client_id=_clean(data.get("youtube_client_id")),
        youtube_refresh_token=_clean(data.get("youtube_refresh_token")),
        youtube_access_token=_clean(data.get("youtube_access_token")),
        youtube_access_expires_at=_clean(data.get("youtube_access_expires_at")),
        youtube_channel_id=_clean(data.get("youtube_channel_id")),
        youtube_channel_title=_clean(data.get("youtube_channel_title")),
    )
    _apply_env(settings)
    settings.graph_version = normalize_version(settings.graph_version)
    return settings


def save_settings(settings: Settings) -> Path:
    payload = {key: value for key, value in asdict(settings).items() if key in _FILE_KEYS}
    return write_config(payload)


def write_config(payload: dict) -> Path:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / "config.json"
    temporary = directory / "config.json.tmp"
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return path


def update_saved_page(page: dict) -> None:
    """Remember a Page chosen during publish. Does nothing until login has created a config file."""
    path = config_path()
    if not path.is_file():
        return
    data = _read_file()
    data["page_id"] = page["id"]
    data["page_name"] = page.get("name")
    data["page_access_token"] = page.get("access_token")
    data["ig_user_id"] = page.get("ig_user_id")
    data["ig_username"] = page.get("ig_username")
    write_config(data)


def update_youtube_auth(fields: dict) -> None:
    """Store a refreshed YouTube access token. Does nothing until login has created a config file."""
    path = config_path()
    if not path.is_file():
        return
    data = _read_file()
    for key, value in fields.items():
        if key in _YOUTUBE_KEYS:
            data[key] = value
    write_config(data)


def clear_youtube_auth() -> bool:
    path = config_path()
    if not path.is_file():
        return False
    data = _read_file()
    found = any(data.get(key) for key in _YOUTUBE_KEYS)
    if not found:
        return False
    for key in _YOUTUBE_KEYS:
        data.pop(key, None)
    write_config(data)
    return True


def delete_config() -> bool:
    path = config_path()
    if not path.is_file():
        return False
    path.unlink()
    return True


def _read_file() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReelpostError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReelpostError(f"{path} is not a JSON object.")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        # Group or world can read the token file. Tighten it.
        os.chmod(path, 0o600)
    return data


def _apply_env(settings: Settings) -> None:
    mapping = {
        "REELPOST_GRAPH_VERSION": "graph_version",
        "REELPOST_APP_ID": "app_id",
        "REELPOST_CONFIG_ID": "config_id",
        "REELPOST_USER_TOKEN": "user_access_token",
        "REELPOST_PAGE_ID": "page_id",
        "REELPOST_PAGE_NAME": "page_name",
        "REELPOST_PAGE_TOKEN": "page_access_token",
        "REELPOST_IG_USER_ID": "ig_user_id",
        "REELPOST_IG_USERNAME": "ig_username",
        "REELPOST_YOUTUBE_CLIENT_ID": "youtube_client_id",
        "REELPOST_YOUTUBE_REFRESH_TOKEN": "youtube_refresh_token",
        "REELPOST_YOUTUBE_ACCESS_TOKEN": "youtube_access_token",
        "REELPOST_YOUTUBE_CHANNEL_ID": "youtube_channel_id",
        "REELPOST_YOUTUBE_CHANNEL_TITLE": "youtube_channel_title",
    }
    for env_name, field in mapping.items():
        value = _clean(os.environ.get(env_name))
        if value is not None:
            setattr(settings, field, value)


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
