"""Tests de login/refresh (JWT)."""

from __future__ import annotations

import httpx
import pytest

from focusyn_cli import auth
from focusyn_cli.http import CliError


def _fake_post(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> dict[str, object]:
    seen: dict[str, object] = {}

    def fake(url: str, json: dict[str, str], timeout: float) -> httpx.Response:
        seen["url"] = url
        seen["json"] = json
        return response

    monkeypatch.setattr("focusyn_cli.auth.httpx.post", fake)
    return seen


def test_login_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = httpx.Response(
        200,
        json={"access_token": "acc", "refresh_token": "ref", "expires_in": 1800},
        request=httpx.Request("POST", "http://gw/auth/login"),
    )
    seen = _fake_post(monkeypatch, resp)
    tokens = auth.login("http://gw", "alice", "pw")
    assert tokens.access_token == "acc"
    assert tokens.refresh_token == "ref"
    assert tokens.expires_in == 1800
    assert seen["url"] == "http://gw/auth/login"
    assert seen["json"] == {"username": "alice", "password": "pw"}


def test_login_credenciales_malas(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = httpx.Response(401, json={}, request=httpx.Request("POST", "http://gw/auth/login"))
    _fake_post(monkeypatch, resp)
    with pytest.raises(CliError, match="[Cc]redenciales"):
        auth.login("http://gw", "alice", "bad")


def test_refresh_rota(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = httpx.Response(
        200,
        json={"access_token": "acc2", "refresh_token": "ref2", "expires_in": 1800},
        request=httpx.Request("POST", "http://gw/auth/refresh"),
    )
    seen = _fake_post(monkeypatch, resp)
    tokens = auth.refresh("http://gw", "ref")
    assert tokens.access_token == "acc2"
    assert tokens.refresh_token == "ref2"
    assert seen["json"] == {"refresh_token": "ref"}


def test_login_error_de_red(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, json: dict[str, str], timeout: float) -> httpx.Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr("focusyn_cli.auth.httpx.post", boom)
    with pytest.raises(CliError, match="conectar"):
        auth.login("http://gw", "t", "p")
