"""Comandos de ESCRITURA del vault por HTTP: propose/apply/delete/link (scope propose/apply).

Solapa con las tools del MCP (que los agentes usan desde Claude Code) — su valor acá es para scripts
y CI. Los ``request_id``/``idempotency_key`` se generan solos; ``source_agent`` es metadata.
"""

from __future__ import annotations

import json as _json
import sys
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer

from focusyn_cli import session
from focusyn_cli.http import CliError

write_app = typer.Typer(
    help="Escritura del vault (scope propose/apply): propose/apply/delete/link."
)

_Profile = Annotated[str | None, typer.Option("--profile")]
_Url = Annotated[str | None, typer.Option("--gateway-url", envvar="FOCUSYN_GATEWAY_URL")]
_Key = Annotated[str | None, typer.Option("--api-key", envvar="FOCUSYN_API_KEY")]


def _post(
    path: str, body: dict[str, Any], profile: str | None, url: str | None, key: str | None
) -> Any:
    try:
        with session.client_for(profile, url, key, need_secret=True) as client:
            return client.post(path, json=body)
    except CliError as exc:
        typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exc.code) from exc


@write_app.command("propose")
def propose(
    intent: Annotated[str, typer.Option("--intent", help="Qué hace este cambio (una frase).")],
    file: Annotated[
        Path | None,
        typer.Option(
            "--file", exists=True, dir_okay=False, help="Archivo con el content (o stdin)."
        ),
    ] = None,
    target_doc_id: Annotated[
        str | None, typer.Option("--target-doc-id", help="doc_id a actualizar (sin él = crear).")
    ] = None,
    source_agent: Annotated[str, typer.Option("--source-agent")] = "focusyn-cli",
    as_json: Annotated[bool, typer.Option("--json")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Propone una escritura (crea o actualiza). El content sale de --file o de stdin."""
    content = file.read_text(encoding="utf-8") if file else sys.stdin.read()
    if not content.strip():
        typer.secho(
            "✗ content vacío (pasá --file o algo por stdin).", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=2)
    body: dict[str, Any] = {
        "request_id": uuid.uuid4().hex,
        "intent": intent,
        "content": content,
        "source_agent": source_agent,
    }
    if target_doc_id:
        body["target_doc_id"] = target_doc_id
    out = _post("/v1/write/propose", body, profile, gateway_url, api_key)
    if as_json:
        typer.echo(_json.dumps(out, indent=2, ensure_ascii=False))
        return
    typer.secho(f"✓ propuesta {out.get('proposal_id', '?')}", fg=typer.colors.GREEN)
    typer.echo(f"  aplicá con: focusyn apply {out.get('proposal_id', '')}")


@write_app.command("apply")
def apply(
    proposal_id: Annotated[str, typer.Argument(help="proposal_id devuelto por `propose`.")],
    approval_token: Annotated[str | None, typer.Option("--approval-token")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Aplica una propuesta (commit + push). Idempotente por idempotency_key."""
    body: dict[str, Any] = {"proposal_id": proposal_id, "idempotency_key": uuid.uuid4().hex}
    if approval_token:
        body["approval_token"] = approval_token
    out = _post("/v1/write/apply", body, profile, gateway_url, api_key)
    if as_json:
        typer.echo(_json.dumps(out, indent=2, ensure_ascii=False))
        return
    typer.secho(
        f"✓ aplicada: {out.get('doc_id', '')} (commit {str(out.get('commit_sha', ''))[:8]})",
        fg=typer.colors.GREEN,
    )


@write_app.command("delete")
def delete(
    doc_id: Annotated[str, typer.Argument(help="doc_id a borrar.")],
    reason: Annotated[str, typer.Option("--reason", help="Motivo del borrado (audit).")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Borra un documento (unlink + commit + push). Idempotente (already_deleted en retry)."""
    body = {
        "request_id": uuid.uuid4().hex,
        "doc_id": doc_id,
        "reason": reason,
        "idempotency_key": uuid.uuid4().hex,
    }
    out = _post("/v1/write/delete", body, profile, gateway_url, api_key)
    if as_json:
        typer.echo(_json.dumps(out, indent=2, ensure_ascii=False))
        return
    typer.secho(f"✓ {out.get('status', 'deleted')}: {doc_id}", fg=typer.colors.GREEN)


@write_app.command("link")
def link(
    source_doc_id: Annotated[str, typer.Argument(help="doc_id origen.")],
    target_doc_id: Annotated[str, typer.Argument(help="doc_id destino.")],
    relation: Annotated[str, typer.Option("--relation", help="Tipo de relación (ej. RELATES_TO).")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Enlaza dos documentos (materializa la arista en el grafo)."""
    body = {
        "request_id": uuid.uuid4().hex,
        "source_doc_id": source_doc_id,
        "target_doc_id": target_doc_id,
        "relation": relation,
        "idempotency_key": uuid.uuid4().hex,
    }
    out = _post("/v1/write/link", body, profile, gateway_url, api_key)
    if as_json:
        typer.echo(_json.dumps(out, indent=2, ensure_ascii=False))
        return
    typer.secho(f"✓ {source_doc_id} -[{relation}]-> {target_doc_id}", fg=typer.colors.GREEN)


__all__ = ["write_app"]
