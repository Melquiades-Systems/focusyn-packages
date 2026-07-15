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
from urllib.parse import urlsplit

import tomli_w

from focusyn_cli.errors import CliError

# URL pública del gateway — se OFRECE como default interactivo en `focusyn init`, NO se usa como
# fallback silencioso: un comando sin gateway configurado debe fallar claro, no adivinar.
DEFAULT_GATEWAY_URL = "https://focusyn.melquiades.systems"
# http:// sólo se acepta hacia la propia máquina (gateway de desarrollo local). Hacia cualquier
# otro host, el header de auth (X-Agent-Key / Bearer) viajaría EN CLARO.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
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
    """El archivo de config completo: perfiles + cuál es el default + la key del hook de memorias.

    ``ingest_key`` es la API key (scope ``ingest``) que usa el hook de ``memory sync``. Vive FUERA
    de los perfiles a propósito: es una key de MÁQUINA para el hook, no tu identidad — y el flujo
    común ``focusyn login`` deja un JWT en el perfil, que ``memory sync`` no puede usar. Separarla
    hace que "login + hooks install" pueda dejar el sync andando sin pisar tu credencial personal.
    """

    default_profile: str = _DEFAULT_PROFILE
    profiles: dict[str, Credential] = field(default_factory=dict)
    ingest_key: str | None = None


def validate_gateway_url(url: str, *, source: str = "") -> str:
    """La URL del gateway si es segura; ``CliError`` si mandaría la credencial en claro.

    ``https`` siempre vale. ``http`` sólo hacia loopback (localhost/127.0.0.1/::1): el único uso
    legítimo es un gateway de desarrollo en la propia máquina. Cualquier otro esquema —o una URL
    sin esquema— se rechaza. Esto también protege el camino ``--invite``: la URL de un invite la
    provee un TERCERO, y sin este check un blob malicioso apuntaría el CLI a un gateway ``http://``
    del atacante que se queda con la credencial y todo lo que se lea/escriba.
    """
    origin = f" (de {source})" if source else ""
    parsed = urlsplit(url)
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http":
        if (parsed.hostname or "") in _LOOPBACK_HOSTS:
            return url
        raise CliError(
            f"URL de gateway insegura{origin}: {url} — con http:// la credencial viaja EN CLARO. "
            "Usá https:// (http sólo se acepta hacia localhost/127.0.0.1).",
            code=2,
        )
    raise CliError(
        f"URL de gateway inválida{origin}: '{url}' "
        f"(esquema {parsed.scheme or 'ausente'!r}). Se espera https://…",
        code=2,
    )


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
    ingest_key = raw.get("ingest_key")
    return Config(
        default_profile=str(raw.get("default_profile", _DEFAULT_PROFILE)),
        profiles=profiles,
        ingest_key=str(ingest_key) if ingest_key else None,
    )


def save_config(config: Config) -> Path:
    """Escribe el config con permisos 0600 (dir 0700). Devuelve la ruta escrita."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # p. ej. dir compartido: si no se puede endurecer, no bloquea el guardado.
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    body: dict[str, object] = {"default_profile": config.default_profile, "profiles": {}}
    if config.ingest_key:
        body["ingest_key"] = config.ingest_key
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
    ingest: bool = False,
) -> Credential | None:
    """La credencial EFECTIVA de un comando, con precedencia flag > entorno > perfil.

    Devuelve ``None`` si no hay gateway configurado por ningún lado (el comando debe entonces
    pedir ``focusyn init``). Una credencial sin secreto (sólo URL) es válida para los endpoints
    públicos (``/v1/capabilities``). Eleva ``CliError`` si la URL efectiva no es segura
    (:func:`validate_gateway_url`) — venga del flag, del entorno o de un config editado a mano.

    ``ingest=True`` es el modo del hook de ``memory sync``: la key se resuelve
    flag > entorno > :attr:`Config.ingest_key` > ``perfil.api_key``, y NUNCA cae al JWT del perfil
    (el gateway rechaza el Bearer para ``ingest``). Así el hook usa la key de máquina dedicada aun
    si el perfil default es un ``login`` (JWT), y ``memory sync`` no revienta en cada compactación.
    """
    config = load_config()
    name = profile or config.default_profile
    base = config.profiles.get(name)

    env_url = os.environ.get(_ENV_URL)
    env_key = next((os.environ[k] for k in _ENV_KEYS if os.environ.get(k)), None)

    url = gateway_url or env_url or (base.gateway_url if base else None)
    if not url:
        return None
    url = validate_gateway_url(url)
    if ingest:
        key = api_key or env_key or config.ingest_key or (base.api_key if base else None)
        # El hook no puede autenticar con Bearer → ni access ni refresh token en modo ingest.
        return Credential(gateway_url=url, api_key=key, tenant=base.tenant if base else None)
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
    "validate_gateway_url",
]
