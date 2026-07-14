"""Registro del MCP focusyn en Claude Code — el `claude mcp add` lo corre el CLI, no la persona.

Envuelve el CLI de Claude Code (``claude mcp add|get|remove``), que es quien realmente gobierna
``~/.claude.json``: escribir ese archivo a mano sería pisar el formato de otro producto. Acá sólo se
construye el comando, se corre y se traduce el resultado.

La key del MCP es una **API key de máquina** (una por máquina, no la personal del humano): la emite
``POST /v1/agents`` con no-escalada de scopes. Este módulo no la persiste en ningún lado nuestro —
vive en la config de Claude Code, que es quien la manda en el header ``X-Agent-Key``.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
from dataclasses import dataclass

from focusyn_cli.http import CliError

# El nombre con el que el MCP aparece en Claude Code (`claude mcp list`).
DEFAULT_SERVER_NAME = "focusyn"
# Scopes del ciclo completo (leer + proponer + aplicar + sync). Se INTERSECAN con los del
# principal: nadie otorga lo que no tiene (el gateway lo rechaza; mejor no pedirlo de entrada).
DEFAULT_SCOPES: tuple[str, ...] = ("read", "propose", "apply", "sync")
# Dónde registra Claude Code el server: user = todos los proyectos; local/project = sólo el actual.
DEFAULT_CLAUDE_SCOPE = "user"

_NAME_RE = re.compile(r"[^a-z0-9._-]+")


@dataclass
class Registration:
    """El MCP tal como lo tiene registrado Claude Code hoy."""

    name: str
    url: str
    api_key: str | None
    raw: str

    def key_prefix(self) -> str:
        """Lo ÚNICO que se muestra de la key: entera se filtraría al scrollback y a los logs."""
        return self.api_key[:12] + "…" if self.api_key else "(sin header X-Agent-Key)"

    def scope_flag(self) -> str | None:
        """El scope que reporta `claude mcp get` (user/local/project), si se puede leer."""
        m = re.search(r"^\s*Scope:\s*(\w+)", self.raw, re.MULTILINE)
        return m.group(1).lower() if m else None


def mcp_url(gateway_url: str) -> str:
    """La URL del endpoint MCP del gateway. La barra final importa: sin ella hay redirect."""
    return gateway_url.rstrip("/") + "/mcp/"


def default_agent_name(host: str | None = None) -> str:
    """``mcp-<hostname>`` saneado al ``[a-z0-9._-]`` que exige el gateway (2–64 chars)."""
    raw = (host or socket.gethostname() or "local").lower()
    clean = _NAME_RE.sub("-", raw).strip("-.") or "local"
    return f"mcp-{clean}"[:64]


def claude_binary() -> str:
    """La ruta del CLI de Claude Code, o un ``CliError`` con el comando manual a mano."""
    found = shutil.which("claude")
    if not found:
        raise CliError(
            "No encuentro el CLI `claude` en el PATH: no puedo registrar el MCP por vos. "
            "Instalá Claude Code y volvé a correr `focusyn mcp install`."
        )
    return found


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True)  # noqa: S603


def get(name: str = DEFAULT_SERVER_NAME) -> Registration | None:
    """Lo que Claude Code tiene registrado con ese nombre, o ``None`` si no hay nada."""
    result = _run([claude_binary(), "mcp", "get", name])
    if result.returncode != 0:
        return None
    out = result.stdout
    url = re.search(r"^\s*URL:\s*(\S+)", out, re.MULTILINE)
    key = re.search(r"^\s*X-Agent-Key:\s*(\S+)", out, re.MULTILINE)
    return Registration(
        name=name,
        url=url.group(1) if url else "",
        api_key=key.group(1) if key else None,
        raw=out,
    )


def build_add_command(
    binary: str,
    name: str,
    url: str,
    api_key: str,
    *,
    claude_scope: str = DEFAULT_CLAUDE_SCOPE,
) -> list[str]:
    """El ``claude mcp add`` exacto que se va a correr."""
    return [
        binary,
        "mcp",
        "add",
        "--transport",
        "http",
        "--scope",
        claude_scope,
        name,
        url,
        "--header",
        f"X-Agent-Key: {api_key}",
    ]


def add(
    name: str,
    gateway_url: str,
    api_key: str,
    *,
    claude_scope: str = DEFAULT_CLAUDE_SCOPE,
    replace: bool = True,
) -> str:
    """Registra el MCP en Claude Code y devuelve la URL registrada. ``replace``: pisa el previo.

    ``claude mcp add`` falla si el nombre ya existe en ese scope → con ``replace`` se quita primero
    (así rotar la key es re-instalar, no editar a mano el header).
    """
    binary = claude_binary()
    url = mcp_url(gateway_url)
    if replace and get(name) is not None:
        remove(name, claude_scope=claude_scope)
    result = _run(build_add_command(binary, name, url, api_key, claude_scope=claude_scope))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:300]
        raise CliError(f"`claude mcp add` falló: {detail}")
    return url


def remove(name: str = DEFAULT_SERVER_NAME, *, claude_scope: str = DEFAULT_CLAUDE_SCOPE) -> bool:
    """Desregistra el MCP. ``True`` si había algo que quitar."""
    result = _run([claude_binary(), "mcp", "remove", name, "-s", claude_scope])
    return result.returncode == 0


__all__ = [
    "DEFAULT_SERVER_NAME",
    "DEFAULT_SCOPES",
    "DEFAULT_CLAUDE_SCOPE",
    "Registration",
    "mcp_url",
    "default_agent_name",
    "claude_binary",
    "get",
    "build_add_command",
    "add",
    "remove",
]
