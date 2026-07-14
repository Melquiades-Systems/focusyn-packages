"""Perfiles y credenciales del cliente en ``~/.config/focusyn/config.toml`` (0600).

Modela una CREDENCIAL ABSTRACTA ``{gateway_url, api_key | access_token+refresh, tenant?}``, no "una
API key": el día del cutover al IdP (MEL-DEC-193, que retira ``X-Agent-Key`` y
``agents.api_key_hash``) cambia este módulo + un ``focusyn login``, no el resto del CLI.

Precedencia de resolución (mayor gana): flag del comando > variable de entorno > perfil del config.
El archivo se escribe con permisos 0600 (dir 0700): lleva secretos. Leerlo es ``tomllib`` (stdlib
≥3.11); escribirlo, ``tomli_w`` (única razón de esa dep).
"""

from __future__ import annotations

import contextlib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

# URL pública del gateway — se OFRECE como default interactivo en `focusyn init`, NO se usa como
# fallback silencioso: un comando sin gateway configurado debe fallar claro, no adivinar.
DEFAULT_GATEWAY_URL = "https://focusyn.melquiades.systems"
_DEFAULT_PROFILE = "default"

# Overrides por entorno. FOCUSYN_API_KEY es el canónico; INGEST/APPLY se aceptan por compat con los
# comandos previos (memory sync usaba FOCUSYN_INGEST_KEY; scaffold/attachment, FOCUSYN_APPLY_KEY).
_ENV_URL = "FOCUSYN_GATEWAY_URL"
_ENV_KEYS = ("FOCUSYN_API_KEY", "FOCUSYN_INGEST_KEY", "FOCUSYN_APPLY_KEY")
_ENV_CONFIG = "FOCUSYN_CONFIG"


@dataclass
class Credential:
    """Una credencial efectiva contra un gateway: URL + (api_key **o** access/refresh token)."""

    gateway_url: str
    api_key: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    tenant: str | None = None

    def auth_header(self) -> dict[str, str]:
        """El header de auth para httpx. Prefiere la API key (larga vida, buena para el hook)."""
        if self.api_key:
            return {"X-Agent-Key": self.api_key}
        if self.access_token:
            return {"Authorization": f"Bearer {self.access_token}"}
        return {}

    def has_secret(self) -> bool:
        return bool(self.api_key or self.access_token)

    def key_prefix(self) -> str:
        """Un identificador seguro para mostrar en `doctor` (nunca la key entera)."""
        if self.api_key:
            return self.api_key[:12] + "…"
        if self.access_token:
            return "jwt:" + self.access_token[:8] + "…"
        return "(sin credencial)"


@dataclass
class Config:
    """El archivo de config completo: perfiles + cuál es el default."""

    default_profile: str = _DEFAULT_PROFILE
    profiles: dict[str, Credential] = field(default_factory=dict)


def config_path() -> Path:
    """Ruta del config: $FOCUSYN_CONFIG > $XDG_CONFIG_HOME/focusyn/ > ~/.config/focusyn/ (.toml)."""
    override = os.environ.get(_ENV_CONFIG)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "focusyn" / "config.toml"


def load_config() -> Config:
    """Lee el config del disco (o uno vacío si no existe). No aplica overrides de entorno."""
    path = config_path()
    if not path.is_file():
        return Config()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    profiles: dict[str, Credential] = {}
    for name, body in (raw.get("profiles") or {}).items():
        if not isinstance(body, dict):
            continue
        profiles[name] = Credential(
            gateway_url=str(body.get("gateway_url", "")),
            api_key=body.get("api_key"),
            access_token=body.get("access_token"),
            refresh_token=body.get("refresh_token"),
            tenant=body.get("tenant"),
        )
    return Config(
        default_profile=str(raw.get("default_profile", _DEFAULT_PROFILE)),
        profiles=profiles,
    )


def save_config(config: Config) -> Path:
    """Escribe el config con permisos 0600 (dir 0700). Devuelve la ruta escrita."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # p. ej. dir compartido: si no se puede endurecer, no bloquea el guardado.
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    body: dict[str, object] = {"default_profile": config.default_profile, "profiles": {}}
    for name, cred in config.profiles.items():
        entry: dict[str, str] = {"gateway_url": cred.gateway_url}
        for k in ("api_key", "access_token", "refresh_token", "tenant"):
            v = getattr(cred, k)
            if v:
                entry[k] = v
        body["profiles"][name] = entry  # type: ignore[index]
    # Escritura atómica + 0600: se crea con permisos restrictivos ANTES de escribir el secreto.
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(tomli_w.dumps(body))
    os.replace(tmp, path)
    return path


def resolve(
    *,
    profile: str | None = None,
    gateway_url: str | None = None,
    api_key: str | None = None,
) -> Credential | None:
    """La credencial EFECTIVA de un comando, con precedencia flag > entorno > perfil.

    Devuelve ``None`` si no hay gateway configurado por ningún lado (el comando debe entonces
    pedir ``focusyn init``). Una credencial sin secreto (sólo URL) es válida para los endpoints
    públicos (``/v1/capabilities``).
    """
    config = load_config()
    name = profile or config.default_profile
    base = config.profiles.get(name)

    env_url = os.environ.get(_ENV_URL)
    env_key = next((os.environ[k] for k in _ENV_KEYS if os.environ.get(k)), None)

    url = gateway_url or env_url or (base.gateway_url if base else None)
    if not url:
        return None
    key = api_key or env_key or (base.api_key if base else None)
    return Credential(
        gateway_url=url,
        api_key=key,
        access_token=base.access_token if base and not key else None,
        refresh_token=base.refresh_token if base else None,
        tenant=base.tenant if base else None,
    )


__all__ = [
    "DEFAULT_GATEWAY_URL",
    "Credential",
    "Config",
    "config_path",
    "load_config",
    "save_config",
    "resolve",
]
