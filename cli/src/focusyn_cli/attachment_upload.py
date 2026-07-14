"""Cliente de subida de adjuntos → gateway (streaming multipart, sin base64).

``focusyn attachment upload`` corre en la máquina del agente, lee un binario LOCAL y lo
**stremea multipart** a ``POST /v1/write/attachment`` — el MISMO endpoint que usan el
frontend y el tool MCP ``add_attachment``. A diferencia de ``add_attachment(content_base64=…)``
(que mete los bytes en el tool-call JSON — frágil y capado a ~8 MB inline), el binario nunca
entra al contexto del modelo: sube hasta ``NAS_MAX_FILE_MB`` (50 MB) por streaming.

Es un artefacto **cliente** (como ``focusyn memory sync``): habla HTTP con el gateway por su
URL configurada (``FOCUSYN_GATEWAY_URL``) → funciona igual con el gateway local o remoto.
Autentica con un agente scope ``apply`` (header ``X-Agent-Key``; **nunca** se loguea). La
respuesta trae ``markdown_ref`` (``![alt](/v1/attachment/{file_id})``) — lo único que se pega
en la nota (con ``edit_note``/``append_note``).

La idempotencia es **content-addressed** con el MISMO formato que el tool MCP
(``sha256(sha256(content)|vault|"attachment")[:36]``): re-subir el mismo binario al mismo
vault devuelve el existente (``status='already_uploaded'``) sin duplicar, sin importar el
transporte (MCP base64 o este CLI).

Ver el vault: ADR MEL-DEC-133 + instructivo MEL-HTTP-062 (attachments NAS).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

import httpx

# ``content_idempotency_key`` es la frontera de contrato (contracts.py) que este CLI comparte con el
# tool MCP ``add_attachment`` → el mismo binario deduplica igual sin importar el transporte. Se
# re-exporta acá (los tests lo importan de este módulo).
from focusyn_cli.contracts import AttachmentUploadOut, content_idempotency_key

# Header de autenticación del gateway (mismo que el resto de la API por key).
_AGENT_KEY_HEADER = "X-Agent-Key"


class AttachmentUploadClient:
    """Cliente HTTP síncrono de ``POST /v1/write/attachment`` (streaming multipart).

    El ``transport`` se inyecta en tests (``httpx.MockTransport``); en producción es
    ``None`` (transporte por defecto). Usable como context manager (``with``). El
    ``api_key`` (header ``X-Agent-Key``) **nunca** se loguea. NO fija ``Content-Type``:
    httpx pone el boundary multipart él solo cuando se usa ``files=``.
    """

    def __init__(
        self,
        gateway_url: str,
        api_key: str,
        *,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=gateway_url.rstrip("/"),
            timeout=timeout,
            headers={_AGENT_KEY_HEADER: api_key},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AttachmentUploadClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def upload(
        self,
        file_path: Path,
        vault: str,
        *,
        doc_id: str | None = None,
        alt: str | None = None,
        content_type: str | None = None,
        filename: str | None = None,
    ) -> AttachmentUploadOut:
        """Lee el binario LOCAL y lo sube por multipart; devuelve la referencia del gateway.

        Eleva ``FileNotFoundError``/``OSError`` si no puede leer el archivo, ``ValueError``
        si está vacío, y ``httpx.HTTPStatusError`` ante un status de error del gateway.
        """
        if not file_path.is_file():
            raise FileNotFoundError(f"No es un archivo: {file_path}")
        content = file_path.read_bytes()
        if not content:
            raise ValueError(f"El archivo está vacío: {file_path}")

        name = filename or file_path.name
        ctype = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        return self.upload_bytes(content, name, vault, doc_id=doc_id, alt=alt, content_type=ctype)

    def upload_bytes(
        self,
        content: bytes,
        filename: str,
        vault: str,
        *,
        doc_id: str | None = None,
        alt: str | None = None,
        content_type: str | None = None,
        source_path: str | None = None,
    ) -> AttachmentUploadOut:
        """Sube un binario **que ya está en memoria** (no toca el disco).

        Lo usa el orquestador de fuentes externas (F4): un archivo que baja de Microsoft Graph se
        sube directo al gateway, sin pasar por un archivo temporal. ``source_path`` es su ruta REAL
        en el drive de origen — hace que el documento espeje el árbol de la carpeta en vez de
        aplanarse bajo ``_attachments/``.
        """
        if not content:
            raise ValueError(f"El archivo está vacío: {filename}")
        ctype = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        data: dict[str, str] = {
            "vault": vault,
            "idempotency_key": content_idempotency_key(content, vault),
        }
        if doc_id:
            data["doc_id"] = doc_id
        if alt:
            data["alt"] = alt
        if source_path:
            data["source_path"] = source_path

        resp = self._client.post(
            "/v1/write/attachment",
            files={"file": (filename, content, ctype)},
            data=data,
        )
        resp.raise_for_status()
        return AttachmentUploadOut.model_validate(resp.json())


__all__ = ["AttachmentUploadClient", "content_idempotency_key"]
