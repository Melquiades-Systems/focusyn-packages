"""Resolución de credencial + construcción del cliente HTTP para los comandos.

Centraliza lo que todos los comandos necesitan: la credencial efectiva (con precedencia flag > env >
perfil) y un ``GatewayClient`` con **auto-refresh del JWT**: si el access expira (401) y el perfil
tiene refresh token, se renueva y se **persiste** el par rotado al config, sin re-login del usuario.
"""

from __future__ import annotations

from collections.abc import Callable

from focusyn_cli import auth
from focusyn_cli import config as cfg
from focusyn_cli.config import Credential
from focusyn_cli.http import CliError, GatewayClient


def credential_for(
    profile: str | None = None,
    gateway_url: str | None = None,
    api_key: str | None = None,
    *,
    need_secret: bool = False,
) -> Credential:
    """La credencial efectiva o un ``CliError`` accionable si falta gateway/secreto."""
    cred = cfg.resolve(profile=profile, gateway_url=gateway_url, api_key=api_key)
    if cred is None:
        raise CliError(
            "No hay gateway configurado. Corré `focusyn init` (o pasá --gateway-url / "
            "FOCUSYN_GATEWAY_URL).",
            code=2,
        )
    if need_secret and not cred.has_secret():
        raise CliError(
            "No hay credencial para este gateway. Corré `focusyn login` o `focusyn init` "
            "(o pasá --api-key / FOCUSYN_API_KEY).",
            code=2,
        )
    return cred


def _make_refresher(
    cred: Credential, profile: str | None
) -> Callable[[], str | None] | None:
    """Closure que renueva el Bearer con el refresh token y persiste el par rotado al config."""
    if cred.api_key or not cred.refresh_token:
        return None  # con API key no hay refresh; sin refresh token, tampoco

    def _refresh() -> str | None:
        try:
            tokens = auth.refresh(cred.gateway_url, cred.refresh_token or "")
        except CliError:
            return None
        # Persistir el par rotado en el perfil (si vino de uno). Sin perfil, sólo dura la sesión.
        conf = cfg.load_config()
        name = profile or conf.default_profile
        entry = conf.profiles.get(name)
        if entry is not None:
            entry.access_token = tokens.access_token
            entry.refresh_token = tokens.refresh_token
            cfg.save_config(conf)
        return tokens.access_token

    return _refresh


def client_for(
    profile: str | None = None,
    gateway_url: str | None = None,
    api_key: str | None = None,
    *,
    need_secret: bool = True,
) -> GatewayClient:
    """Un ``GatewayClient`` listo para el perfil, con auto-refresh del JWT si aplica."""
    cred = credential_for(profile, gateway_url, api_key, need_secret=need_secret)
    return GatewayClient(cred, refresher=_make_refresher(cred, profile))


__all__ = ["credential_for", "client_for"]
