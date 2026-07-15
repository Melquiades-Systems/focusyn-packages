"""Errores compartidos del CLI.

``CliError`` vive acá (y no en ``http.py``) para que los módulos que ``http.py`` importa —como
``config.py``, que valida la URL del gateway— puedan elevarlo sin un import circular. ``http.py``
lo re-exporta, así el resto del código sigue importándolo de donde siempre.
"""

from __future__ import annotations


class CliError(Exception):
    """Error de cara al usuario: mensaje limpio + exit code. Lo captura el entrypoint del CLI."""

    def __init__(self, message: str, *, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


__all__ = ["CliError"]
