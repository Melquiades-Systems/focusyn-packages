"""Aislamiento del entorno del CLI para TODOS los tests del paquete.

Sin esto, los tests leerían el ``~/.config/focusyn/config.toml`` REAL del desarrollador (o las env
``FOCUSYN_*``) y el resultado dependería de la máquina. La fixture apunta el config a un archivo
inexistente del ``tmp_path`` y borra las env de credencial → cada test parte de cero.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOCUSYN_CONFIG", str(tmp_path / "focusyn-config.toml"))
    for var in (
        "FOCUSYN_GATEWAY_URL",
        "FOCUSYN_API_KEY",
        "FOCUSYN_INGEST_KEY",
        "FOCUSYN_APPLY_KEY",
        "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
