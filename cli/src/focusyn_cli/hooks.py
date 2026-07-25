"""Instala/quita los hooks de memory sync en ``~/.claude/settings.json`` — idempotente y seguro.

Reemplaza el bloque que hoy se edita a mano (con la API key INLINE). El comando del hook nuevo NO
lleva la key: invoca ``focusyn memory sync --quiet`` y el CLI la lee del config 0600. Preserva todo
lo ajeno del settings, hace backup, y escribe atómico.

⚠️ Trampa #1: se escribe la ruta del **shim estable** (``~/.local/bin/focusyn``, lo que resuelve
``shutil.which``), NO ``sys.executable`` (el python interno del venv de la tool cambia en cada
``uv tool upgrade`` → el hook rompería callado).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Eventos que disparan el sync (mismos que el diseño 9.7). SessionEnd al cerrar; PreCompact antes de
# compactar. Ambos async: no retrasan la salida ni bloquean la compactación.
DEFAULT_EVENTS: tuple[str, ...] = ("SessionEnd", "PreCompact")
# Lista blanca: `--events` es input del usuario y termina DENTRO de un comando de shell escrito en
# settings.json. Aceptar cualquier string dejaba escribir un comando arbitrario que corre solo al
# cerrar cada sesión. Sólo eventos de Claude Code que tengan sentido para un sync de memorias.
VALID_EVENTS: frozenset[str] = frozenset({"SessionEnd", "PreCompact", "SessionStart", "Stop"})
_TIMEOUTS = {"SessionEnd": 60, "PreCompact": 120}
_STATUS = "Focusyn: sincronizando memorias al corpus…"
# Substring que identifica NUESTROS handlers (Claude Code no tiene campo de autoría). Caza tanto el
# comando nuevo (`focusyn memory sync --quiet`) como el legacy (`… focusyn memory sync`).
_MARKER = "memory sync"


@dataclass
class InstallResult:
    """Qué cambiaría/cambió: eventos tocados, handlers removidos, ruta del binario, backup."""

    events: list[str]
    removed_legacy: int
    binary: str
    settings_path: Path
    backup_path: Path | None
    dry_run: bool


def _claude_dir() -> Path:
    """El dir de config de Claude Code: ``$CLAUDE_CONFIG_DIR`` o ``~/.claude``."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def settings_path() -> Path:
    return _claude_dir() / "settings.json"


def _log_path() -> Path:
    return _claude_dir() / "focusyn-memory-sync.log"


def resolve_binary() -> str:
    """La ruta ABSOLUTA del shim estable ``focusyn``. Eleva ``FileNotFoundError`` si no aparece."""
    found = shutil.which("focusyn")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "focusyn"
    if fallback.is_file():
        return str(fallback)
    raise FileNotFoundError(
        "No encuentro el binario `focusyn` en el PATH ni en ~/.local/bin. "
        "¿Instalaste el CLI con `uv tool install`? (el hook necesita su ruta absoluta)."
    )


def build_command(binary: str, event: str) -> str:
    """El comando del hook: fecha + `focusyn memory sync --quiet` + log. SIN la key adentro.

    Todo lo interpolado va por ``shlex.quote``: esto se escribe en ``settings.json`` y Claude Code
    lo ejecuta **en un shell**. Sin quoting, un ``event`` con una comilla (viene de ``--events``)
    cerraba el literal y dejaba escrito un comando arbitrario que corre solo en cada SessionEnd; y
    un ``$HOME`` con espacios rompía el hook en silencio (es ``async`` y loguea a un archivo).
    ``event`` además se valida contra la lista blanca de eventos en :func:`install`.

    El ``|| true`` es load-bearing: ``memory sync`` sale **3** cuando hay conflictos (algo que
    decidir, no un fallo), y un ``PreCompact`` con exit ≠ 0 **bloquearía la compactación**. El
    hook informa por el log; no es el lugar donde se atiende un conflicto.
    """
    log = _log_path()
    return (
        f"{{ date '+[{event} %F %T]'; {shlex.quote(binary)} memory sync --quiet || true; }} "
        f">> {shlex.quote(str(log))} 2>&1"
    )


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} no es un objeto JSON.")
    return data


def _is_ours(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("type") == "command"
        and _MARKER in str(entry.get("command", ""))
    )


def _strip_ours(groups: list[Any]) -> tuple[list[Any], int]:
    """Quita nuestros handlers de los grupos; devuelve (grupos limpios, cuántos se removieron)."""
    removed = 0
    cleaned: list[Any] = []
    for group in groups:
        if not isinstance(group, dict) or "hooks" not in group:
            cleaned.append(group)
            continue
        kept = [h for h in group.get("hooks", []) if not _is_ours(h)]
        removed += len(group.get("hooks", [])) - len(kept)
        if kept:
            cleaned.append({**group, "hooks": kept})
        # un grupo que queda sin hooks se descarta (no dejar cáscaras vacías)
    return cleaned, removed


def _our_entry(binary: str, event: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": build_command(binary, event),
        "timeout": _TIMEOUTS.get(event, 60),
        "statusMessage": _STATUS,
        "async": True,
    }


def _write_atomic(path: Path, data: dict[str, Any]) -> Path | None:
    """Backup .bak-<ts> del previo + escritura atómica (os.replace). Devuelve la ruta del backup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.is_file():
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_suffix(f".json.bak-{ts}")
        shutil.copy2(path, backup)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def install(events: tuple[str, ...] = DEFAULT_EVENTS, *, dry_run: bool = False) -> InstallResult:
    """Instala (o reemplaza) nuestros hooks. Idempotente: migra el legacy con key inline.

    ``events`` se valida contra :data:`VALID_EVENTS`: lo que entra acá termina escrito en un comando
    de shell dentro de ``settings.json``, así que sólo se aceptan nombres de evento conocidos.
    """
    unknown = [e for e in events if e not in VALID_EVENTS]
    if unknown:
        raise ValueError(
            f"Evento(s) desconocido(s): {', '.join(unknown)}. "
            f"Válidos: {', '.join(sorted(VALID_EVENTS))}."
        )
    binary = resolve_binary()
    path = settings_path()
    settings = _load_settings(path)
    hooks_root = (
        settings.setdefault("hooks", {}) if not dry_run else dict(settings.get("hooks", {}))
    )

    total_removed = 0
    # Contar removidos también en eventos que quizá no re-instalamos (limpieza de legacy suelto).
    for event in set(events) | set(hooks_root.keys()):
        groups = hooks_root.get(event, [])
        if not isinstance(groups, list):
            continue
        cleaned, removed = _strip_ours(groups)
        total_removed += removed
        if event in events:
            cleaned.append({"hooks": [_our_entry(binary, event)]})
        if cleaned:
            hooks_root[event] = cleaned
        elif event in hooks_root:
            del hooks_root[event]

    backup: Path | None = None
    if not dry_run:
        settings["hooks"] = hooks_root
        backup = _write_atomic(path, settings)

    return InstallResult(
        events=list(events),
        removed_legacy=total_removed,
        binary=binary,
        settings_path=path,
        backup_path=backup,
        dry_run=dry_run,
    )


def current() -> list[tuple[str, str]]:
    """Nuestros hooks instalados hoy: lista de (evento, comando). Vacía si ninguno."""
    settings = _load_settings(settings_path())
    out: list[tuple[str, str]] = []
    for event, groups in (settings.get("hooks") or {}).items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for h in group.get("hooks", []):
                if _is_ours(h):
                    out.append((event, str(h.get("command", ""))))
    return out


def uninstall(*, dry_run: bool = False) -> InstallResult:
    """Quita TODOS nuestros hooks de todos los eventos. Backup + atómico."""
    path = settings_path()
    settings = _load_settings(path)
    hooks_root = dict(settings.get("hooks", {}))
    total_removed = 0
    for event in list(hooks_root.keys()):
        groups = hooks_root.get(event, [])
        if not isinstance(groups, list):
            continue
        cleaned, removed = _strip_ours(groups)
        total_removed += removed
        if cleaned:
            hooks_root[event] = cleaned
        else:
            del hooks_root[event]

    backup: Path | None = None
    if not dry_run and total_removed:
        settings["hooks"] = hooks_root
        backup = _write_atomic(path, settings)

    return InstallResult(
        events=[],
        removed_legacy=total_removed,
        binary="",
        settings_path=path,
        backup_path=backup,
        dry_run=dry_run,
    )


__all__ = [
    "DEFAULT_EVENTS",
    "VALID_EVENTS",
    "InstallResult",
    "settings_path",
    "resolve_binary",
    "build_command",
    "install",
    "current",
    "uninstall",
]
