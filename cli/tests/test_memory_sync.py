"""Tests del cliente de sync de memorias ``focusyn memory sync``.

Cubre el descubrimiento de proyectos, la recolección de ``.md`` (ignorando ``.jsonl``),
el cliente HTTP con ``httpx.MockTransport`` (payload + parseo) y el comando del CLI vía
``CliRunner`` (dry-run sin red + flujo con cliente fake). El contrato del payload contra el
``IngestSyncIn`` REAL del gateway vive en ``tests/contract/`` (donde ``focusyn`` es importable).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from focusyn_cli.cli import app
from focusyn_cli.memory_sync import (
    MemoryProject,
    MemorySyncClient,
    ProjectSyncResult,
    collect_memory_docs,
    discover_projects,
)

runner = CliRunner()

_REAL_SLUG = "-home-melquiades-focusyn"  # los slugs reales empiezan con guion


def _make_project(root: Path, slug: str, files: dict[str, str]) -> MemoryProject:
    """Crea ``root/<slug>/memory/`` con ``files`` (rel→content) y lo devuelve."""
    memory_dir = root / slug / "memory"
    for rel, content in files.items():
        target = memory_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    memory_dir.mkdir(parents=True, exist_ok=True)
    return MemoryProject(slug=slug, memory_dir=memory_dir)


# --------------------------------------------------------------------------- #
# discover_projects
# --------------------------------------------------------------------------- #


def test_discover_projects_solo_los_que_tienen_memory(tmp_path: Path) -> None:
    _make_project(tmp_path, "proj-a", {"MEMORY.md": "a"})
    _make_project(tmp_path, "proj-b", {"MEMORY.md": "b"})
    # Un proyecto SIN memory/ (sólo transcripts) no debe aparecer.
    (tmp_path / "proj-c").mkdir()
    (tmp_path / "proj-c" / "session.jsonl").write_text("{}", encoding="utf-8")

    found = discover_projects(tmp_path)
    assert [p.slug for p in found] == ["proj-a", "proj-b"]  # orden estable


def test_discover_projects_raiz_inexistente(tmp_path: Path) -> None:
    assert discover_projects(tmp_path / "no-existe") == []


# --------------------------------------------------------------------------- #
# collect_memory_docs
# --------------------------------------------------------------------------- #


def test_collect_solo_md_ignora_jsonl(tmp_path: Path) -> None:
    project = _make_project(
        tmp_path,
        _REAL_SLUG,
        {
            "MEMORY.md": "index",
            "project-overview.md": "overview",
            "session.jsonl": '{"transcript": true}',  # se ignora
            "notes.txt": "nope",  # se ignora (no .md)
        },
    )
    docs = collect_memory_docs(project)
    rel_paths = [rp for rp, _ in docs]
    # Sólo los .md, con el slug por delante, ordenados por ruta.
    assert rel_paths == [
        f"{_REAL_SLUG}/MEMORY.md",
        f"{_REAL_SLUG}/project-overview.md",
    ]
    assert dict(docs)[f"{_REAL_SLUG}/MEMORY.md"] == "index"


def test_collect_recursivo_con_subdir(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "proj-a", {"MEMORY.md": "i", "sub/nested.md": "n"})
    rel_paths = [rp for rp, _ in collect_memory_docs(project)]
    assert rel_paths == ["proj-a/MEMORY.md", "proj-a/sub/nested.md"]


# --------------------------------------------------------------------------- #
# MemorySyncClient (httpx.MockTransport)
# --------------------------------------------------------------------------- #


def _client(
    handler: Callable[[httpx.Request], httpx.Response], *, key: str = "a2a_k"
) -> MemorySyncClient:
    return MemorySyncClient("http://gw:7415", key, transport=httpx.MockTransport(handler))


def test_sync_project_envia_payload_y_parsea(tmp_path: Path) -> None:
    project = _make_project(tmp_path, _REAL_SLUG, {"MEMORY.md": "body"})
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["key"] = request.headers.get("X-Agent-Key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "partition": "claude-memory",
                "created": 1,
                "updated": 0,
                "unchanged": 0,
                "deleted": 0,
                "errors": [],
            },
        )

    with _client(handler, key="a2a_secret") as client:
        result = client.sync_project(project)

    assert captured["path"] == "/v1/ingest/sync"
    assert captured["key"] == "a2a_secret"  # header de auth correcto
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["partition"] == "claude-memory"
    assert body["path_prefix"] == f"{_REAL_SLUG}/"
    assert body["prune"] is True
    assert body["docs"] == [{"rel_path": f"{_REAL_SLUG}/MEMORY.md", "content": "body"}]
    assert result == ProjectSyncResult(
        slug=_REAL_SLUG,
        path_prefix=f"{_REAL_SLUG}/",
        sent=1,
        created=1,
        updated=0,
        unchanged=0,
        deleted=0,
        errors=[],
    )


def test_sync_project_no_prune(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "proj-a", {"MEMORY.md": "x"})
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "partition": "claude-memory",
                "created": 0,
                "updated": 0,
                "unchanged": 1,
                "deleted": 0,
                "errors": [],
            },
        )

    with _client(handler) as client:
        client.sync_project(project, prune=False)
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["prune"] is False


def test_sync_project_eleva_en_error_http(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "proj-a", {"MEMORY.md": "x"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "AUTH_INSUFFICIENT_SCOPE"}})

    with _client(handler) as client:
        try:
            client.sync_project(project)
        except httpx.HTTPStatusError as exc:
            assert exc.response.status_code == 403
        else:  # pragma: no cover - debe elevar
            raise AssertionError("sync_project debió elevar HTTPStatusError")


# El contrato del payload contra el `IngestSyncIn` REAL del gateway vive en
# tests/contract/test_client_contracts.py (donde ambos paquetes son importables), NO acá: este
# paquete no importa `focusyn`.


# --------------------------------------------------------------------------- #
# CLI (CliRunner)
# --------------------------------------------------------------------------- #


def test_cli_dry_run_sin_red(tmp_path: Path) -> None:
    _make_project(tmp_path, "proj-a", {"MEMORY.md": "a", "extra.md": "b"})
    result = runner.invoke(
        app,
        ["memory", "sync", "--dry-run", "--projects-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.stdout
    assert "proj-a/  → 2 .md" in result.stdout


def test_cli_sin_api_key_falla(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Gateway configurado por env, pero SIN key → debe fallar pidiendo una.
    monkeypatch.setenv("FOCUSYN_GATEWAY_URL", "http://gw:7415")
    _make_project(tmp_path, "proj-a", {"MEMORY.md": "a"})
    result = runner.invoke(app, ["memory", "sync", "--projects-root", str(tmp_path)])
    assert result.exit_code != 0
    assert "api-key" in result.stdout.lower() or "api-key" in str(result.output).lower()


_CLI_CALLS: list[tuple[str, bool]] = []


class _FakeClient:
    """Cliente fake que registra las llamadas (sin red)."""

    def __init__(self, gateway_url: str, api_key: str, **kwargs: object) -> None:
        self.gateway_url = gateway_url
        self.api_key = api_key

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def sync_project(self, project: MemoryProject, *, prune: bool = True) -> ProjectSyncResult:
        _CLI_CALLS.append((project.slug, prune))
        return ProjectSyncResult(
            slug=project.slug,
            path_prefix=f"{project.slug}/",
            sent=1,
            created=1,
            updated=0,
            unchanged=0,
            deleted=0,
            errors=[],
        )


def test_cli_sync_invoca_cliente_por_proyecto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _CLI_CALLS.clear()
    _make_project(tmp_path, "proj-a", {"MEMORY.md": "a"})
    _make_project(tmp_path, "proj-b", {"MEMORY.md": "b"})
    monkeypatch.setattr("focusyn_cli.cli.MemorySyncClient", _FakeClient)

    result = runner.invoke(
        app,
        [
            "memory",
            "sync",
            "--gateway-url",
            "http://gw:7415",
            "--api-key",
            "a2a_test",
            "--projects-root",
            str(tmp_path),
            "--project",
            "proj-a",
        ],
    )
    assert result.exit_code == 0, result.stdout
    # Sólo proj-a (filtrado por --project); cliente invocado una vez.
    assert _CLI_CALLS == [("proj-a", True)]
    assert "Total: created=1" in result.stdout
