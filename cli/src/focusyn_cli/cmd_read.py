"""Comandos de LECTURA del vault por HTTP (scope ``read``): read/list/search/ask/map."""

from __future__ import annotations

import json as _json
import uuid
from typing import Annotated, Any

import typer

from focusyn_cli import session
from focusyn_cli.http import CliError

read_app = typer.Typer(help="Lectura del vault (scope read): read/list/search/ask/map.")

# Opciones de perfil/credencial compartidas por todos los comandos de lectura.
_Profile = Annotated[str | None, typer.Option("--profile", help="Perfil del config a usar.")]
_Url = Annotated[str | None, typer.Option("--gateway-url", envvar="FOCUSYN_GATEWAY_URL")]
_Key = Annotated[str | None, typer.Option("--api-key", envvar="FOCUSYN_API_KEY")]


@read_app.command("read")
def read(
    ref: Annotated[str, typer.Argument(help="doc_id (ej. MEL-DEC-201) o path del vault.")],
    graph: Annotated[
        bool, typer.Option("--graph", help="Incluye entidades + related_docs.")
    ] = False,
    enrich: Annotated[
        bool, typer.Option("--enrich", help="Añade la síntesis del grafo (LLM).")
    ] = False,
    vault: Annotated[
        str | None, typer.Option("--vault", help="Vault del path (si el ref va sin prefijo).")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Vuelca el payload completo.")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Lee un documento por doc_id (MEL-DEC-201) o por path (<vault>/<ruta> o --vault + ruta)."""
    # Un ID canónico tiene forma XXX-YYY-NNN; si no, es un path (el gateway pide vault + path).
    is_doc_id = ref[:1].isalpha() and ref.count("-") >= 2 and "/" not in ref
    params: dict[str, Any] = {"graph": graph, "enrich": enrich}
    if is_doc_id:
        params["doc_id"] = ref
    elif vault:
        params["vault"], params["path"] = vault, ref
    elif "/" in ref:
        params["vault"], params["path"] = ref.split("/", 1)  # <vault>/<ruta relativa>
    else:
        typer.secho(
            "✗ para leer por path pasá <vault>/<ruta> o --vault <v> <ruta>.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        with session.client_for(profile, gateway_url, api_key, need_secret=True) as client:
            doc = client.get("/v1/read", **params)
    except CliError as exc:
        typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exc.code) from exc
    if as_json:
        typer.echo(_json.dumps(doc, indent=2, ensure_ascii=False))
        return
    typer.echo(doc.get("raw_content") or _json.dumps(doc, indent=2, ensure_ascii=False))


@read_app.command("list")
def list_docs(
    vault: Annotated[str, typer.Option("--vault", help="Vault a listar (obligatorio).")],
    path_prefix: Annotated[str | None, typer.Option("--path-prefix")] = None,
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 50,
    offset: Annotated[int, typer.Option("--offset")] = 0,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Lista documentos del índice (Postgres) de un vault."""
    try:
        with session.client_for(profile, gateway_url, api_key, need_secret=True) as client:
            out = client.get(
                "/v1/list",
                vault=vault,
                path_prefix=path_prefix,
                kind=kind,
                limit=limit,
                offset=offset,
            )
    except CliError as exc:
        typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exc.code) from exc
    if as_json:
        typer.echo(_json.dumps(out, indent=2, ensure_ascii=False))
        return
    docs = out.get("items", out) if isinstance(out, dict) else out
    for d in docs if isinstance(docs, list) else []:
        typer.echo(
            f"{d.get('doc_id', '?'):<24} {d.get('kind', ''):<12} "
            f"{d.get('path', d.get('title', ''))}"
        )
    if isinstance(out, dict) and out.get("total") is not None:
        typer.secho(f"— {len(docs)} de {out['total']}", fg=typer.colors.BLUE)


@read_app.command("search")
def search(
    query: Annotated[str, typer.Argument(help="Consulta (semántica + léxica).")],
    vault: Annotated[str | None, typer.Option("--vault", help="Acota a un vault.")] = None,
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    top_k: Annotated[int, typer.Option("--top-k")] = 10,
    no_rerank: Annotated[
        bool, typer.Option("--no-rerank", help="Sin reranker (sólo vectorial).")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Busca notas/ADR/wiki (NO código — para eso está el MCP find_code)."""
    body = {"query": query, "vault": vault, "kind": kind, "top_k": top_k, "rerank": not no_rerank}
    try:
        with session.client_for(profile, gateway_url, api_key, need_secret=True) as client:
            out = client.post("/v1/search", json={k: v for k, v in body.items() if v is not None})
    except CliError as exc:
        typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exc.code) from exc
    if as_json:
        typer.echo(_json.dumps(out, indent=2, ensure_ascii=False))
        return
    for h in (out.get("results") or []) if isinstance(out, dict) else []:
        score = h.get("score")
        loc = h.get("path") or h.get("doc_id") or h.get("location", "")
        typer.echo(
            f"{score:.3f}  {h.get('title', loc)}" if isinstance(score, float) else f"  {loc}"
        )


@read_app.command("ask")
def ask(
    question: Annotated[str, typer.Argument(help="Pregunta en lenguaje natural.")],
    vault: Annotated[str | None, typer.Option("--vault", help="Acota el scope a un vault.")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Pregunta al GraphRAG (respuesta sintetizada + fuentes)."""
    body: dict[str, Any] = {"request_id": uuid.uuid4().hex, "question": question}
    if vault:
        body["scope"] = vault
    try:
        with session.client_for(profile, gateway_url, api_key, need_secret=True) as client:
            out = client.post("/v1/graphrag/query", json=body)
    except CliError as exc:
        typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exc.code) from exc
    if as_json:
        typer.echo(_json.dumps(out, indent=2, ensure_ascii=False))
        return
    typer.echo(out.get("answer") or _json.dumps(out, indent=2, ensure_ascii=False))
    sources = out.get("sources") or out.get("citations") or []
    if sources:
        typer.secho("\nFuentes:", bold=True)
        for s in sources:
            typer.echo(f"  - {s.get('doc_id') or s.get('path') or s}")


@read_app.command("map")
def map_vault(
    vault: Annotated[str | None, typer.Option("--vault", help="Vault (vacío = los tres).")] = None,
    path: Annotated[str | None, typer.Option("--path")] = None,
    depth: Annotated[int, typer.Option("--depth")] = 1,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Explora la estructura del vault un nivel a la vez."""
    try:
        with session.client_for(profile, gateway_url, api_key, need_secret=True) as client:
            out = client.get("/v1/map", vault=vault, path=path, depth=depth)
    except CliError as exc:
        typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exc.code) from exc
    typer.echo(_json.dumps(out, indent=2, ensure_ascii=False))


__all__ = ["read_app"]
