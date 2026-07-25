"""Sync de memorias de Claude Code ↔ corpus del gateway — reconciliación cross-equipo.

``focusyn memory sync`` recorre ``~/.claude/projects/<proj>/memory/``, recoge los ``*.md``
(incl. ``MEMORY.md``; **ignora** los ``.jsonl`` de transcripts) y los reconcilia contra la
partición corpus **en los dos sentidos**: sube lo que cambió acá y baja lo que cambió en otra
máquina. El corpus es el punto de encuentro entre equipos, no el espejo de uno.

Tres invariantes lo gobiernan:

1. **Nunca borra por su cuenta.** El sync sólo crea, actualiza y descarga. Borrar del corpus
   es ``focusyn memory forget`` — explícito y enumerado. La versión anterior deducía los
   borrados de la *ausencia* (``prune``), y por eso un ``memory/`` vacío —una máquina recién
   migrada, un directorio que no se copió— borraba el proyecto entero del corpus.
2. **Ante discrepancia real, se detiene.** Si un archivo cambió de los dos lados desde la
   última reconciliación, es CONFLICTO: no sube ni baja, lo reporta y sigue con el resto.
   Se resuelve con ``focusyn memory resolve --prefer-local|--prefer-remote``.
3. **La comparación es por contenido, no por fecha.** ``body_hash_of`` (SHA-256 del body sin
   frontmatter) debe dar byte-idéntico al del gateway; los mtime no viajan entre máquinas y
   una copia de archivos los reescribe.

La clasificación es a **tres bandas** (local / remoto / base), donde *base* es el estado de la
última reconciliación exitosa, guardado en el journal. Sin base no se puede distinguir "el
otro equipo lo creó" de "yo lo borré", que es exactamente la ambigüedad que causaba borrados
silenciosos.

Ver el vault: ADR MEL-DEC-130 (corpus claude-memory).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from focusyn_cli.contracts import CORPUS_PARTITION_DEFAULT, IngestSyncOut

# Raíz por defecto de los proyectos de Claude Code en el host.
DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
# Subdirectorio de memorias dentro de cada proyecto.
_MEMORY_SUBDIR = "memory"
# Header de autenticación del gateway (mismo que el resto de la API por key).
_AGENT_KEY_HEADER = "X-Agent-Key"
# Versión del formato del journal (migrable sin romper: un formato viejo se descarta).
_JOURNAL_VERSION = 1


# --------------------------------------------------------------------------- #
# Hash de contenido — ESPEJO de focusyn.graphrag.ingest.body_hash_of
# --------------------------------------------------------------------------- #

# Espejo de focusyn.vault.frontmatter._FM_RE. El cliente no puede importar del gateway
# (arrastraría fastapi/sqlalchemy/…), así que la derivación se copia y el guard
# tests/contract/test_client_contracts.py del gateway asserta que no divergen. Si divergen,
# TODO se clasifica como conflicto: el síntoma sería un sync que no hace nada y se queja.
_FM_RE = re.compile(r"^---\r?\n(.*?\r?\n)---\r?\n?", re.DOTALL)


def body_hash_of(content: str) -> str:
    """SHA-256 del body markdown (sin frontmatter) — idéntico al ``body_hash`` del gateway.

    El frontmatter queda fuera a propósito: el gateway lo gobierna y lo reescribe, así que
    incluirlo haría que cada doc pareciera modificado tras cada ingesta.
    """
    m = _FM_RE.match(content)
    body = content[m.end() :] if m else content
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Descubrimiento del disco local
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MemoryProject:
    """Un proyecto de Claude Code con memorias: su slug y el ``memory/`` real."""

    slug: str
    memory_dir: Path


@dataclass(frozen=True)
class ProjectSyncResult:
    """Resultado de reconciliar un proyecto: qué se movió y qué quedó trabado."""

    slug: str
    path_prefix: str
    uploaded: int = 0
    downloaded: int = 0
    unchanged: int = 0
    conflicts: list[PlanItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """¿Hubo algo que reportar? (para el modo --quiet del hook)."""
        return bool(self.uploaded or self.downloaded or self.conflicts or self.errors)


def discover_projects(projects_root: Path) -> list[MemoryProject]:
    """Lista los proyectos bajo ``projects_root`` con un ``memory/`` **no vacío**.

    Un ``memory/`` vacío se ignora deliberadamente: bajo el modelo anterior significaba
    "borrá todo lo de este proyecto en el corpus", que es lo que un directorio recién creado
    (o no copiado al migrar de máquina) dice sin querer. Ahora un proyecto sin memorias
    locales simplemente no participa del push; si el corpus tiene memorias suyas, se
    descargan cuando el proyecto se pide explícitamente con ``--project``.

    Orden estable por slug (determinista).
    """
    if not projects_root.is_dir():
        return []
    out: list[MemoryProject] = []
    for child in sorted(projects_root.iterdir()):
        memory_dir = child / _MEMORY_SUBDIR
        if memory_dir.is_dir() and any(_iter_memory_files(memory_dir)):
            out.append(MemoryProject(slug=child.name, memory_dir=memory_dir))
    return out


def project_for_slug(projects_root: Path, slug: str) -> MemoryProject:
    """``MemoryProject`` de ``slug``, exista o no su ``memory/`` (para descargar de cero)."""
    return MemoryProject(slug=slug, memory_dir=projects_root / slug / _MEMORY_SUBDIR)


def _iter_memory_files(memory_dir: Path) -> list[Path]:
    """Los ``*.md`` reales de un ``memory/`` (sin symlinks ni directorios), ordenados."""
    out: list[Path] = []
    for md in sorted(memory_dir.rglob("*.md")):
        # Un symlink a ~/.ssh/config pasa is_file() y su contenido acabaría en el corpus:
        # acá sólo sube lo que VIVE en memory/.
        if md.is_symlink() or not md.is_file():
            continue
        out.append(md)
    return out


def collect_memory_docs(project: MemoryProject) -> list[tuple[str, str]]:
    """Recoge los ``*.md`` del proyecto como ``(rel_path, content)``.

    ``rel_path = '<slug>/<ruta relativa al memory/>'``: incluye el slug → único entre
    proyectos y cae bajo ``path_prefix='<slug>/'``. Rutas en estilo POSIX, orden estable.
    """
    if not project.memory_dir.is_dir():
        return []
    return [
        (
            f"{project.slug}/{md.relative_to(project.memory_dir).as_posix()}",
            md.read_text(encoding="utf-8"),
        )
        for md in _iter_memory_files(project.memory_dir)
    ]


def local_state(project: MemoryProject) -> dict[str, str]:
    """``{rel_path -> body_hash}`` del disco local del proyecto."""
    return {rel: body_hash_of(content) for rel, content in collect_memory_docs(project)}


def write_memory_doc(project: MemoryProject, rel_path: str, content: str) -> Path:
    """Materializa un doc descargado en el ``memory/`` del proyecto.

    ``rel_path`` viene con el slug adelante (como en el corpus); se le quita para escribir
    dentro de ``memory/``. Valida que no escape del directorio del proyecto.
    """
    prefix = f"{project.slug}/"
    rel_within = rel_path[len(prefix) :] if rel_path.startswith(prefix) else rel_path
    base = project.memory_dir.resolve()
    target = (project.memory_dir / rel_within).resolve()
    if not str(target).startswith(str(base) + os.sep) and target != base:
        raise ValueError(f"ruta '{rel_path}' escapa de {base}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Journal: el estado de la última reconciliación (la "base" del 3-way)
# --------------------------------------------------------------------------- #


def journal_path() -> Path:
    """Ruta del journal: junto al config (``$FOCUSYN_CONFIG`` respeta el override del test)."""
    override = os.environ.get("FOCUSYN_MEMORY_JOURNAL")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "focusyn" / "memory-state.json"


def _gateway_key(gateway_url: str) -> str:
    """Clave de journal por gateway: host (+puerto). Un journal mezclado clasificaría mal."""
    parts = urlsplit(gateway_url if "//" in gateway_url else f"//{gateway_url}")
    return parts.netloc or gateway_url


def load_journal(gateway_url: str) -> dict[str, str]:
    """``{rel_path -> body_hash}`` de la última reconciliación con ese gateway.

    Un journal ausente, ilegible o de otra versión devuelve ``{}`` — que es el bootstrap:
    sin base, lo que coincide de los dos lados siembra la base y lo que difiere es conflicto.
    """
    path = journal_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != _JOURNAL_VERSION:
        return {}
    gateways = raw.get("gateways")
    if not isinstance(gateways, dict):
        return {}
    entry = gateways.get(_gateway_key(gateway_url))
    if not isinstance(entry, dict):
        return {}
    docs = entry.get("docs")
    if not isinstance(docs, dict):
        return {}
    return {str(k): str(v) for k, v in docs.items()}


def save_journal(gateway_url: str, docs: dict[str, str], *, path_prefixes: list[str]) -> Path:
    """Persiste la base para ``gateway_url``, reemplazando sólo los prefijos reconciliados.

    Correr con ``--project`` no debe invalidar la base de los proyectos que no se tocaron:
    se conserva todo lo que caiga fuera de ``path_prefixes``.
    """
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != _JOURNAL_VERSION:
            raw = {}
    except (OSError, ValueError):
        raw = {}

    gateways = raw.get("gateways")
    if not isinstance(gateways, dict):
        gateways = {}
    key = _gateway_key(gateway_url)
    entry = gateways.get(key)
    previous: dict[str, str] = {}
    if isinstance(entry, dict) and isinstance(entry.get("docs"), dict):
        previous = {str(k): str(v) for k, v in entry["docs"].items()}

    merged = {
        rel: h
        for rel, h in previous.items()
        if not any(rel.startswith(p) for p in path_prefixes)
    }
    merged.update(docs)
    gateways[key] = {"partition": CORPUS_PARTITION_DEFAULT, "docs": merged}

    path.write_text(
        json.dumps({"version": _JOURNAL_VERSION, "gateways": gateways}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    path.chmod(0o600)  # mismo trato que el config: es estado de la cuenta, no público
    return path


# --------------------------------------------------------------------------- #
# Clasificación a tres bandas (función pura: local × remoto × base → plan)
# --------------------------------------------------------------------------- #


class Action(StrEnum):
    """Qué hacer con un documento tras comparar los tres estados."""

    NOOP = "noop"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class PlanItem:
    """Una decisión por documento, con el porqué (lo que se le muestra al humano)."""

    rel_path: str
    action: Action
    reason: str = ""


def classify(
    local: dict[str, str], remote: dict[str, str | None], base: dict[str, str]
) -> list[PlanItem]:
    """Decide qué hacer con cada documento. Pura y determinista (ordenada por ruta).

    La regla de fondo: **sólo se mueve lo que cambió de UN lado**. Si cambió de los dos,
    nadie gana automáticamente; si desapareció de un lado, tampoco se propaga el borrado —
    ambos casos salen como CONFLICT para que decida una persona.
    """
    plan: list[PlanItem] = []
    for rel in sorted(set(local) | set(remote) | set(base)):
        loc, rem, bas = local.get(rel), remote.get(rel), base.get(rel)

        if loc is not None and rem is not None:
            if loc == rem:
                # Convergieron (o nunca divergieron): nada que mover, la base se re-siembra.
                plan.append(PlanItem(rel, Action.NOOP))
            elif bas is not None and loc == bas:
                plan.append(PlanItem(rel, Action.DOWNLOAD, "cambió en el corpus"))
            elif bas is not None and rem == bas:
                plan.append(PlanItem(rel, Action.UPLOAD, "cambió acá"))
            else:
                # Cambió de los dos lados, o no hay base para saber quién cambió.
                plan.append(
                    PlanItem(
                        rel,
                        Action.CONFLICT,
                        "difiere de los dos lados" if bas else "difiere y no hay base común",
                    )
                )
        elif loc is not None:  # sólo local
            if bas is None:
                plan.append(PlanItem(rel, Action.UPLOAD, "nuevo acá"))
            else:
                # Estaba sincronizado y ya no está en el corpus: alguien lo borró allá. No lo
                # resucitamos solos (sería deshacer un forget ajeno) ni lo borramos de acá.
                plan.append(PlanItem(rel, Action.CONFLICT, "borrado en el corpus, sigue acá"))
        elif rem is not None:  # sólo remoto
            if bas is None:
                plan.append(PlanItem(rel, Action.DOWNLOAD, "nuevo en el corpus"))
            else:
                # Se borró local algo que estaba sincronizado. Con borrados no automáticos,
                # esto se reporta: `memory forget` lo cierra, `resolve --prefer-remote` lo baja.
                plan.append(PlanItem(rel, Action.CONFLICT, "borrado acá, sigue en el corpus"))
        # loc y rem None ⇒ sólo quedaba en la base: ya no existe en ningún lado, se olvida.
    return plan


# --------------------------------------------------------------------------- #
# Cliente HTTP
# --------------------------------------------------------------------------- #


class MemorySyncClient:
    """Cliente HTTP síncrono de las rutas ``/v1/ingest/{state,fetch,sync}``.

    El ``transport`` se inyecta en tests (``httpx.MockTransport``); en producción es ``None``.
    Usable como context manager. El ``api_key`` (header ``X-Agent-Key``) **nunca** se loguea.
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
        self.gateway_url = gateway_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.gateway_url,
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

    def remote_state(self, path_prefix: str | None = None) -> dict[str, str | None]:
        """``{rel_path -> body_hash}`` del corpus: clasificar sin descargar contenido."""
        params = {"partition": self._partition}
        if path_prefix:
            params["path_prefix"] = path_prefix
        resp = self._client.get("/v1/ingest/state", params=params)
        resp.raise_for_status()
        docs = resp.json().get("docs") or {}
        return {str(k): v for k, v in docs.items()}

    def fetch(self, paths: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
        """Descarga el contenido de ``paths`` → ``(docs, errors)``. Trocea de a 200."""
        docs: list[tuple[str, str]] = []
        errors: list[str] = []
        for i in range(0, len(paths), 200):  # el endpoint admite 500; 200 deja margen
            chunk = paths[i : i + 200]
            resp = self._client.post(
                "/v1/ingest/fetch", json={"partition": self._partition, "paths": chunk}
            )
            resp.raise_for_status()
            body = resp.json()
            docs.extend((d["rel_path"], d["content"]) for d in body.get("docs", []))
            errors.extend(body.get("errors", []))
        return docs, errors

    def push(self, docs: list[tuple[str, str]], *, path_prefix: str | None = None) -> IngestSyncOut:
        """Sube docs SIN borrar nada (``prune=false``, sin ``delete_paths``)."""
        resp = self._client.post(
            "/v1/ingest/sync",
            json={
                "partition": self._partition,
                "path_prefix": path_prefix,
                "docs": [{"rel_path": rp, "content": c} for rp, c in docs],
                "prune": False,
            },
        )
        resp.raise_for_status()
        return IngestSyncOut.model_validate(resp.json())

    def forget(self, paths: list[str]) -> IngestSyncOut:
        """Borra del corpus **exactamente** ``paths`` (borrado explícito, idempotente)."""
        resp = self._client.post(
            "/v1/ingest/sync",
            json={"partition": self._partition, "docs": [], "prune": False, "delete_paths": paths},
        )
        resp.raise_for_status()
        return IngestSyncOut.model_validate(resp.json())


# --------------------------------------------------------------------------- #
# Motor de reconciliación
# --------------------------------------------------------------------------- #


def plan_project(
    client: MemorySyncClient, project: MemoryProject, base: dict[str, str]
) -> tuple[list[PlanItem], dict[str, str], dict[str, str | None]]:
    """Plan de un proyecto → ``(plan, local, remote)``. No escribe nada."""
    prefix = f"{project.slug}/"
    local = local_state(project)
    remote = client.remote_state(prefix)
    scoped_base = {k: v for k, v in base.items() if k.startswith(prefix)}
    return classify(local, remote, scoped_base), local, remote


def apply_plan(
    client: MemorySyncClient,
    project: MemoryProject,
    plan: list[PlanItem],
    local: dict[str, str],
    remote: dict[str, str | None],
) -> tuple[ProjectSyncResult, dict[str, str]]:
    """Ejecuta un plan → ``(resultado, base_nueva)``.

    La base nueva sólo incluye lo que quedó **efectivamente convergido**: los NOOP, lo subido
    y lo bajado. Un conflicto no entra — si entrara, la próxima corrida creería que ese estado
    fue acordado y el conflicto desaparecería sin que nadie lo resolviera.
    """
    prefix = f"{project.slug}/"
    errors: list[str] = []
    new_base: dict[str, str] = {}
    uploaded = downloaded = 0

    for item in plan:
        if item.action is Action.NOOP:
            new_base[item.rel_path] = local[item.rel_path]

    to_upload = [i.rel_path for i in plan if i.action is Action.UPLOAD]
    if to_upload:
        contents = dict(collect_memory_docs(project))
        payload = [(rel, contents[rel]) for rel in to_upload if rel in contents]
        out = client.push(payload, path_prefix=prefix)
        errors.extend(out.errors)
        uploaded = out.created + out.updated
        for rel, _ in payload:
            new_base[rel] = local[rel]

    to_download = [i.rel_path for i in plan if i.action is Action.DOWNLOAD]
    if to_download:
        fetched, fetch_errors = client.fetch(to_download)
        errors.extend(fetch_errors)
        for rel, content in fetched:
            try:
                write_memory_doc(project, rel, content)
            except (OSError, ValueError) as exc:
                errors.append(f"{rel}: no se pudo escribir: {exc}")
                continue
            downloaded += 1
            # La base se siembra con el hash del contenido REALMENTE escrito, no con el que
            # anunció el índice: si difirieran, la próxima corrida lo vería como cambio local.
            new_base[rel] = body_hash_of(content)

    conflicts = [i for i in plan if i.action is Action.CONFLICT]
    result = ProjectSyncResult(
        slug=project.slug,
        path_prefix=prefix,
        uploaded=uploaded,
        downloaded=downloaded,
        unchanged=sum(1 for i in plan if i.action is Action.NOOP),
        conflicts=conflicts,
        errors=errors,
    )
    return result, new_base


__all__ = [
    "DEFAULT_PROJECTS_ROOT",
    "Action",
    "MemoryProject",
    "MemorySyncClient",
    "PlanItem",
    "ProjectSyncResult",
    "apply_plan",
    "body_hash_of",
    "classify",
    "collect_memory_docs",
    "discover_projects",
    "journal_path",
    "load_journal",
    "local_state",
    "plan_project",
    "project_for_slug",
    "save_journal",
    "write_memory_doc",
]
