"""Tests de los comandos HTTP (read/search/list/vault/usage/write) con el cliente mockeado.

Los comandos construyen su cliente vía ``session.client_for``; lo parcheamos para inyectar un
``httpx.MockTransport`` — así probamos el wiring del comando (params, parseo, formato, manejo de
error) sin tocar la red ni el gateway.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from focusyn_cli.cli import app
from focusyn_cli.config import Credential
from focusyn_cli.http import GatewayClient

runner = CliRunner()


@pytest.fixture()
def gateway(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Parchea session.client_for para devolver un GatewayClient con MockTransport controlable."""
    state: dict[str, Any] = {"handler": None, "requests": []}

    cred = Credential(gateway_url="https://gw", api_key="a2a_k")

    def _client_for(*_a: object, **_k: object) -> GatewayClient:
        def handler(request: httpx.Request) -> httpx.Response:
            state["requests"].append(request)
            resp: httpx.Response = state["handler"](request)
            return resp

        return GatewayClient(cred, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("focusyn_cli.session.client_for", _client_for)
    # `agent create` también resuelve la credencial (para el gateway_url del blob de invite).
    monkeypatch.setattr("focusyn_cli.session.credential_for", lambda *a, **k: cred)
    return state


def _set(gateway: dict[str, Any], fn: Callable[[httpx.Request], httpx.Response]) -> None:
    gateway["handler"] = fn


def test_search_formatea_hits(gateway: dict[str, object]) -> None:
    _set(
        gateway,
        lambda r: httpx.Response(
            200,
            json={"results": [{"score": 0.9, "title": "Un doc"}, {"score": 0.5, "title": "Otro"}]},
        ),
    )
    result = runner.invoke(app, ["search", "graphrag", "--vault", "wiki"])
    assert result.exit_code == 0, result.stdout
    assert "Un doc" in result.stdout
    assert "0.900" in result.stdout


def test_list_muestra_items_y_total(gateway: dict[str, object]) -> None:
    _set(
        gateway,
        lambda r: httpx.Response(
            200,
            json={
                "items": [{"doc_id": "WIKI-X-001", "kind": "concept", "path": "a.md"}],
                "total": 42,
            },
        ),
    )
    result = runner.invoke(app, ["list", "--vault", "wiki"])
    assert result.exit_code == 0, result.stdout
    assert "WIKI-X-001" in result.stdout
    assert "de 42" in result.stdout


def test_read_imprime_raw_content(gateway: dict[str, object]) -> None:
    _set(gateway, lambda r: httpx.Response(200, json={"raw_content": "# Hola\ncuerpo"}))
    result = runner.invoke(app, ["read", "WIKI-X-001"])
    assert result.exit_code == 0, result.stdout
    assert "# Hola" in result.stdout


def test_read_por_doc_id_vs_path(gateway: dict[str, object]) -> None:
    _set(gateway, lambda r: httpx.Response(200, json={"raw_content": "x"}))
    runner.invoke(app, ["read", "MEL-DEC-201"])
    req = gateway["requests"][-1]  # type: ignore[index]
    assert "doc_id=MEL-DEC-201" in str(req.url)
    runner.invoke(app, ["read", "melquiades/proyectos/x.md"])
    req = gateway["requests"][-1]  # type: ignore[index]
    assert "path=" in str(req.url)


def test_ask_imprime_answer(gateway: dict[str, object]) -> None:
    _set(gateway, lambda r: httpx.Response(200, json={"answer": "La respuesta", "sources": []}))
    result = runner.invoke(app, ["ask", "¿qué es X?"])
    assert result.exit_code == 0, result.stdout
    assert "La respuesta" in result.stdout


def test_vault_list_formatea(gateway: dict[str, object]) -> None:
    _set(
        gateway,
        lambda r: httpx.Response(
            200, json={"vaults": [{"name": "wiki", "vault_type": "wiki", "status": "ready"}]}
        ),
    )
    result = runner.invoke(app, ["vault", "list"])
    assert result.exit_code == 0, result.stdout
    assert "wiki" in result.stdout


def test_403_da_error_limpio(gateway: dict[str, object]) -> None:
    _set(gateway, lambda r: httpx.Response(403, json={"error": {}}))
    result = runner.invoke(app, ["usage", "summary"])
    assert result.exit_code != 0
    assert "403" in result.output and "scope" in result.output.lower()


def test_propose_toma_content_de_stdin(gateway: dict[str, object]) -> None:
    _set(gateway, lambda r: httpx.Response(200, json={"proposal_id": "prop-123"}))
    result = runner.invoke(
        app, ["propose", "--intent", "crear X"], input="---\nid: X\n---\ncuerpo\n"
    )
    assert result.exit_code == 0, result.stdout
    assert "prop-123" in result.stdout
    # el body llevó el content de stdin + un request_id generado
    import json as _json

    sent = _json.loads(gateway["requests"][-1].content)  # type: ignore[index]
    assert sent["intent"] == "crear X"
    assert "cuerpo" in sent["content"]
    assert sent["request_id"]


def test_delete_manda_reason_e_idempotency(gateway: dict[str, object]) -> None:
    _set(gateway, lambda r: httpx.Response(200, json={"status": "deleted"}))
    result = runner.invoke(app, ["delete", "WIKI-X-001", "--reason", "obsoleto"])
    assert result.exit_code == 0, result.stdout
    import json as _json

    sent = _json.loads(gateway["requests"][-1].content)  # type: ignore[index]
    assert sent["doc_id"] == "WIKI-X-001"
    assert sent["reason"] == "obsoleto"
    assert sent["idempotency_key"]


def test_agent_create_muestra_la_key(gateway: dict[str, object]) -> None:
    _set(
        gateway,
        lambda r: httpx.Response(
            201,
            json={
                "agent_id": "mi-laptop",
                "api_key": "a2a_NUEVA",
                "scopes": ["ingest"],
                "owner": "user:alice",
            },
        ),
    )
    result = runner.invoke(app, ["agent", "create", "mi-laptop", "--scopes", "ingest"])
    assert result.exit_code == 0, result.output
    assert "a2a_NUEVA" in result.output
    import json as _json

    sent = _json.loads(gateway["requests"][-1].content)  # type: ignore[index]
    assert sent["name"] == "mi-laptop"
    assert sent["scopes"] == ["ingest"]


def test_agent_create_invite_emite_blob(gateway: dict[str, object]) -> None:
    import base64
    import json as _json

    _set(
        gateway,
        lambda r: httpx.Response(
            201,
            json={"agent_id": "l", "api_key": "a2a_K", "scopes": ["ingest"], "owner": "user:alice"},
        ),
    )
    result = runner.invoke(app, ["agent", "create", "l", "--scopes", "ingest", "--invite"])
    assert result.exit_code == 0, result.output
    line = next(x for x in result.output.splitlines() if x.startswith("focusyn-invite:"))
    decoded = _json.loads(base64.urlsafe_b64decode(line[len("focusyn-invite:") :].encode()))
    assert decoded["key"] == "a2a_K"
    assert decoded["url"] == "https://gw"


def test_agent_list_no_admin(gateway: dict[str, object]) -> None:
    _set(
        gateway,
        lambda r: httpx.Response(
            200,
            json={
                "agents": [
                    {
                        "agent_id": "mi-laptop",
                        "scopes": ["ingest"],
                        "key_prefix": "a2a_x",
                        "active": True,
                    }
                ]
            },
        ),
    )
    result = runner.invoke(app, ["agent", "list"])
    assert result.exit_code == 0, result.output
    assert "mi-laptop" in result.output


def test_agent_rotate(gateway: dict[str, object]) -> None:
    _set(gateway, lambda r: httpx.Response(200, json={"agent_id": "l", "api_key": "a2a_ROT"}))
    result = runner.invoke(app, ["agent", "rotate", "l"])
    assert result.exit_code == 0, result.output
    assert "a2a_ROT" in result.output
    assert gateway["requests"][-1].url.path == "/v1/agents/l/rotate"  # type: ignore[index]
