"""focusyn-cli — el CLI cliente de focusyn.

Paquete DELGADO (httpx + typer + pydantic + tomli-w), hermano Python del ``frontend/``: consume la
API del gateway por HTTP y **nunca** importa el paquete del gateway (``focusyn``). El corte lo
garantiza el guard ``make cli-check`` (el cierre de dependencias del wheel no contiene fastapi,
sqlalchemy, pgvector, pygit2, grpc, cryptography, mcp ni tree-sitter).

La versión es INDEPENDIENTE del gateway: el cliente cambia poco, el gateway mucho. El contrato entre
ambos no es la versión, es ``openapi.json`` + los tests de ``tests/contract/``.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
