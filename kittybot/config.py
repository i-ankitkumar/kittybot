"""Configuration.

Settings live in ``~/.config/kittybot/config.toml`` (mode 600) and can be
overridden by environment variables prefixed ``KITTYBOT_``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

CONFIG_DIR = Path.home() / ".config" / "kittybot"
CONFIG_FILE = CONFIG_DIR / "config.toml"


class KittyConfig(BaseModel):
    """Which kitty we manage and who we are in it."""

    #: the secret Kittysplit link, e.g. "https://www.kittysplit.com/k/AbCdEf-12"
    kitty_url: str = ""
    #: our name among the kitty's participants
    my_name: str = ""
    #: the home currency of the kitty
    currency: str = "INR"


class TelegramConfig(BaseModel):
    """Bot credentials and behaviour."""

    bot_token: str = ""
    #: chat ids allowed to use the bot (empty means anyone)
    allowed_chat_ids: list[int] = Field(default_factory=list)
    #: who pays for the orders we import - usually the kitty owner
    default_payer: str = ""
    #: how to propose a split for imported orders
    default_split_mode: str = "equal"


class DashboardConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080


class Config(BaseModel):
    kitty: KittyConfig = Field(default_factory=KittyConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    #: where the SQLite database lives
    db_path: str = ""
    #: which marketplace adapters to enable
    enabled_sources: list[str] = Field(default_factory=list)

    def save(self, path: Path | None = None) -> Path:
        target = path or CONFIG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_to_toml(self.model_dump()))
        target.chmod(0o600)
        return target


def _to_toml(data: dict) -> str:
    """Render the config as TOML. Small enough to hand-roll."""
    lines: list[str] = []
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            if isinstance(value, list):
                rendered = ", ".join(f'"{item}"' for item in value)
                lines.append(f"{key} = [{rendered}]")
            elif isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, (int, float)):
                lines.append(f"{key} = {value}")
            else:
                lines.append(f'{key} = "{value}"')
        lines.append("")
    return "\n".join(lines)


def load_config(path: Path | None = None) -> Config:
    """Load the config from disk, then apply environment overrides."""
    target = path or CONFIG_FILE
    data: dict = {}
    if target.exists():
        try:
            data = tomllib.loads(target.read_text())
        except Exception:
            data = {}

    config = Config(**data) if data else Config()

    # Environment overrides, useful for containers.
    env = os.environ
    if value := env.get("KITTYBOT_KITTY_URL"):
        config.kitty.kitty_url = value
    if value := env.get("KITTYBOT_MY_NAME"):
        config.kitty.my_name = value
    if value := env.get("KITTYBOT_BOT_TOKEN"):
        config.telegram.bot_token = value
    if value := env.get("KITTYBOT_DEFAULT_PAYER"):
        config.telegram.default_payer = value
    if value := env.get("KITTYBOT_DEFAULT_SPLIT_MODE"):
        config.telegram.default_split_mode = value
    if value := env.get("KITTYBOT_DB_PATH"):
        config.db_path = value
    if value := env.get("KITTYBOT_ALLOWED_CHAT_IDS"):
        config.telegram.allowed_chat_ids = [
            int(part) for part in value.replace(",", " ").split() if part.strip().isdigit()
        ]
    if value := env.get("KITTYBOT_SOURCES"):
        config.enabled_sources = [part.strip() for part in value.split(",") if part.strip()]
    if value := env.get("KITTYBOT_DASHBOARD_HOST"):
        config.dashboard.host = value
    if value := env.get("KITTYBOT_DASHBOARD_PORT"):
        config.dashboard.port = int(value)

    return config
