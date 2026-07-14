"""Cliente de sync de memorias de Claude Code → corpus del gateway (refactor pre-9.7, F3/S8).

``focusyn memory sync`` recorre ``~/.claude/projects/<proj>/memory/``, recoge los
``*.md`` (incl. ``MEMORY.md``; **ignora** los ``.jsonl`` de transcripts) y los envía a
``POST /v1/ingest/sync`` con ``path_prefix='<proj>/'`` y ``prune=true``. El gateway
reconcilia el corpus contra el disco: create/update por ``body_hash`` + delete por
ausencia, **acotado al subárbol del proyecto** — re-correr es no-op, borrar una memoria
local la borra del índice en el próximo sync.

Es el cliente **manual** que la Fase 9.7 (hooks de Claude Code) automatizará por
proyecto/sesión. La lógica vive aquí (testeable con ``httpx.MockTransport``); el CLI
(``focusyn.cli``) es una cáscara fina. Autentica con un agente scope ``ingest``
(``focusyn agent create --name memory-pusher --scopes ingest``).

Ver el vault: ADR MEL-DEC-130 (corpus claude-memory).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx

from focusyn_cli.contracts import CORPUS_PARTITION_DEFAULT, IngestSyncOut

# Raíz por defecto de los proyectos de Claude Code en el host.
DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
# Subdirectorio de memorias dentro de cada proyecto.
_MEMORY_SUBDIR = "memory"
# Header de autenticación del gateway (mismo que el resto de la API por key).
_AGENT_KEY_HEADER = "X-Agent-Key"


@dataclass(frozen=True)
class MemoryProject:
    """Un proyecto de Claude Code con memorias: su slug y el ``memory/`` real."""

    slug: str
    memory_dir: Path


@dataclass(frozen=True)
class ProjectSyncResult:
    """Resultado del sync de un proyecto: conteos del reconcile del gateway."""

    slug: str
    path_prefix: str
    sent: int
    created: int
    updated: int
    unchanged: int
    deleted: int
    errors: list[str] = field(default_factory=list)


def discover_projects(projects_root: Path) -> list[MemoryProject]:
    """Lista los proyectos bajo ``projects_root`` que tienen un ``memory/``.

    El slug es el nombre del directorio del proyecto (p. ej.
    ``-home-usuario-miproyecto``). Orden estable por slug (determinista).
    """
    if not projects_root.is_dir():
        return []
    out: list[MemoryProject] = []
    for child in sorted(projects_root.iterdir()):
        memory_dir = child / _MEMORY_SUBDIR
        if memory_dir.is_dir():
            out.append(MemoryProject(slug=child.name, memory_dir=memory_dir))
    return out


def collect_memory_docs(project: MemoryProject) -> list[tuple[str, str]]:
    """Recoge los ``*.md`` del ``memory/`` del proyecto como ``(rel_path, content)``.

    ``rel_path = '<slug>/<ruta relativa al memory/>'``: incluye el slug → único entre
    proyectos y cae bajo ``path_prefix='<slug>/'`` (lo exige el validador del endpoint).
    **Sólo ``.md``**: ignora ``.jsonl`` (transcripts) y cualquier otra extensión. Orden
    estable por ruta (determinista). Las rutas se emiten en estilo POSIX.
    """
    docs: list[tuple[str, str]] = []
    for md in sorted(project.memory_dir.rglob("*.md")):
        if md.is_symlink():
            continue  # un symlink a ~/.ssh/config pasa `is_file()` y su contenido acabaría en el
            # corpus: acá sólo se sube lo que VIVE en memory/. El `path_prefix` del endpoint acota
            # el DESTINO, no el origen — este filtro es el que cuida el origen.
        if not md.is_file():
            continue  # un directorio que case el glob
        rel_within = md.relative_to(project.memory_dir).as_posix()
        rel_path = f"{project.slug}/{rel_within}"
        docs.append((rel_path, md.read_text(encoding="utf-8")))
    return docs


class MemorySyncClient:
    """Cliente HTTP síncrono de ``POST /v1/ingest/sync``.

    El ``transport`` se inyecta en tests (``httpx.MockTransport``); en producción es
    ``None`` (transporte por defecto). Usable como context manager (``with``) para cerrar
    el cliente. El ``api_key`` (header ``X-Agent-Key``) **nunca** se loguea.
    """

    def __init__(
        self,
        gateway_url: str,
        api_key: str,
        *,
        partition: str = CORPUS_PARTITION_DEFAULT,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._partition = partition
        self._client = httpx.Client(
            base_url=gateway_url.rstrip("/"),
            timeout=timeout,
            headers={_AGENT_KEY_HEADER: api_key},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MemorySyncClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def sync_project(self, project: MemoryProject, *, prune: bool = True) -> ProjectSyncResult:
        """Materializa + reconcilia las memorias de un proyecto contra el corpus.

        Eleva ``httpx.HTTPStatusError`` ante un status de error (4xx/5xx) del gateway.
        """
        docs = collect_memory_docs(project)
        path_prefix = f"{project.slug}/"
        resp = self._client.post(
            "/v1/ingest/sync",
            json={
                "partition": self._partition,
                "path_prefix": path_prefix,
                "docs": [{"rel_path": rp, "content": c} for rp, c in docs],
                "prune": prune,
            },
        )
        resp.raise_for_status()
        out = IngestSyncOut.model_validate(resp.json())
        return ProjectSyncResult(
            slug=project.slug,
            path_prefix=path_prefix,
            sent=len(docs),
            created=out.created,
            updated=out.updated,
            unchanged=out.unchanged,
            deleted=out.deleted,
            errors=out.errors,
        )


__all__ = [
    "DEFAULT_PROJECTS_ROOT",
    "MemoryProject",
    "ProjectSyncResult",
    "MemorySyncClient",
    "discover_projects",
    "collect_memory_docs",
]
