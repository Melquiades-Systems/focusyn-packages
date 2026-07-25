"""Tests del sync de memorias ``focusyn memory {sync,status,resolve,forget}``.

Cubre el descubrimiento de proyectos, el hash de contenido, la **clasificación a tres bandas**
(el corazón: local × remoto × base → subir/bajar/conflicto), el journal, el cliente HTTP con
``httpx.MockTransport`` y los comandos vía ``CliRunner``.

El contrato contra los schemas REALES del gateway (``IngestSyncIn``, ``body_hash_of``) vive en
``tests/contract/test_client_contracts.py`` del repo del gateway, donde ambos paquetes son
importables; este paquete no importa ``focusyn``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from focusyn_cli import config as cfg
from focusyn_cli.cli import app
from focusyn_cli.config import Credential
from focusyn_cli.memory_sync import (
    Action,
    MemoryProject,
    MemorySyncClient,
    body_hash_of,
    classify,
    collect_memory_docs,
    discover_projects,
    load_journal,
    local_state,
    save_journal,
    write_memory_doc,
)

runner = CliRunner()

_REAL_SLUG = "-home-usuario-miproyecto"  # los slugs reales empiezan con guion


def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Config + journal aislados, sin FOCUSYN_* del shell contaminando la resolución."""
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("FOCUSYN_MEMORY_JOURNAL", str(tmp_path / "journal.json"))
    for k in ("FOCUSYN_INGEST_KEY", "FOCUSYN_API_KEY", "FOCUSYN_APPLY_KEY", "FOCUSYN_GATEWAY_URL"):
        monkeypatch.delenv(k, raising=False)


def _make_project(root: Path, slug: str, files: dict[str, str]) -> MemoryProject:
    """Crea ``root/<slug>/memory/`` con ``files`` (rel→content) y lo devuelve."""
    memory_dir = root / slug / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = memory_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
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


def test_discover_projects_ignora_memory_vacio(tmp_path: Path) -> None:
    """Un memory/ vacío NO participa: bajo el modelo viejo significaba 'borrá todo'.

    Es el caso exacto de una máquina recién migrada: el directorio existe (lo crea Claude
    Code) pero los .md no se copiaron. Antes eso podaba el proyecto entero del corpus.
    """
    _make_project(tmp_path, "proj-a", {"MEMORY.md": "a"})
    (tmp_path / "proj-vacio" / "memory").mkdir(parents=True)

    assert [p.slug for p in discover_projects(tmp_path)] == ["proj-a"]


def test_discover_projects_raiz_inexistente(tmp_path: Path) -> None:
    assert discover_projects(tmp_path / "no-existe") == []


# --------------------------------------------------------------------------- #
# collect_memory_docs / local_state / body_hash_of
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
    rel_paths = [rp for rp, _ in collect_memory_docs(project)]
    assert rel_paths == [f"{_REAL_SLUG}/MEMORY.md", f"{_REAL_SLUG}/project-overview.md"]


def test_collect_recursivo_con_subdir(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "proj-a", {"MEMORY.md": "i", "sub/nested.md": "n"})
    assert [rp for rp, _ in collect_memory_docs(project)] == [
        "proj-a/MEMORY.md",
        "proj-a/sub/nested.md",
    ]


def test_collect_ignora_symlinks(tmp_path: Path) -> None:
    """Un symlink dentro de memory/ NO se sube: sólo se sincroniza lo que vive ahí."""
    secreto = tmp_path / "fuera" / "privado.md"
    secreto.parent.mkdir(parents=True)
    secreto.write_text("clave-que-no-debe-subir", encoding="utf-8")

    project = _make_project(tmp_path, "-p", {"real.md": "contenido legítimo"})
    (project.memory_dir / "robado.md").symlink_to(secreto)

    docs = collect_memory_docs(project)
    assert [rel for rel, _ in docs] == ["-p/real.md"]
    assert all("clave-que-no-debe-subir" not in content for _, content in docs)


def test_body_hash_ignora_el_frontmatter() -> None:
    """El gateway reescribe el frontmatter; incluirlo haría ver cambios donde no los hay."""
    a = "---\nname: x\nupdated_at: 2026-01-01\n---\nel cuerpo"
    b = "---\nname: x\nupdated_at: 2026-07-25\n---\nel cuerpo"
    assert body_hash_of(a) == body_hash_of(b)
    assert body_hash_of(a) != body_hash_of("---\nname: x\n---\notro cuerpo")


def test_body_hash_sin_frontmatter_usa_todo_el_texto() -> None:
    assert body_hash_of("sin frontmatter") == body_hash_of("sin frontmatter")
    assert body_hash_of("sin frontmatter") != body_hash_of("otro")


def test_local_state_devuelve_hashes(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "p", {"a.md": "uno"})
    assert local_state(project) == {"p/a.md": body_hash_of("uno")}


def test_write_memory_doc_rechaza_traversal(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "p", {"a.md": "x"})
    with pytest.raises(ValueError, match="escapa"):
        write_memory_doc(project, "p/../../fuera.md", "malicioso")


def test_write_memory_doc_crea_subdirs(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "p", {"a.md": "x"})
    written = write_memory_doc(project, "p/sub/nueva.md", "contenido")
    assert written.read_text(encoding="utf-8") == "contenido"


# --------------------------------------------------------------------------- #
# classify — el corazón: sólo se mueve lo que cambió de UN lado
# --------------------------------------------------------------------------- #


def _one(local: dict[str, str], remote: dict[str, str | None], base: dict[str, str]) -> Action:
    plan = classify(local, remote, base)
    assert len(plan) == 1
    return plan[0].action


def test_classify_todo_igual_es_noop() -> None:
    assert _one({"p/a.md": "h"}, {"p/a.md": "h"}, {"p/a.md": "h"}) is Action.NOOP


def test_classify_cambio_local_sube() -> None:
    assert _one({"p/a.md": "nuevo"}, {"p/a.md": "viejo"}, {"p/a.md": "viejo"}) is Action.UPLOAD


def test_classify_cambio_remoto_baja() -> None:
    assert _one({"p/a.md": "viejo"}, {"p/a.md": "nuevo"}, {"p/a.md": "viejo"}) is Action.DOWNLOAD


def test_classify_cambio_de_los_dos_lados_es_conflicto() -> None:
    """Lo que el usuario pidió: si difiere de ambos lados, ni sube ni baja."""
    assert _one({"p/a.md": "mio"}, {"p/a.md": "suyo"}, {"p/a.md": "base"}) is Action.CONFLICT


def test_classify_convergencia_sin_base_es_noop() -> None:
    """Ambos lados iguales y sin base (bootstrap): no hay nada que mover, se siembra base."""
    assert _one({"p/a.md": "h"}, {"p/a.md": "h"}, {}) is Action.NOOP


def test_classify_divergencia_sin_base_es_conflicto() -> None:
    """Sin base no se puede saber quién cambió → nadie gana automáticamente."""
    plan = classify({"p/a.md": "mio"}, {"p/a.md": "suyo"}, {})
    assert plan[0].action is Action.CONFLICT
    assert "no hay base" in plan[0].reason


def test_classify_nuevo_local_sube() -> None:
    assert _one({"p/a.md": "h"}, {}, {}) is Action.UPLOAD


def test_classify_nuevo_remoto_baja() -> None:
    """El caso de la migración: el corpus tiene memorias que esta máquina nunca vio."""
    assert _one({}, {"p/a.md": "h"}, {}) is Action.DOWNLOAD


def test_classify_borrado_local_no_propaga_borrado() -> None:
    """Borré local algo sincronizado: NO se borra del corpus, se reporta."""
    plan = classify({}, {"p/a.md": "h"}, {"p/a.md": "h"})
    assert plan[0].action is Action.CONFLICT
    assert "borrado acá" in plan[0].reason


def test_classify_borrado_remoto_no_resucita() -> None:
    """Lo borraron del corpus (forget): no lo re-subimos solos ni lo borramos de acá."""
    plan = classify({"p/a.md": "h"}, {}, {"p/a.md": "h"})
    assert plan[0].action is Action.CONFLICT
    assert "borrado en el corpus" in plan[0].reason


def test_classify_solo_en_base_desaparece() -> None:
    """Ya no está en ningún lado: se olvida, sin ruido."""
    assert classify({}, {}, {"p/a.md": "h"}) == []


def test_classify_es_determinista_y_ordenado() -> None:
    plan = classify({"p/b.md": "1", "p/a.md": "2"}, {}, {})
    assert [i.rel_path for i in plan] == ["p/a.md", "p/b.md"]


def test_classify_escenario_de_la_migracion() -> None:
    """El estado real del 2026-07-25: 1 igual, 1 sólo remoto, 1 sólo local, 0 borrados."""
    plan = classify(
        local={"p/igual.md": "h", "p/nueva-aca.md": "n"},
        remote={"p/igual.md": "h", "p/solo-corpus.md": "r"},
        base={},
    )
    acciones = {i.rel_path: i.action for i in plan}
    assert acciones == {
        "p/igual.md": Action.NOOP,
        "p/nueva-aca.md": Action.UPLOAD,
        "p/solo-corpus.md": Action.DOWNLOAD,
    }


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #


def test_journal_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(tmp_path, monkeypatch)
    save_journal("https://gw:7415", {"p/a.md": "h1"}, path_prefixes=["p/"])
    assert load_journal("https://gw:7415") == {"p/a.md": "h1"}


def test_journal_separa_por_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un journal mezclado entre tenants clasificaría mal (hashes de otro corpus)."""
    _isolate_env(tmp_path, monkeypatch)
    save_journal("https://uno:7415", {"p/a.md": "h1"}, path_prefixes=["p/"])
    save_journal("https://dos:7415", {"p/a.md": "h2"}, path_prefixes=["p/"])
    assert load_journal("https://uno:7415") == {"p/a.md": "h1"}
    assert load_journal("https://dos:7415") == {"p/a.md": "h2"}


def test_journal_conserva_prefijos_no_reconciliados(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correr con --project no debe invalidar la base de los demás proyectos."""
    _isolate_env(tmp_path, monkeypatch)
    save_journal("https://gw", {"a/x.md": "h1", "b/y.md": "h2"}, path_prefixes=["a/", "b/"])
    save_journal("https://gw", {"a/x.md": "h1b"}, path_prefixes=["a/"])
    assert load_journal("https://gw") == {"a/x.md": "h1b", "b/y.md": "h2"}


def test_journal_ausente_es_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(tmp_path, monkeypatch)
    assert load_journal("https://gw") == {}


def test_journal_corrupto_no_revienta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un journal ilegible degrada a bootstrap, no aborta el sync."""
    _isolate_env(tmp_path, monkeypatch)
    (tmp_path / "journal.json").write_text("{no es json", encoding="utf-8")
    assert load_journal("https://gw") == {}


def test_journal_version_desconocida_se_descarta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(tmp_path, monkeypatch)
    (tmp_path / "journal.json").write_text(json.dumps({"version": 99}), encoding="utf-8")
    assert load_journal("https://gw") == {}


def test_journal_permisos_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(tmp_path, monkeypatch)
    path = save_journal("https://gw", {"p/a.md": "h"}, path_prefixes=["p/"])
    assert path.stat().st_mode & 0o777 == 0o600


# --------------------------------------------------------------------------- #
# MemorySyncClient (httpx.MockTransport)
# --------------------------------------------------------------------------- #


def _client(
    handler: Callable[[httpx.Request], httpx.Response], *, key: str = "a2a_k"
) -> MemorySyncClient:
    return MemorySyncClient("https://gw:7415", key, transport=httpx.MockTransport(handler))


def test_remote_state_pide_el_snapshot() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["key"] = request.headers.get("X-Agent-Key")
        return httpx.Response(200, json={"partition": "claude-memory", "docs": {"p/a.md": "h"}})

    with _client(handler, key="a2a_secret") as client:
        assert client.remote_state("p/") == {"p/a.md": "h"}
    assert seen["path"] == "/v1/ingest/state"
    assert seen["params"] == {"partition": "claude-memory", "path_prefix": "p/"}
    assert seen["key"] == "a2a_secret"


def test_fetch_devuelve_contenidos_y_errores() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "partition": "claude-memory",
                "docs": [{"rel_path": "p/a.md", "content": "cuerpo"}],
                "errors": ["p/roto.md: no se pudo leer"],
            },
        )

    with _client(handler) as client:
        docs, errors = client.fetch(["p/a.md", "p/roto.md"])
    assert docs == [("p/a.md", "cuerpo")]
    assert len(errors) == 1


def test_fetch_trocea_lotes_grandes() -> None:
    """500 paths → más de una request: el endpoint tiene tope de 500 por cuerpo."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(len(body["paths"]))
        return httpx.Response(200, json={"partition": "claude-memory", "docs": [], "errors": []})

    with _client(handler) as client:
        client.fetch([f"p/n{i}.md" for i in range(450)])
    assert calls == [200, 200, 50]


def test_push_nunca_manda_prune() -> None:
    """La invariante central: el sync no borra. Ni por ausencia, ni por lista."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "partition": "claude-memory", "created": 1, "updated": 0,
                "unchanged": 0, "deleted": 0, "errors": [],
            },
        )

    with _client(handler) as client:
        client.push([("p/a.md", "x")], path_prefix="p/")
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["prune"] is False
    assert "delete_paths" not in body
    assert body["docs"] == [{"rel_path": "p/a.md", "content": "x"}]


def test_forget_usa_delete_paths_explicito() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "partition": "claude-memory", "created": 0, "updated": 0,
                "unchanged": 0, "deleted": 1, "errors": [],
            },
        )

    with _client(handler) as client:
        out = client.forget(["p/vieja.md"])
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["delete_paths"] == ["p/vieja.md"]
    assert body["prune"] is False
    assert out.deleted == 1


def test_cliente_eleva_en_error_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "AUTH_INSUFFICIENT_SCOPE"}})

    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError) as exc:
        client.remote_state()
    assert exc.value.response.status_code == 403


# --------------------------------------------------------------------------- #
# CLI (CliRunner) — con un gateway falso por MockTransport
# --------------------------------------------------------------------------- #


def _gateway(
    *, state: dict[str, str], contents: dict[str, str] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Gateway falso: /state devuelve `state`, /fetch sirve `contents`, /sync acepta todo."""
    pushed: list[str] = []
    contents = contents or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ingest/state":
            prefix = dict(request.url.params).get("path_prefix", "")
            return httpx.Response(
                200,
                json={
                    "partition": "claude-memory",
                    "docs": {k: v for k, v in state.items() if k.startswith(prefix)},
                },
            )
        if request.url.path == "/v1/ingest/fetch":
            paths = json.loads(request.content)["paths"]
            return httpx.Response(
                200,
                json={
                    "partition": "claude-memory",
                    "docs": [
                        {"rel_path": p, "content": contents[p]} for p in paths if p in contents
                    ],
                    "errors": [],
                },
            )
        body = json.loads(request.content)
        pushed.extend(d["rel_path"] for d in body.get("docs", []))
        return httpx.Response(
            200,
            json={
                "partition": "claude-memory",
                "created": len(body.get("docs", [])),
                "updated": 0,
                "unchanged": 0,
                "deleted": len(body.get("delete_paths") or []),
                "errors": [],
            },
        )

    handler.pushed = pushed  # type: ignore[attr-defined]
    return handler


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Hace que el CLI construya su cliente contra el gateway falso."""
    original = MemorySyncClient

    def factory(gateway_url: str, api_key: str, **kwargs: object) -> MemorySyncClient:
        return original(gateway_url, api_key, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("focusyn_cli.cli.MemorySyncClient", factory)


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--gateway-url", "https://gw:7415", "--api-key", "a2a_test",
        "--projects-root", str(tmp_path), *extra,
    ]


def test_cli_dry_run_consulta_el_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El dry-run viejo era 100% local y no avisaba de borrados; ahora muestra el plan REAL."""
    _isolate_env(tmp_path, monkeypatch)
    _make_project(tmp_path, "proj-a", {"local.md": "contenido"})
    handler = _gateway(
        state={"proj-a/remota.md": "hr"}, contents={"proj-a/remota.md": "del corpus"}
    )
    _patch_client(monkeypatch, handler)

    result = runner.invoke(app, ["memory", "sync", "--dry-run", *_args(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "↑ proj-a/local.md" in result.output
    assert "↓ proj-a/remota.md" in result.output
    # dry-run no escribe: ni el archivo remoto ni el journal.
    assert not (tmp_path / "proj-a" / "memory" / "remota.md").exists()
    assert load_journal("https://gw:7415") == {}


def test_cli_sync_descarga_lo_que_falta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El objetivo cross-equipo: lo que existe en el corpus y no acá, se baja."""
    _isolate_env(tmp_path, monkeypatch)
    _make_project(tmp_path, "proj-a", {"local.md": "mío"})
    handler = _gateway(
        state={"proj-a/remota.md": "hr"}, contents={"proj-a/remota.md": "del corpus"}
    )
    _patch_client(monkeypatch, handler)

    result = runner.invoke(app, ["memory", "sync", *_args(tmp_path)])

    assert result.exit_code == 0, result.output
    bajada = tmp_path / "proj-a" / "memory" / "remota.md"
    assert bajada.read_text(encoding="utf-8") == "del corpus"
    assert handler.pushed == ["proj-a/local.md"]  # type: ignore[attr-defined]
    # La base quedó sembrada con ambos → la próxima corrida es no-op.
    assert set(load_journal("https://gw:7415")) == {"proj-a/local.md", "proj-a/remota.md"}


def test_cli_sync_memory_vacio_no_borra_nada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La regresión que motivó todo: memory/ vacío + corpus poblado ⇒ DESCARGA, no borra."""
    _isolate_env(tmp_path, monkeypatch)
    (tmp_path / "proj-a" / "memory").mkdir(parents=True)  # existe pero vacío
    handler = _gateway(
        state={"proj-a/MEMORY.md": "h1", "proj-a/nota.md": "h2"},
        contents={"proj-a/MEMORY.md": "índice", "proj-a/nota.md": "nota"},
    )
    _patch_client(monkeypatch, handler)

    result = runner.invoke(app, ["memory", "sync", *_args(tmp_path, "--project", "proj-a")])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "proj-a" / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "índice"
    assert (tmp_path / "proj-a" / "memory" / "nota.md").read_text(encoding="utf-8") == "nota"
    assert handler.pushed == []  # type: ignore[attr-defined]


def test_cli_sync_conflicto_no_toca_nada_y_sale_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discrepancia sin base común: ni sube ni baja, reporta y sale con código propio."""
    _isolate_env(tmp_path, monkeypatch)
    _make_project(tmp_path, "proj-a", {"MEMORY.md": "mi versión"})
    handler = _gateway(
        state={"proj-a/MEMORY.md": "otro-hash"}, contents={"proj-a/MEMORY.md": "su versión"}
    )
    _patch_client(monkeypatch, handler)

    result = runner.invoke(app, ["memory", "sync", *_args(tmp_path)])

    assert result.exit_code == 3
    assert "CONFLICTO" in result.output
    # El disco local intacto y nada subido.
    disco = tmp_path / "proj-a" / "memory" / "MEMORY.md"
    assert disco.read_text(encoding="utf-8") == "mi versión"
    assert handler.pushed == []  # type: ignore[attr-defined]


def test_cli_pull_only_no_sube(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(tmp_path, monkeypatch)
    _make_project(tmp_path, "proj-a", {"local.md": "mío"})
    handler = _gateway(state={"proj-a/remota.md": "h"}, contents={"proj-a/remota.md": "corpus"})
    _patch_client(monkeypatch, handler)

    result = runner.invoke(app, ["memory", "sync", "--pull-only", *_args(tmp_path)])

    assert result.exit_code == 0, result.output
    assert handler.pushed == []  # type: ignore[attr-defined]
    assert (tmp_path / "proj-a" / "memory" / "remota.md").exists()


def test_cli_push_only_no_baja(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(tmp_path, monkeypatch)
    _make_project(tmp_path, "proj-a", {"local.md": "mío"})
    handler = _gateway(state={"proj-a/remota.md": "h"}, contents={"proj-a/remota.md": "corpus"})
    _patch_client(monkeypatch, handler)

    result = runner.invoke(app, ["memory", "sync", "--push-only", *_args(tmp_path)])

    assert result.exit_code == 0, result.output
    assert handler.pushed == ["proj-a/local.md"]  # type: ignore[attr-defined]
    assert not (tmp_path / "proj-a" / "memory" / "remota.md").exists()


def test_cli_status_no_escribe_nada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(tmp_path, monkeypatch)
    _make_project(tmp_path, "proj-a", {"local.md": "mío"})
    handler = _gateway(state={"proj-a/remota.md": "h"}, contents={"proj-a/remota.md": "corpus"})
    _patch_client(monkeypatch, handler)

    result = runner.invoke(app, ["memory", "status", *_args(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "↑ proj-a/local.md" in result.output and "↓ proj-a/remota.md" in result.output
    assert not (tmp_path / "proj-a" / "memory" / "remota.md").exists()
    assert load_journal("https://gw:7415") == {}


def test_cli_resolve_prefer_remote_escribe_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(tmp_path, monkeypatch)
    _make_project(tmp_path, "proj-a", {"MEMORY.md": "mi versión"})
    handler = _gateway(
        state={"proj-a/MEMORY.md": "h"}, contents={"proj-a/MEMORY.md": "la del corpus"}
    )
    _patch_client(monkeypatch, handler)

    result = runner.invoke(
        app,
        [
            "memory", "resolve", "proj-a/MEMORY.md", "--prefer-remote",
            "--gateway-url", "https://gw:7415", "--api-key", "a2a_test",
            "--projects-root", str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    disco = tmp_path / "proj-a" / "memory" / "MEMORY.md"
    assert disco.read_text(encoding="utf-8") == "la del corpus"
    # La base quedó sembrada → el conflicto no reaparece.
    assert load_journal("https://gw:7415")["proj-a/MEMORY.md"] == body_hash_of("la del corpus")


def test_cli_resolve_prefer_local_sube(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(tmp_path, monkeypatch)
    _make_project(tmp_path, "proj-a", {"MEMORY.md": "mi versión"})
    handler = _gateway(state={"proj-a/MEMORY.md": "h"}, contents={"proj-a/MEMORY.md": "la otra"})
    _patch_client(monkeypatch, handler)

    result = runner.invoke(
        app,
        [
            "memory", "resolve", "proj-a/MEMORY.md", "--prefer-local",
            "--gateway-url", "https://gw:7415", "--api-key", "a2a_test",
            "--projects-root", str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert handler.pushed == ["proj-a/MEMORY.md"]  # type: ignore[attr-defined]
    disco = tmp_path / "proj-a" / "memory" / "MEMORY.md"
    assert disco.read_text(encoding="utf-8") == "mi versión"  # el local no se toca


def test_cli_resolve_acepta_un_slug_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Los slugs REALES empiezan con '-' y click los leía como una opción.

    Regresión de producción: `memory resolve -home-melquiades-focusyn/MEMORY.md
    --prefer-local` moría con "No such option: -h" — o sea el caso NORMAL, no un borde,
    porque el slug deriva del path absoluto y siempre arranca en guion.
    """
    _isolate_env(tmp_path, monkeypatch)
    _make_project(tmp_path, _REAL_SLUG, {"MEMORY.md": "mi versión"})
    handler = _gateway(
        state={f"{_REAL_SLUG}/MEMORY.md": "h"}, contents={f"{_REAL_SLUG}/MEMORY.md": "la otra"}
    )
    _patch_client(monkeypatch, handler)

    result = runner.invoke(
        app,
        [
            "memory", "resolve", f"{_REAL_SLUG}/MEMORY.md", "--prefer-local",
            "--gateway-url", "https://gw:7415", "--api-key", "a2a_test",
            "--projects-root", str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert handler.pushed == [f"{_REAL_SLUG}/MEMORY.md"]  # type: ignore[attr-defined]


def test_cli_forget_acepta_un_slug_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mismo caso que resolve: el rel_path arranca con guion."""
    _isolate_env(tmp_path, monkeypatch)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "partition": "claude-memory", "created": 0, "updated": 0,
                "unchanged": 0, "deleted": 1, "errors": [],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(
        app,
        [
            "memory", "forget", f"{_REAL_SLUG}/vieja.md", "--yes",
            "--gateway-url", "https://gw:7415", "--api-key", "a2a_test",
        ],
    )

    assert result.exit_code == 0, result.output
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["delete_paths"] == [f"{_REAL_SLUG}/vieja.md"]


def test_comandos_con_rel_path_no_declaran_flags_cortos() -> None:
    """Ningún comando que reciba un rel_path puede tener alias cortos (`-y`, `-f`, …).

    Los rel_path empiezan con '-' y click parsea ese token como un GRUPO de flags cortos,
    consumiendo cada letra que coincida con uno declarado. Con `-y` en `forget`, el path
    `-home-usuario-miproyecto/x.md` llegaba como `-home-usuario-miproecto/x.md`: corrupción
    silenciosa que apunta a otro documento. Sin cortos declarados, el token entero cae al
    argumento posicional.
    """
    from typer.main import get_command

    grupo = get_command(app).commands["memory"]  # type: ignore[attr-defined]
    for nombre in ("resolve", "forget"):
        cmd = grupo.commands[nombre]  # type: ignore[attr-defined]
        cortos = [
            opt
            for p in cmd.params
            for opt in getattr(p, "opts", [])
            if opt.startswith("-") and not opt.startswith("--")
        ]
        assert not cortos, f"'memory {nombre}' declara flags cortos {cortos}"


def test_cli_resolve_exige_un_lado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(tmp_path, monkeypatch)
    result = runner.invoke(
        app, ["memory", "resolve", "proj-a/MEMORY.md", "--projects-root", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "exactamente uno" in result.output


def test_cli_forget_borra_del_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(tmp_path, monkeypatch)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "partition": "claude-memory", "created": 0, "updated": 0,
                "unchanged": 0, "deleted": 1, "errors": [],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(
        app,
        [
            "memory", "forget", "proj-a/vieja.md", "--yes",
            "--gateway-url", "https://gw:7415", "--api-key", "a2a_test",
        ],
    )

    assert result.exit_code == 0, result.output
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["delete_paths"] == ["proj-a/vieja.md"]
    assert "1 borradas" in result.output


def test_cli_forget_exige_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(tmp_path, monkeypatch)
    result = runner.invoke(app, ["memory", "forget", "suelta.md", "--yes"])
    assert result.exit_code != 0
    assert "slug" in result.output


# --------------------------------------------------------------------------- #
# Credencial (scope ingest): mensajes accionables
# --------------------------------------------------------------------------- #


def test_cli_sin_api_key_falla(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_env(tmp_path, monkeypatch)
    monkeypatch.setenv("FOCUSYN_GATEWAY_URL", "https://gw:7415")
    _make_project(tmp_path, "proj-a", {"MEMORY.md": "a"})
    result = runner.invoke(app, ["memory", "sync", "--projects-root", str(tmp_path)])
    assert result.exit_code != 0
    out = result.output.lower()
    assert "ingest" in out and ("--ingest-key" in out or "focusyn_ingest_key" in out)


def test_cli_jwt_no_sirve_para_el_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Perfil con JWT (login) sin key ingest → error CLARO, no silencio."""
    _isolate_env(tmp_path, monkeypatch)
    cfg.save_config(
        cfg.Config(profiles={"default": Credential(gateway_url="https://gw", access_token="jwt")})
    )
    _make_project(tmp_path, "proj-a", {"MEMORY.md": "a"})
    result = runner.invoke(app, ["memory", "sync", "--projects-root", str(tmp_path)])
    assert result.exit_code != 0
    assert "JWT" in result.output and "ingest" in result.output.lower()


def test_cli_usa_la_ingest_key_del_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """login (JWT) + config.ingest_key → el hook usa la key de máquina, no revienta."""
    _isolate_env(tmp_path, monkeypatch)
    cfg.save_config(
        cfg.Config(
            profiles={"default": Credential(gateway_url="https://gw", access_token="jwt")},
            ingest_key="a2a_config_ingest",
        )
    )
    _make_project(tmp_path, "proj-a", {"MEMORY.md": "a"})
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("X-Agent-Key", "")
        if request.url.path == "/v1/ingest/state":
            return httpx.Response(200, json={"partition": "claude-memory", "docs": {}})
        return httpx.Response(
            200,
            json={
                "partition": "claude-memory", "created": 1, "updated": 0,
                "unchanged": 0, "deleted": 0, "errors": [],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["memory", "sync", "--projects-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert seen["key"] == "a2a_config_ingest"  # usó la key del config, no el JWT
