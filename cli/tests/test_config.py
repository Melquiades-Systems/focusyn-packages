"""Tests de perfiles, permisos y precedencia del config del cliente."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from focusyn_cli import config as cfg
from focusyn_cli.config import Credential


def test_config_path_respeta_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "x" / "cfg.toml"
    monkeypatch.setenv("FOCUSYN_CONFIG", str(target))
    assert cfg.config_path() == target


def test_config_path_usa_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOCUSYN_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cfg.config_path() == tmp_path / "focusyn" / "config.toml"


def test_save_y_load_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "cfg.toml"))
    config = cfg.Config(
        default_profile="prod",
        profiles={"prod": Credential(gateway_url="https://gw", api_key="a2a_k", tenant="901")},
    )
    cfg.save_config(config)
    loaded = cfg.load_config()
    assert loaded.default_profile == "prod"
    assert loaded.profiles["prod"].gateway_url == "https://gw"
    assert loaded.profiles["prod"].api_key == "a2a_k"
    assert loaded.profiles["prod"].tenant == "901"


def test_save_escribe_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sub" / "cfg.toml"
    monkeypatch.setenv("FOCUSYN_CONFIG", str(path))
    cfg.save_config(
        cfg.Config(profiles={"default": Credential(gateway_url="https://gw", api_key="k")})
    )
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, oct(mode)


def test_load_inexistente_devuelve_vacio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "nope.toml"))
    config = cfg.load_config()
    assert config.profiles == {}


def test_resolve_sin_nada_devuelve_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "nope.toml"))
    assert cfg.resolve() is None


def test_resolve_precedencia_flag_sobre_env_sobre_perfil(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "cfg.toml"))
    cfg.save_config(
        cfg.Config(
            profiles={"default": Credential(gateway_url="https://perfil", api_key="perfil-key")}
        )
    )
    monkeypatch.setenv("FOCUSYN_GATEWAY_URL", "https://env")
    monkeypatch.setenv("FOCUSYN_API_KEY", "env-key")

    # flag gana a env y a perfil
    cred = cfg.resolve(gateway_url="https://flag", api_key="flag-key")
    assert cred is not None
    assert cred.gateway_url == "https://flag"
    assert cred.api_key == "flag-key"

    # sin flag: env gana al perfil
    cred = cfg.resolve()
    assert cred is not None
    assert cred.gateway_url == "https://env"
    assert cred.api_key == "env-key"


def test_resolve_ingest_key_por_compat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "cfg.toml"))
    monkeypatch.setenv("FOCUSYN_GATEWAY_URL", "https://gw")
    monkeypatch.setenv("FOCUSYN_INGEST_KEY", "ingest-key")  # compat con memory sync viejo
    cred = cfg.resolve()
    assert cred is not None
    assert cred.api_key == "ingest-key"


def test_auth_header_key_vs_bearer() -> None:
    assert Credential(gateway_url="x", api_key="k").auth_header() == {"X-Agent-Key": "k"}
    assert Credential(gateway_url="x", access_token="t").auth_header() == {
        "Authorization": "Bearer t"
    }
    assert Credential(gateway_url="x").auth_header() == {}


def test_key_prefix_no_expone_la_key_entera() -> None:
    cred = Credential(gateway_url="x", api_key="a2a_supersecretvalue1234567890")
    prefix = cred.key_prefix()
    assert prefix.startswith("a2a_")
    assert "supersecret" not in prefix
