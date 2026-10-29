"""Configuration and credential loading.

Secrets come from environment variables or an ignored mode-0600 env file. They
are never committed or written to the journal or console.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

DEFAULT_BASE_URL = "https://foservices.icegate.gov.in"
DEFAULT_ESANCHIT_BASE = "https://www.icegate.gov.in"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

ENV_TEMPLATE = """\
# ICEGATE credentials for icegate-client.
# Keep this file mode 0600 and never commit it.
ICEGATE_USERNAME=
ICEGATE_PASSWORD=
"""


def _xdg(name: str, fallback: str) -> Path:
    base = os.environ.get(name)
    if base:
        return Path(base).expanduser()
    return Path(fallback).expanduser()


def default_state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", "~/.local/state") / "icegate-client"


def default_env_file() -> Path:
    root = Path(__file__).resolve().parents[3]
    return (root if (root / "pyproject.toml").is_file() else Path.cwd()) / ".env"


def parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        data[key.strip()] = value
    return data


def check_env_file_permissions(path: Path) -> None:
    # Windows access is controlled by ACLs, not POSIX mode bits.
    if os.name == "nt":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"{path} is readable by group or others (mode {mode:04o}). "
            f"Run: chmod 600 {path}"
        )


def mask_identifier(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return value[0] + "*" * (len(value) - 1)
    return f"{value[:3]}{'*' * max(3, len(value) - 5)}{value[-2:]}"


@dataclass(frozen=True)
class Settings:
    icegate_id: str
    password: str = field(repr=False)
    state_dir: Path = field(default_factory=default_state_dir)
    env_file: Path | None = None
    timeout: float = 45.0
    base_url: str = DEFAULT_BASE_URL
    esanchit_base: str = DEFAULT_ESANCHIT_BASE
    user_agent: str = DEFAULT_USER_AGENT

    def __post_init__(self) -> None:
        if not self.icegate_id or not self.password:
            raise ConfigError("ICEGATE_USERNAME and ICEGATE_PASSWORD are required")

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    def journal_path(self) -> Path:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.runs_dir / f"{stamp}-{os.getpid()}.jsonl"

    @classmethod
    def load(
        cls,
        *,
        env_file: Path | str | None = None,
        env: dict[str, str] | None = None,
        state_dir: Path | str | None = None,
        allow_insecure_env_file: bool = False,
    ) -> Settings:
        environ = dict(os.environ if env is None else env)

        path: Path | None
        if env_file is not None:
            path = Path(env_file).expanduser()
            if not path.is_file():
                raise ConfigError(f"env file not found: {path}")
        else:
            candidate = default_env_file()
            path = candidate if candidate.is_file() else None

        file_values: dict[str, str] = {}
        if path is not None:
            if not allow_insecure_env_file:
                check_env_file_permissions(path)
            file_values = parse_env_file(path)

        def pick(key: str, fallback: str | None = None) -> str | None:
            return environ.get(key) or file_values.get(key) or fallback

        icegate_id = pick("ICEGATE_USERNAME")
        password = pick("ICEGATE_PASSWORD")
        if not icegate_id or not password:
            raise ConfigError(
                "ICEGATE_USERNAME and ICEGATE_PASSWORD are not set. "
                f"Create {default_env_file()} with mode 0600, or export both variables."
            )

        chosen_state = state_dir or pick("ICEGATE_STATE_DIR")
        timeout_raw = pick("ICEGATE_TIMEOUT", "45")

        try:
            timeout = float(timeout_raw or 45)
        except ValueError as exc:
            raise ConfigError("ICEGATE_TIMEOUT must be a number") from exc

        return cls(
            icegate_id=icegate_id.strip(),
            password=password,
            state_dir=Path(chosen_state).expanduser() if chosen_state else default_state_dir(),
            env_file=path,
            timeout=timeout,
        )

    def describe(self) -> str:
        return (
            f"icegate_id={mask_identifier(self.icegate_id)} "
            f"env_file={self.env_file or '<none>'} "
            f"state_dir={self.state_dir}"
        )


def write_env_template(path: Path | None = None, *, force: bool = False) -> Path:
    target = Path(path).expanduser() if path else default_env_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        raise ConfigError(f"{target} already exists")
    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(ENV_TEMPLATE)
    os.chmod(target, 0o600)
    return target
