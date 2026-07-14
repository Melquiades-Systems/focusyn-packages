"""Tests del merge idempotente de ~/.claude/settings.json (install/status/uninstall)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from focusyn_cli import hooks as h

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
