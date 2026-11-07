"""Configuration loading from config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import bcrypt


@dataclass(frozen=True)
class User:
    name: str
    unix: str
    admin: bool = False


@dataclass
class Config:
    num_gpus: int = 2
    slot_minutes: int = 30
    max_hours_single: float = 24
    max_hours_all: float = 12
    require_approval: bool = False  # False: bookings that clash with nobody are approved at once
    idle_release_minutes: int = 60  # end a running booking if the booker's jobs don't use it this long; 0 = never
    notify_webhook_url: str = ""
    # Zulip: post booking news to a stream through a bot (see README). Leave zulip_site empty to skip.
    zulip_site: str = ""
    zulip_bot_email: str = ""
    zulip_bot_key: str = ""
    zulip_stream: str = ""
    zulip_topic: str = "GPU bookings"
    secret_key: str = "dev-secret-change-me"
    db_path: str = "gpu_booking.db"
    users: list[User] = field(default_factory=list)

    def user(self, name: str | None) -> User | None:
        return next((u for u in self.users if u.name == name), None)


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get("GPU_BOOKING_CONFIG", "config.toml")
    data: dict = {}
    if Path(path).exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    elif path != "config.toml":
        raise FileNotFoundError(path)

    users = [User(name=u["name"], unix=u["unix"], admin=u.get("admin", False)) for u in data.pop("users", [])]
    known = Config.__dataclass_fields__.keys()
    cfg = Config(**{k: v for k, v in data.items() if k in known}, users=users)

    cfg.secret_key = os.environ.get("GPU_BOOKING_SECRET", cfg.secret_key)
    cfg.db_path = os.environ.get("GPU_BOOKING_DB", cfg.db_path)
    return cfg


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False

