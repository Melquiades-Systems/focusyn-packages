"""Tests del cliente de subida de adjuntos ``focusyn attachment upload``.

Cubre la idempotency key content-addressed (idéntica a la del tool MCP → dedup entre
transportes), el cliente HTTP con ``httpx.MockTransport`` (multipart + header de auth +
parseo), los errores locales (archivo vacío/inexistente) y HTTP, y el comando del CLI vía
``CliRunner`` (falta de key + flujo con cliente fake).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from focusyn_cli.attachment_upload import AttachmentUploadClient, content_idempotency_key
from focusyn_cli.cli import app
from focusyn_cli.contracts import AttachmentUploadOut

runner = CliRunner()


def _mcp_key(content: bytes, vault: str) -> str:
    """Réplica INDEPENDIENTE del formato del tool MCP (``mcp_app._idempotency_key``).

    Si esto se desincroniza, el dedup content-addressed entre MCP y CLI se rompe.
    """
    inner = hashlib.sha256(content).hexdigest()
    return hashlib.sha256("|".join([inner, vault, "attachment"]).encode()).hexdigest()[:36]


def _ok_response() -> dict[str, object]:
    return {
        "file_id": "9f8b1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d",
        "nas_url": "https://nas/9f8b1c2d.png",
        "content_type": "image/png",
        "size_bytes": 4,
        "content_hash": "sha256:abc",
        "markdown_ref": "![alt](/v1/attachment/9f8b1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d)",
        "status": "uploaded",
    }


def _client(
    handler: Callable[[httpx.Request], httpx.Response], *, key: str = "a2a_k"
) -> AttachmentUploadClient:
    return AttachmentUploadClient("http://gw:7415", key, transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# content_idempotency_key — content-addressed, idéntica al tool MCP
# --------------------------------------------------------------------------- #


def test_idempotency_key_coincide_con_el_formato_mcp() -> None:
    content = b"binary-bytes"
    assert content_idempotency_key(content, "acme") == _mcp_key(content, "acme")


def test_idempotency_key_varia_por_contenido_y_por_vault() -> None:
    assert content_idempotency_key(b"a", "acme") != content_idempotency_key(b"b", "acme")
    assert content_idempotency_key(b"a", "acme") != content_idempotency_key(b"a", "wiki")
    # 8 <= len <= 128 (lo exige el endpoint).
    key = content_idempotency_key(b"a", "acme")
    assert 8 <= len(key) <= 128


# --------------------------------------------------------------------------- #
# AttachmentUploadClient.upload (httpx.MockTransport)
# --------------------------------------------------------------------------- #


def test_upload_envia_multipart_y_parsea(tmp_path: Path) -> None:
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["key"] = request.headers.get("X-Agent-Key")
        captured["ctype"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(200, json=_ok_response())

    with _client(handler, key="a2a_secret") as client:
        out = client.upload(f, "acme", doc_id="ACME-PEND-013", alt="una imagen")

    assert captured["path"] == "/v1/write/attachment"
    assert captured["key"] == "a2a_secret"  # header de auth
    ctype = captured["ctype"]
    assert isinstance(ctype, str) and ctype.startswith("multipart/form-data")  # NO json
    body = captured["body"]
    assert isinstance(body, bytes)
    # Campos multipart presentes + el binario streameado (no base64).
    assert b'name="file"; filename="shot.png"' in body
    assert b"\x89PNG" in body
    assert b'name="vault"' in body and b"acme" in body
    assert b'name="doc_id"' in body and b"ACME-PEND-013" in body
    assert b'name="alt"' in body
    assert content_idempotency_key(b"\x89PNG", "acme").encode() in body
    # Respuesta parseada al schema del gateway.
    assert isinstance(out, AttachmentUploadOut)
    assert out.status == "uploaded"
    assert out.markdown_ref.startswith("![alt](/v1/attachment/")


def test_upload_infiere_content_type_del_nombre(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={**_ok_response(), "content_type": "application/pdf"})

    with _client(handler) as client:
        client.upload(f, "acme")
    body = seen["body"]
    assert isinstance(body, bytes)
    assert b"application/pdf" in body  # MIME inferido de la extensión
    # Sin doc_id/alt → esos campos no van.
    assert b'name="doc_id"' not in body
    assert b'name="alt"' not in body


def test_upload_content_type_explicito_gana(tmp_path: Path) -> None:
    f = tmp_path / "blob.bin"
    f.write_bytes(b"data")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json=_ok_response())

    with _client(handler) as client:
        client.upload(f, "acme", content_type="image/webp")
    body = seen["body"]
    assert isinstance(body, bytes)
    assert b"image/webp" in body


def test_upload_archivo_vacio_eleva(tmp_path: Path) -> None:
    f = tmp_path / "empty.png"
    f.write_bytes(b"")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - no llega
        return httpx.Response(200, json=_ok_response())

    with _client(handler) as client, pytest.raises(ValueError, match="vacío"):
        client.upload(f, "acme")


def test_upload_archivo_inexistente_eleva(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - no llega
        return httpx.Response(200, json=_ok_response())

    with _client(handler) as client, pytest.raises(FileNotFoundError):
        client.upload(tmp_path / "no-existe.png", "acme")


def test_upload_eleva_en_error_http(tmp_path: Path) -> None:
    f = tmp_path / "shot.png"
    f.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "AUTH_INSUFFICIENT_SCOPE"}})

    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError) as exc:
        client.upload(f, "acme")
    assert exc.value.response.status_code == 403


# --------------------------------------------------------------------------- #
# CLI (CliRunner)
# --------------------------------------------------------------------------- #


def test_cli_sin_api_key_falla(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOCUSYN_GATEWAY_URL", "http://gw:7415")  # gateway sí, key no
    f = tmp_path / "shot.png"
    f.write_bytes(b"x")
    result = runner.invoke(app, ["attachment", "upload", "--file", str(f), "--vault", "acme"])
    assert result.exit_code != 0
    assert "api-key" in (result.stdout + str(result.output)).lower()


_CLI_UPLOADS: list[tuple[str, str, str | None]] = []


class _FakeClient:
    """Cliente fake que registra la subida (sin red)."""

    def __init__(self, gateway_url: str, api_key: str, **kwargs: object) -> None:
        self.gateway_url = gateway_url
        self.api_key = api_key

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

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
        _CLI_UPLOADS.append((file_path.name, vault, doc_id))
        return AttachmentUploadOut.model_validate(_ok_response())


def test_cli_upload_invoca_cliente_y_muestra_markdown_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _CLI_UPLOADS.clear()
    f = tmp_path / "evidencia.png"
    f.write_bytes(b"\x89PNG")
    monkeypatch.setattr("focusyn_cli.cli.AttachmentUploadClient", _FakeClient)

    result = runner.invoke(
        app,
        [
            "attachment",
            "upload",
            "--file",
            str(f),
            "--vault",
            "acme",
            "--doc-id",
            "ACME-PEND-013",
            "--gateway-url",
            "http://gw:7415",
            "--api-key",
            "a2a_test",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert _CLI_UPLOADS == [("evidencia.png", "acme", "ACME-PEND-013")]
    # El markdown_ref se imprime (lo único que el agente pega en la nota).
    assert "![alt](/v1/attachment/" in result.stdout
