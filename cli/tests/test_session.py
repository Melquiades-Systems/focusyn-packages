"""Tests de la resolución de credencial y el auto-refresh del JWT persistido."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from focusyn_cli import auth, session
from focusyn_cli import config as cfg
from focusyn_cli.config import Credential
from focusyn_cli.http import CliError


def test_credential_for_sin_gateway_falla(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "none.toml"))
    with pytest.raises(CliError, match="gateway"):
        session.credential_for()


def test_credential_for_sin_secreto_falla(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "cfg.toml"))
    cfg.save_config(cfg.Config(profiles={"default": Credential(gateway_url="https://gw")}))
    with pytest.raises(CliError, match="credencial"):
        session.credential_for(need_secret=True)


def test_client_for_api_key_no_refresca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "cfg.toml"))
    cfg.save_config(
        cfg.Config(profiles={"default": Credential(gateway_url="https://gw", api_key="a2a_k")})
    )
    client = session.client_for(need_secret=True)
    assert client._refresher is None  # con API key no hay refresh


def test_auto_refresh_persiste_el_par_rotado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "cfg.toml"))
    cfg.save_config(
        cfg.Config(
            profiles={
                "default": Credential(
                    gateway_url="https://gw", access_token="viejo", refresh_token="ref-viejo"
                )
            }
        )
    )

    # El refresh devuelve un par nuevo; NO llamamos al gateway real.
    monkeypatch.setattr(
        auth, "refresh", lambda url, rt: auth.Tokens("acc-nuevo", "ref-nuevo", 1800)
    )

    # El transporte responde 401 la 1ª vez y 200 la 2ª (tras el refresh).
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"error": {}})
        assert request.headers.get("Authorization") == "Bearer acc-nuevo"
        return httpx.Response(200, json={"ok": True})

    cred = cfg.resolve()
    assert cred is not None
    from focusyn_cli.http import GatewayClient

    refresher = session._make_refresher(cred, None)
    with GatewayClient(cred, transport=httpx.MockTransport(handler), refresher=refresher) as client:
        out = client.get("/x")
    assert out == {"ok": True}
    assert calls["n"] == 2  # reintentó tras refrescar

    # Y el par rotado quedó PERSISTIDO en el config.
    reloaded = cfg.load_config().profiles["default"]
    assert reloaded.access_token == "acc-nuevo"
    assert reloaded.refresh_token == "ref-nuevo"
