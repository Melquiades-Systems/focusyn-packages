"""Tests de la capa HTTP compartida: mapeo de errores, check_url, aviso de versión."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from focusyn_cli import __version__
from focusyn_cli.config import Credential
from focusyn_cli.http import CliError, GatewayClient, check_url, version_warning


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> GatewayClient:
    return GatewayClient(
        Credential(gateway_url="https://gw", api_key="a2a_k"),
        transport=httpx.MockTransport(handler),
    )


def test_get_ok() -> None:
    with _client(lambda r: httpx.Response(200, json={"ok": True})) as c:
        assert c.get("/x") == {"ok": True}


def test_401_mensaje_accionable() -> None:
    with (
        _client(lambda r: httpx.Response(401, json={"error": {}})) as c,
        pytest.raises(CliError) as ei,
    ):
        c.get("/x")
    assert "401" in ei.value.message
    assert "init" in ei.value.message.lower() or "doctor" in ei.value.message.lower()


def test_403_habla_de_scope() -> None:
    with (
        _client(lambda r: httpx.Response(403, json={"error": {}})) as c,
        pytest.raises(CliError) as ei,
    ):
        c.get("/x")
    assert "403" in ei.value.message
    assert "scope" in ei.value.message.lower()


def test_error_de_conexion() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with _client(boom) as c, pytest.raises(CliError) as ei:
        c.get("/x")
    assert "No se pudo conectar" in ei.value.message


def test_capabilities_manda_auth_header_si_hay() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("X-Agent-Key")
        return httpx.Response(200, json={"api_version": "0.1.0", "vaults": []})

    with _client(handler) as c:
        c.capabilities()
    assert seen["key"] == "a2a_k"


def test_check_url_rechaza_no_gateway() -> None:
    # Una URL que responde 200 pero SIN api_version no es un gateway focusyn → CliError claro.
    cred = Credential(gateway_url="https://gw")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"hello": "world"}))
    with pytest.raises(CliError) as ei:
        check_url(cred, transport=transport)
    assert "gateway" in ei.value.message.lower()


def test_check_url_acepta_gateway_valido() -> None:
    cred = Credential(gateway_url="https://gw")
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"api_version": "0.1.0", "vaults": ["wiki"]})
    )
    caps = check_url(cred, transport=transport)
    assert caps["api_version"] == "0.1.0"


def test_version_warning_none_si_no_hay_minimo() -> None:
    assert version_warning({"api_version": "9.9.9"}) is None


def test_version_warning_avisa_si_cliente_viejo() -> None:
    warn = version_warning({"min_client_version": "999.0.0"})
    assert warn is not None
    assert "upgrade" in warn.lower()


def test_version_warning_ok_si_cliente_al_dia() -> None:
    # el propio __version__ nunca debería ser menor a sí mismo
    assert version_warning({"min_client_version": __version__}) is None


def test_put_y_delete() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.method] = request.url.path
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as c:
        assert c.put("/v1/x/role", json={"r": 1}) == {"ok": True}
        assert c.delete("/v1/x/1") == {"ok": True}
    assert seen["PUT"] == "/v1/x/role"
    assert seen["DELETE"] == "/v1/x/1"


def test_refresh_on_401_reintenta_una_vez() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"ok": True})

    cred = Credential(gateway_url="https://gw", access_token="viejo")
    client = GatewayClient(cred, transport=httpx.MockTransport(handler), refresher=lambda: "nuevo")
    with client:
        assert client.get("/x") == {"ok": True}
    assert calls["n"] == 2


def test_sin_refresher_el_401_no_reintenta() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={})

    with _client(handler) as c, pytest.raises(CliError):
        c.get("/x")
    assert calls["n"] == 1  # sin refresher, no hay 2º intento
