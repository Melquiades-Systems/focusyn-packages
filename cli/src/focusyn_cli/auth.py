"""Login humano por JWT (``POST /auth/login`` + ``/auth/refresh``).

Un Bearer lleva los scopes del usuario Y su claim de tenant, y funciona en toda la superficie /v1 y
en /mcp. Es el modo que sobrevive al cutover al IdP (MEL-DEC-193). El access dura ~30 min; el
refresh ~14 días y rota en cada uso. La renovación transparente en 401 vive en ``session.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from focusyn_cli.http import CliError


@dataclass(frozen=True)
class Tokens:
    """El par de tokens que devuelve el login/refresh."""

    access_token: str
    refresh_token: str
    expires_in: int


def _post(url: str, path: str, body: dict[str, str]) -> Tokens:
    try:
        resp = httpx.post(url.rstrip("/") + path, json=body, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise CliError("Credenciales inválidas.") from exc
        raise CliError(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
    except httpx.HTTPError as exc:
        raise CliError(f"No se pudo conectar a {url}: {exc}") from exc
    data = resp.json()
    return Tokens(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_in=int(data.get("expires_in", 0)),
    )


def login(gateway_url: str, username: str, password: str) -> Tokens:
    """``POST /auth/login`` → par de tokens. Eleva ``CliError`` si las credenciales fallan."""
    return _post(gateway_url, "/auth/login", {"username": username, "password": password})


def refresh(gateway_url: str, refresh_token: str) -> Tokens:
    """``POST /auth/refresh`` → par rotado. Eleva ``CliError`` si el refresh no vale."""
    return _post(gateway_url, "/auth/refresh", {"refresh_token": refresh_token})


__all__ = ["Tokens", "login", "refresh"]
