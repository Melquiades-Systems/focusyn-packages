"""El CLI exige TLS hacia el gateway (V1 de la auditoría de publicación).

Con ``http://`` el header de auth (``X-Agent-Key``/``Bearer``) viaja EN CLARO. Y la URL de un
``--invite`` la provee un TERCERO: sin validación, un blob malicioso apunta el CLI de la víctima a
un gateway del atacante. Regla: https siempre; http sólo hacia loopback (gateway de desarrollo).

Se cubren las tres puertas de entrada (``init``, ``login``, ``--invite``) y las capas de defensa
(``resolve``, ``GatewayClient``, ``mcp.add``).
"""

from __future__ import annotations

import base64
import json
import subprocess

import pytest
from typer.testing import CliRunner

from focusyn_cli import config as cfg
from focusyn_cli import mcp as mcp_mod
from focusyn_cli.cli import app
from focusyn_cli.config import Credential
from focusyn_cli.errors import CliError
from focusyn_cli.http import GatewayClient

runner = CliRunner()


def _invite(url: str, key: str = "a2a_k") -> str:
    blob = base64.urlsafe_b64encode(json.dumps({"url": url, "key": key}).encode()).decode()
    return f"focusyn-invite:{blob}"


# --------------------------------------------------------------------------- validate_gateway_url


def test_acepta_https() -> None:
    assert cfg.validate_gateway_url("https://gw.example") == "https://gw.example"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:7415",
        "http://127.0.0.1:7415",
        "http://[::1]:7415",
        "http://localhost",
    ],
)
def test_acepta_http_solo_loopback(url: str) -> None:
    # El único http legítimo: un gateway de desarrollo en la propia máquina.
    assert cfg.validate_gateway_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://gw.example",
        "http://192.168.0.99:7415",  # LAN tampoco: el tráfico igual cruza la red en claro
        "http://localhost.evil.example",  # el 'localhost' tiene que ser el HOST, no un prefijo
    ],
)
def test_rechaza_http_no_loopback(url: str) -> None:
    with pytest.raises(CliError, match="EN CLARO"):
        cfg.validate_gateway_url(url)


@pytest.mark.parametrize("url", ["gw.example", "ftp://gw.example", ""])
def test_rechaza_esquema_ausente_o_raro(url: str) -> None:
    with pytest.raises(CliError, match="inválida"):
        cfg.validate_gateway_url(url)


# --------------------------------------------------------------------------- capas de defensa


def test_resolve_rechaza_env_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOCUSYN_GATEWAY_URL", "http://evil.example")
    monkeypatch.setenv("FOCUSYN_API_KEY", "a2a_k")
    with pytest.raises(CliError, match="EN CLARO"):
        cfg.resolve()


def test_resolve_acepta_http_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOCUSYN_GATEWAY_URL", "http://localhost:7415")
    cred = cfg.resolve()
    assert cred is not None
    assert cred.gateway_url == "http://localhost:7415"


def test_gateway_client_rechaza_http() -> None:
    # Defensa en profundidad: aunque la URL esquive resolve(), el cliente no la usa.
    with pytest.raises(CliError, match="EN CLARO"):
        GatewayClient(Credential(gateway_url="http://evil.example", api_key="a2a_k"))


def test_mcp_add_no_registra_http(monkeypatch: pytest.MonkeyPatch) -> None:
    # Un endpoint http registrado en Claude Code mandaría la key en claro en CADA sesión.
    calls: list[list[str]] = []

    def _run(
        args: list[str], env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(mcp_mod, "claude_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(mcp_mod, "_run", _run)
    with pytest.raises(CliError, match="EN CLARO"):
        mcp_mod.add("focusyn", "http://evil.example", "a2a_k")
    assert not calls  # ni un `claude mcp …` llegó a correr


# --------------------------------------------------------------------------- las tres puertas


def test_init_con_invite_http_rechaza_y_no_guarda() -> None:
    result = runner.invoke(app, ["init", "--invite", _invite("http://evil.example")])
    assert result.exit_code == 2, result.output
    assert "EN CLARO" in result.output
    assert "--invite" in result.output  # el mensaje dice de dónde vino la URL
    assert not cfg.config_path().exists()  # la key del blob NO se persistió


def test_init_con_gateway_url_http_rechaza() -> None:
    result = runner.invoke(
        app, ["init", "--gateway-url", "http://evil.example", "--api-key", "a2a_k"]
    )
    assert result.exit_code == 2, result.output
    assert "EN CLARO" in result.output
    assert not cfg.config_path().exists()


def test_login_con_gateway_url_http_rechaza() -> None:
    # Falla ANTES de pedir usuario/contraseña (check_url → GatewayClient valida).
    result = runner.invoke(app, ["login", "--gateway-url", "http://evil.example"])
    assert result.exit_code == 2, result.output
    assert "EN CLARO" in result.output


def test_init_invite_https_sigue_funcionando(monkeypatch: pytest.MonkeyPatch) -> None:
    # El camino feliz no se rompe: un invite https pasa la validación y llega a check_url.
    seen: dict[str, str] = {}

    def _check(cred: Credential, **_kw: object) -> dict[str, object]:
        seen["url"] = cred.gateway_url
        return {"api_version": "1"}

    monkeypatch.setattr("focusyn_cli.cli.check_url", _check)
    result = runner.invoke(app, ["init", "--invite", _invite("https://gw.example")])
    assert result.exit_code == 0, result.output
    assert seen["url"] == "https://gw.example"
    assert cfg.load_config().profiles["default"].api_key == "a2a_k"
