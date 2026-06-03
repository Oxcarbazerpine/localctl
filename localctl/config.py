from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class HealthCheck:
    port: int | None = None
    # Port auto-negotiation: when `port` is taken, pick an alternate from `range`.
    auto: bool = False
    range: tuple[int, int] | None = None
    # How the chosen port is injected into the child process:
    #   env: name of env var to set (e.g. "PORT", "WEB_PORT")
    #   placeholder: literal token to substitute inside `cmd` argv (e.g. "${PORT}")
    env: str | None = None
    placeholder: str | None = None
    # HTTP probe: when set, status/wait will GET http://localhost:<port><http>
    # and treat 2xx/3xx as "ready". Stricter than TCP-connect probe.
    http: str | None = None


@dataclass
class AppSpec:
    name: str
    cwd: str
    cmd: list[str]
    env: dict[str, str] = field(default_factory=dict)
    health: HealthCheck = field(default_factory=HealthCheck)


# Recognized keys at each level — used for friendly "unknown key" errors.
_APP_KEYS = {"name", "cwd", "cmd", "env", "health"}
_HEALTH_KEYS = {"port", "auto", "range", "env", "placeholder", "http"}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ConfigError(ValueError):
    """Raised with a human-readable message anchored to a YAML path."""


def _err(path: str, msg: str) -> ConfigError:
    return ConfigError(f"{path}: {msg}")


def _parse_range(raw: object, where: str) -> tuple[int, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise _err(where, "must be a 2-element list [lo, hi]")
    try:
        lo, hi = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        raise _err(where, "lo and hi must be integers")
    if lo < 1 or hi > 65535:
        raise _err(where, "ports must be in [1, 65535]")
    if lo > hi:
        raise _err(where, f"lo ({lo}) > hi ({hi})")
    return (lo, hi)


def _validate_app(idx: int, item: object) -> AppSpec:
    base = f"apps[{idx}]"
    if not isinstance(item, dict):
        raise _err(base, "must be a mapping")

    # Required fields.
    for key in ("name", "cwd", "cmd"):
        if key not in item:
            raise _err(base, f"missing required field `{key}`")

    name = item["name"]
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _err(f"{base}.name", "must be a string of [A-Za-z0-9_.-], starting with alnum")
    path_anchor = f"apps[{idx}] ({name})"

    # Unknown top-level keys → typo guard.
    unknown = set(item.keys()) - _APP_KEYS
    if unknown:
        raise _err(path_anchor, f"unknown field(s): {sorted(unknown)}")

    cwd = item["cwd"]
    if not isinstance(cwd, str) or not cwd:
        raise _err(f"{path_anchor}.cwd", "must be a non-empty string")

    cmd = item["cmd"]
    if not isinstance(cmd, list) or not cmd:
        raise _err(f"{path_anchor}.cmd", "must be a non-empty list of strings")
    cmd_str: list[str] = []
    for j, arg in enumerate(cmd):
        if not isinstance(arg, (str, int, float)):
            raise _err(f"{path_anchor}.cmd[{j}]", f"unsupported type {type(arg).__name__}")
        cmd_str.append(str(arg))

    env_raw = item.get("env") or {}
    if not isinstance(env_raw, dict):
        raise _err(f"{path_anchor}.env", "must be a mapping")
    env: dict[str, str] = {}
    for k, v in env_raw.items():
        if not isinstance(k, str) or not k:
            raise _err(f"{path_anchor}.env", "keys must be non-empty strings")
        env[k] = str(v)

    health_raw = item.get("health") or {}
    if not isinstance(health_raw, dict):
        raise _err(f"{path_anchor}.health", "must be a mapping")
    unknown_h = set(health_raw.keys()) - _HEALTH_KEYS
    if unknown_h:
        raise _err(f"{path_anchor}.health", f"unknown field(s): {sorted(unknown_h)}")

    port = health_raw.get("port")
    if port is not None:
        if not isinstance(port, int) or not (1 <= port <= 65535):
            raise _err(f"{path_anchor}.health.port", "must be an int in [1, 65535]")
    auto = bool(health_raw.get("auto", False))
    if auto and port is None:
        raise _err(f"{path_anchor}.health", "auto=true requires `port` to be set")
    rng = _parse_range(health_raw.get("range"), f"{path_anchor}.health.range")
    if auto and rng is None and port is not None:
        rng = (int(port), int(port) + 50)

    env_name = health_raw.get("env")
    if env_name is not None and (not isinstance(env_name, str) or not env_name):
        raise _err(f"{path_anchor}.health.env", "must be a non-empty string")

    placeholder = health_raw.get("placeholder")
    if placeholder is not None and (not isinstance(placeholder, str) or not placeholder):
        raise _err(f"{path_anchor}.health.placeholder", "must be a non-empty string")
    if placeholder and not any(placeholder in a for a in cmd_str):
        raise _err(
            f"{path_anchor}.health.placeholder",
            f"token {placeholder!r} not found in any cmd argv",
        )

    http = health_raw.get("http")
    if http is not None:
        if not isinstance(http, str) or not http:
            raise _err(f"{path_anchor}.health.http", "must be a non-empty path like '/healthz'")
        if port is None:
            raise _err(f"{path_anchor}.health.http", "requires `port` to be set")

    return AppSpec(
        name=name,
        cwd=cwd,
        cmd=cmd_str,
        env=env,
        health=HealthCheck(
            port=port, auto=auto, range=rng,
            env=env_name, placeholder=placeholder, http=http,
        ),
    )


def load_config(path: Path) -> list[AppSpec]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: YAML parse error: {e}") from e
    if data is None:
        raise ConfigError(f"{path}: empty config")
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level must be a mapping with `apps:`")
    apps_raw = data.get("apps")
    if apps_raw is None:
        raise ConfigError(f"{path}: missing top-level `apps:` list")
    if not isinstance(apps_raw, list):
        raise ConfigError(f"{path}.apps: must be a list")

    apps = [_validate_app(i, item) for i, item in enumerate(apps_raw)]
    names = [a.name for a in apps]
    if len(names) != len(set(names)):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise ConfigError(f"{path}: duplicate app names: {dupes}")
    return apps


def find_app(apps: list[AppSpec], name: str) -> AppSpec:
    for app in apps:
        if app.name == name:
            return app
    available = ", ".join(a.name for a in apps) or "(none)"
    raise KeyError(f"app not found: {name!r} (available: {available})")
