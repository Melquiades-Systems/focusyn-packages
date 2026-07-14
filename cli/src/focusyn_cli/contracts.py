"""Contrato cliente↔gateway — COPIA deliberada de ~4 símbolos de ``focusyn.schemas.*``.

El cliente no puede importar ``focusyn`` (arrastraría fastapi/sqlalchemy/pgvector/…) ni depender de
un tercer paquete compartido (``[tool.uv.sources]`` no viaja en la metadata del wheel → rompería el
``uv tool install`` de una línea). Por eso el acoplamiento REAL —cuatro símbolos: una constante, dos
modelos de respuesta y una función de idempotencia— se copia acá.

El precio de la copia se paga en ``tests/contract/test_client_contracts.py`` (corre en
``make check``, donde AMBOS paquetes son importables por el workspace): asserta que
``content_idempotency_key`` es byte-idéntica a la del gateway, que el payload que el cliente
construye valida contra el ``IngestSyncIn`` REAL, y que los campos de estas respuestas ⊆ los del
schema del gateway. Es el mismo contrato que el ``frontend/`` ya tiene (``openapi.json`` → ``.ts``).

⚠️ Estos símbolos son la frontera. NO cambiar ``content_idempotency_key`` sin migrar (rompería el
dedup de attachments); no divergir de ``focusyn/schemas/{ingest,attachment}.py`` sin correr
``make check`` (el test de contrato lo caza).
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# ingest — POST /v1/ingest/sync  (espejo de focusyn.schemas.ingest)
# --------------------------------------------------------------------------- #

# Nombre por defecto de la partición corpus (sembrada por la migración 0007 del gateway).
CORPUS_PARTITION_DEFAULT = "claude-memory"


class IngestSyncOut(BaseModel):
    """Respuesta de ``POST /v1/ingest/sync``: conteos del reconcile."""

    partition: str
    created: int
    updated: int
    unchanged: int
    deleted: int
    errors: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# attachment — POST /v1/write/attachment  (espejo de focusyn.schemas.attachment)
# --------------------------------------------------------------------------- #


def content_idempotency_key(content: bytes, vault: str) -> str:
    """Idempotency key content-addressed para ``POST /v1/write/attachment``.

    Formato: ``sha256("|".join([sha256(content).hex, vault, "attachment"]))[:36]``. **Fuente
    única** que este CLI comparte con el tool MCP ``add_attachment`` → el mismo binario deduplica
    igual sin importar el transporte. NO cambiar el formato sin migrar: rompería el dedup de los
    adjuntos ya subidos (idempotencia del endpoint por ``(vault, key)``).
    """
    inner = hashlib.sha256(content).hexdigest()
    return hashlib.sha256("|".join([inner, vault, "attachment"]).encode()).hexdigest()[:36]


class AttachmentUploadOut(BaseModel):
    """Respuesta de ``POST /v1/write/attachment``."""

    file_id: str
    nas_url: str
    content_type: str
    size_bytes: int
    content_hash: str
    markdown_ref: str = Field(description="![alt](/v1/attachment/{file_id}) listo para pegar")
    status: str = Field(description="uploaded | already_uploaded")


__all__ = [
    "CORPUS_PARTITION_DEFAULT",
    "IngestSyncOut",
    "AttachmentUploadOut",
    "content_idempotency_key",
]
