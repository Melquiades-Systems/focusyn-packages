"""Tests de `focusyn mcp` (emitir la key de máquina + registrar el MCP) y de `focusyn help`.

El CLI de Claude Code se mockea en ``focusyn_cli.mcp._run``: los tests no deben tocar el
``~/.claude.json`` real (registrar un MCP es un efecto sobre otro producto). El gateway se mockea
con ``httpx.MockTransport``, igual que en test_commands.py.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from focusyn_cli import mcp as mcp_mod
from focusyn_cli.cli import app
from focusyn_cli.config import Credential
from focusyn_cli.http import CliError, GatewayClient

runner = CliRunner()

_GET_OUTPUT = """\
focusyn:
  Scope: User config (available in all your projects)
  Status: ✔ Connected
  Type: http
  URL: https://gw/mcp/
  Headers:
    X-Agent-Key: a2a_supersecretkey123
"""

_GET_OUTPUT_TPL = """\
{name}:
  Scope: User config (available in all your projects)
  Status: ✔ Connected
  Type: http
  URL: {url}
  Headers:
    X-Agent-Key: {key}
"""


@pytest.fixture()
def gateway(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """GatewayClient con MockTransport controlable (mismo patrón que test_commands)."""
    state: dict[str, Any] = {"handler": None, "requests": []}
    cred = Credential(gateway_url="https://gw", api_key="a2a_k")

    def _client_for(*_a: object, **_k: object) -> GatewayClient:
        def handler(request: httpx.Request) -> httpx.Response:
            state["requests"].append(request)
            resp: httpx.Response = state["handler"](request)
            return resp

        return GatewayClient(cred, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("focusyn_cli.session.client_for", _client_for)
    monkeypatch.setattr("focusyn_cli.session.credential_for", lambda *a, **k: cred)
    return state


class _ClaudeCalls(list[list[str]]):
    """Las invocaciones al CLI `claude`, más el estado del registro fake."""

    servers: dict[str, dict[str, str]]


@pytest.fixture()
def claude(monkeypatch: pytest.MonkeyPatch) -> _ClaudeCalls:
    """Fake STATEFUL del CLI `claude` (add/get/remove sobre un dict; nunca el binario real)."""
    calls = _ClaudeCalls()
    calls.servers = {}

    def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        verb = args[2]
        if verb == "add":
            name, url, header = args[7], args[8], args[10]
            calls.servers[name] = {"url": url, "key": header.split(": ", 1)[1]}
            return subprocess.CompletedProcess(args, 0, "added", "")
        if verb == "get":
            reg = calls.servers.get(args[3])
            if reg is None:
                return subprocess.CompletedProcess(args, 1, "", "no server")
            out = _GET_OUTPUT_TPL.format(name=args[3], url=reg["url"], key=reg["key"])
            return subprocess.CompletedProcess(args, 0, out, "")
        if verb == "remove":
            existed = calls.servers.pop(args[3], None) is not None
            return subprocess.CompletedProcess(args, 0 if existed else 1, "", "")
        return subprocess.CompletedProcess(args, 1, "", f"verbo desconocido: {verb}")

    monkeypatch.setattr(mcp_mod, "claude_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(mcp_mod, "_run", _run)
    return calls


def _gw(state: dict[str, Any], fn: Callable[[httpx.Request], httpx.Response]) -> None:
    state["handler"] = fn


def _routes(
    *,
    scopes: list[str],
    agents: list[dict[str, Any]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Gateway de mentira: whoami + list/create/rotate de agentes."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/auth/whoami":
            return httpx.Response(200, json={"principal": "user:alice", "scopes": scopes})
        if path == "/v1/agents" and request.method == "GET":
            return httpx.Response(200, json={"agents": agents or []})
        if path == "/v1/agents" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "mcp-host", "api_key": "a2a_nueva"})
        if path.endswith("/rotate"):
            return httpx.Response(200, json={"agent_id": "mcp-host", "api_key": "a2a_rotada"})
        return httpx.Response(404, json={"error": "no"})

    return handler


# --------------------------------------------------------------------------- módulo mcp


def test_default_agent_name_sanea_el_hostname() -> None:
    assert mcp_mod.default_agent_name("Mi PC Rara!") == "mcp-mi-pc-rara"


def test_mcp_url_agrega_la_barra_final() -> None:
    # Sin la barra el server responde un redirect y el cliente MCP no lo sigue.
    assert mcp_mod.mcp_url("https://gw") == "https://gw/mcp/"
    assert mcp_mod.mcp_url("https://gw/") == "https://gw/mcp/"


def test_get_parsea_url_y_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_mod, "claude_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(
        mcp_mod,
        "_run",
        lambda args, env=None: subprocess.CompletedProcess(args, 0, _GET_OUTPUT, ""),
    )
    reg = mcp_mod.get("focusyn")
    assert reg is not None
    assert reg.url == "https://gw/mcp/"
    assert reg.api_key == "a2a_supersecretkey123"
    assert reg.scope_flag() == "user"
    assert reg.key_prefix() == "a2a_supersec…"  # nunca la key entera


def test_add_reemplaza_el_registro_previo(claude: _ClaudeCalls) -> None:
    claude.servers["focusyn"] = {"url": "https://gw/mcp/", "key": "a2a_vieja"}

    url = mcp_mod.add("focusyn", "https://gw", "a2a_k")
    assert url == "https://gw/mcp/"
    verbs = [c[2] for c in claude]
    assert verbs == ["get", "remove", "add"]  # remove ANTES del add (add falla si el nombre existe)
    assert claude.servers["focusyn"]["key"] == "a2a_k"


@pytest.mark.parametrize(
    ("value", "esperado"),
    [
        ("${FOCUSYN_MCP_KEY}", True),
        ("  ${FOCUSYN_MCP_KEY}  ", True),
        ("${FOCUSYN_GATEWAY_URL:-x}", True),
        ("a2a_supersecretkey123", False),
        ("", False),
        (None, False),
    ],
)
def test_looks_like_unexpanded_placeholder(value: str | None, esperado: bool) -> None:
    assert mcp_mod.looks_like_unexpanded_placeholder(value) is esperado


def test_add_rechaza_una_key_placeholder(claude: _ClaudeCalls) -> None:
    """`claude mcp add` NO expande ${VAR}: registrar el literal dejaría un MCP con tools en 401."""
    with pytest.raises(CliError, match="placeholder sin expandir"):
        mcp_mod.add("focusyn", "https://gw", "${FOCUSYN_MCP_KEY}")
    assert not [c for c in claude if c[2] == "add"]  # no registró nada


def test_add_propaga_el_fallo_del_cli_de_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_mod, "claude_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(
        mcp_mod,
        "_run",
        lambda args, env=None: subprocess.CompletedProcess(args, 1, "", "boom")
        if args[2] == "add"
        else subprocess.CompletedProcess(args, 1, "", ""),
    )
    with pytest.raises(CliError, match="boom"):
        mcp_mod.add("focusyn", "https://gw", "a2a_k")


# --------------------------------------------------------------------------- higiene de la key
# El riesgo residual (V4 de la auditoría) está DOCUMENTADO en mcp.py: la key transita una vez por
# el argv del `claude mcp add` (sin alternativa en el CLI de Claude Code de hoy, verificado contra
# 2.1.207: guarda `${VAR}` literal, sin stdin/archivo para headers). Lo que sí es exigible por
# test: la key jamás en la SALIDA del propio CLI, y un camino sin historial para pegarla a mano.


def test_claude_binary_ausente_es_error_accionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("focusyn_cli.mcp.shutil.which", lambda _: None)
    with pytest.raises(CliError, match="Claude Code"):
        mcp_mod.claude_binary()


# --------------------------------------------------------------------------- mcp install


def test_install_emite_la_key_y_registra(
    gateway: dict[str, Any], claude: _ClaudeCalls
) -> None:
    _gw(gateway, _routes(scopes=["read", "propose", "apply", "sync"]))
    result = runner.invoke(app, ["mcp", "install", "--name", "mcp-host"])
    assert result.exit_code == 0, result.stdout
    assert "key emitida" in result.stdout
    add = next(c for c in claude if c[2] == "add")
    assert "https://gw/mcp/" in add
    assert claude.servers["focusyn"]["key"] == "a2a_nueva"
    # La key registrada JAMÁS aparece en la salida del CLI (sí transita el argv del
    # subproceso `claude mcp add`: riesgo residual documentado en mcp.py).
    assert "a2a_nueva" not in result.stdout


def test_install_recorta_los_scopes_que_no_tenes(
    gateway: dict[str, Any], claude: list[list[str]]
) -> None:
    # Pedir de más no es un aviso del gateway: es un 403. El CLI interseca y lo dice.
    _gw(gateway, _routes(scopes=["read", "propose"]))
    result = runner.invoke(app, ["mcp", "install", "--name", "mcp-host"])
    assert result.exit_code == 0, result.stdout
    assert "no tenés apply, sync" in result.stdout
    posted = next(r for r in gateway["requests"] if r.method == "POST")
    assert b'"scopes":["read","propose"]' in posted.content.replace(b" ", b"")


def test_install_sin_read_no_registra_nada(
    gateway: dict[str, Any], claude: list[list[str]]
) -> None:
    _gw(gateway, _routes(scopes=["ingest"]))
    result = runner.invoke(app, ["mcp", "install"])
    assert result.exit_code == 2
    assert "no tiene el scope 'read'" in result.stderr
    assert not [c for c in claude if c[2] == "add"]


def test_install_con_agente_existente_exige_rotate(
    gateway: dict[str, Any], claude: list[list[str]]
) -> None:
    _gw(
        gateway,
        _routes(scopes=["read"], agents=[{"agent_id": "mcp-host", "scopes": ["read"]}]),
    )
    result = runner.invoke(app, ["mcp", "install", "--name", "mcp-host"])
    assert result.exit_code == 2
    assert "--rotate" in result.stderr
    assert not [c for c in claude if c[2] == "add"]


def test_install_rotate_reusa_el_agente(gateway: dict[str, Any], claude: _ClaudeCalls) -> None:
    _gw(
        gateway,
        _routes(scopes=["read"], agents=[{"agent_id": "mcp-host", "scopes": ["read"]}]),
    )
    result = runner.invoke(app, ["mcp", "install", "--name", "mcp-host", "--rotate"])
    assert result.exit_code == 0, result.stdout
    assert "key rotada" in result.stdout
    assert claude.servers["focusyn"]["key"] == "a2a_rotada"


def test_install_use_key_no_emite_nada(gateway: dict[str, Any], claude: _ClaudeCalls) -> None:
    # El gateway sin Fase 3 desplegada (404 en /v1/agents) igual debe poder registrar el MCP.
    _gw(gateway, lambda r: httpx.Response(404, json={"error": "not found"}))
    result = runner.invoke(app, ["mcp", "install", "--use-key", "a2a_manual"])
    assert result.exit_code == 0, result.stdout
    assert claude.servers["focusyn"]["key"] == "a2a_manual"
    assert not gateway["requests"]  # ni una llamada: la key ya la trajo el usuario


def test_install_use_key_guion_la_pide_oculta(
    gateway: dict[str, Any], claude: _ClaudeCalls, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `--use-key -` = pegarla oculta: no queda en el historial del shell (inline sí).
    monkeypatch.setattr("focusyn_cli.cli.getpass.getpass", lambda _prompt="": " a2a_pegada ")
    result = runner.invoke(app, ["mcp", "install", "--use-key", "-"])
    assert result.exit_code == 0, result.stdout
    assert claude.servers["focusyn"]["key"] == "a2a_pegada"  # con .strip()
    assert "a2a_pegada" not in result.stdout
    assert not gateway["requests"]


def test_install_contra_gateway_sin_whoami_sugiere_use_key(
    gateway: dict[str, Any], claude: list[list[str]]
) -> None:
    _gw(gateway, lambda r: httpx.Response(404, json={"error": "not found"}))
    result = runner.invoke(app, ["mcp", "install"])
    assert result.exit_code == 2
    assert "--use-key" in result.stderr
    assert not [c for c in claude if c[2] == "add"]


def test_install_dry_run_no_toca_nada(gateway: dict[str, Any], claude: list[list[str]]) -> None:
    _gw(gateway, _routes(scopes=["read"]))
    result = runner.invoke(app, ["mcp", "install", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "https://gw/mcp/" in result.stdout
    assert not gateway["requests"]  # no emite la key…
    assert not claude  # …ni registra el server


# --------------------------------------------------------------------------- mcp status / uninstall


def test_status_valida_la_key_registrada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_mod,
        "get",
        lambda name=mcp_mod.DEFAULT_SERVER_NAME: mcp_mod.Registration(
            name=name,
            url="https://gw/mcp/",
            api_key="a2a_viva_larga_como_una_real",
            raw=_GET_OUTPUT,
        ),
    )
    handler = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"principal": "mcp-host", "scopes": ["read", "apply"]})
    )
    monkeypatch.setattr(
        "focusyn_cli.cli.GatewayClient",
        lambda cred, **kw: GatewayClient(cred, transport=handler),
    )
    result = runner.invoke(app, ["mcp", "status"])
    assert result.exit_code == 0, result.stdout
    assert "key válida" in result.stdout
    assert "a2a_viva_larga_como_una_real" not in result.stdout  # sólo el prefijo


def test_status_key_muerta_falla_y_sugiere_rotate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_mod,
        "get",
        lambda name=mcp_mod.DEFAULT_SERVER_NAME: mcp_mod.Registration(
            name=name, url="https://gw/mcp/", api_key="a2a_muerta", raw=_GET_OUTPUT
        ),
    )
    handler = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "invalid"}))
    monkeypatch.setattr(
        "focusyn_cli.cli.GatewayClient",
        lambda cred, **kw: GatewayClient(cred, transport=handler),
    )
    result = runner.invoke(app, ["mcp", "status"])
    assert result.exit_code == 1
    assert "--rotate" in result.stdout


def test_status_sin_registro(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_mod, "get", lambda name=mcp_mod.DEFAULT_SERVER_NAME: None)
    result = runner.invoke(app, ["mcp", "status"])
    assert result.exit_code == 0
    assert "no registrado" in result.stdout


def test_status_key_placeholder_avisa_y_no_valida(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key registrada = ${FOCUSYN_MCP_KEY} literal → avisa (no la valida: sería un 401 confuso)."""
    monkeypatch.setattr(
        mcp_mod,
        "get",
        lambda name=mcp_mod.DEFAULT_SERVER_NAME: mcp_mod.Registration(
            name=name, url="https://gw/mcp/", api_key="${FOCUSYN_MCP_KEY}", raw=_GET_OUTPUT
        ),
    )
    # Si intentara validar contra el gateway, este boom lo delataría: no debe llamarse.
    monkeypatch.setattr(
        "focusyn_cli.cli.GatewayClient",
        lambda *a, **k: pytest.fail("no debe validar un placeholder contra el gateway"),
    )
    result = runner.invoke(app, ["mcp", "status"])
    assert result.exit_code == 1
    assert "placeholder sin expandir" in result.stdout


def test_uninstall_desregistra(claude: _ClaudeCalls) -> None:
    claude.servers["focusyn"] = {"url": "https://gw/mcp/", "key": "a2a_k"}
    result = runner.invoke(app, ["mcp", "uninstall"])
    assert result.exit_code == 0, result.stdout
    assert claude[0][2] == "remove"
    assert "desregistrado" in result.stdout
    assert "focusyn" not in claude.servers


# --------------------------------------------------------------------------- help


def test_help_muestra_la_guia_por_tarea() -> None:
    result = runner.invoke(app, ["help"])
    assert result.exit_code == 0
    assert "EMPEZAR DESDE CERO" in result.stdout
    assert "focusyn mcp install" in result.stdout


def test_help_de_un_subcomando() -> None:
    result = runner.invoke(app, ["help", "mcp", "install"])
    assert result.exit_code == 0, result.stdout
    assert "--rotate" in result.stdout


def test_help_de_comando_inexistente_lista_los_que_hay() -> None:
    result = runner.invoke(app, ["help", "nope"])
    assert result.exit_code == 2
    assert "no es un comando" in result.stderr
    assert "doctor" in result.stderr
