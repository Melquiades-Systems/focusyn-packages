"""Comandos de ADMINISTRACIÓN por HTTP (gated por scopes vault/credential/admin del gateway).

vault (create/list/config/scaffold) · credential (list/create/rotate/delete/set-role/assign) ·
org create · tenant provision · usage (summary/list). El gateway autoriza: un usuario sin el scope
recibe 403 limpio. Los comandos de identidad de USUARIOS (crear/desactivar) NO están acá — quedan en
el operador ``focusynctl`` (Postgres), que es lo que el IdP absorberá (MEL-DEC-193).
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from typing import Annotated, Any

import typer

from focusyn_cli import session
from focusyn_cli.http import CliError, GatewayClient
from focusyn_cli.scaffold import scaffold_vault

vault_app = typer.Typer(help="Vaults por HTTP (scope vault/read): create/list/config/scaffold.")
credential_app = typer.Typer(help="Credenciales cifradas por HTTP (scope credential).")
org_app = typer.Typer(help="Organizaciones (scope admin).")
tenant_app = typer.Typer(help="Aprovisionamiento de tenants (scope admin).")
usage_app = typer.Typer(help="Consumo de AI (scope admin).")
agent_app = typer.Typer(help="Tus agentes (API keys de máquina): create/list/rotate/disable.")

_Profile = Annotated[str | None, typer.Option("--profile")]
_Url = Annotated[str | None, typer.Option("--gateway-url", envvar="FOCUSYN_GATEWAY_URL")]
_Key = Annotated[str | None, typer.Option("--api-key", envvar="FOCUSYN_API_KEY")]


def _run(
    fn: Callable[[GatewayClient], Any],
    profile: str | None,
    url: str | None,
    key: str | None,
) -> Any:
    """Corre ``fn(client)`` con manejo uniforme de CliError → exit."""
    try:
        with session.client_for(profile, url, key, need_secret=True) as client:
            return fn(client)
    except CliError as exc:
        typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exc.code) from exc


def _dump(data: Any) -> None:
    typer.echo(_json.dumps(data, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- vault


@vault_app.command("create")
def vault_create(
    name: Annotated[str, typer.Argument(help="Nombre del vault.")],
    vault_type: Annotated[
        str, typer.Option("--type", help="wiki|org|código|freeform|dossier|fuente…")
    ],
    mode: Annotated[
        str, typer.Option("--mode", help="existing (clona un repo) | new (crea el repo).")
    ] = "existing",
    org: Annotated[str | None, typer.Option("--org")] = None,
    prefix: Annotated[
        str | None, typer.Option("--prefix", help="Prefijo de IDs (ej. MEL).")
    ] = None,
    repo_full_name: Annotated[
        str | None, typer.Option("--repo", help="owner/repo (mode=existing).")
    ] = None,
    credential_label: Annotated[str | None, typer.Option("--credential-label")] = None,
    private: Annotated[bool, typer.Option("--private/--public")] = True,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Registra + clona/crea un vault tipado (scope vault). ⚠️ Efectos reales (fila + repo)."""
    body: dict[str, Any] = {
        "name": name,
        "vault_type": vault_type,
        "mode": mode,
        "private": private,
    }
    for k, v in {
        "org": org,
        "prefix": prefix,
        "repo_full_name": repo_full_name,
        "credential_label": credential_label,
    }.items():
        if v is not None:
            body[k] = v
    out = _run(lambda c: c.post("/v1/vaults", json=body), profile, gateway_url, api_key)
    typer.secho(f"✓ vault '{name}' ({out.get('status', 'creado')})", fg=typer.colors.GREEN)


@vault_app.command("list")
def vault_list(
    counts: Annotated[bool, typer.Option("--counts", help="Incluye conteos de docs.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Lista los vaults registrados (scope read)."""
    out = _run(lambda c: c.get("/v1/vaults", counts=counts), profile, gateway_url, api_key)
    if as_json:
        _dump(out)
        return
    vaults = out.get("vaults", out) if isinstance(out, dict) else out
    for v in vaults if isinstance(vaults, list) else []:
        typer.echo(
            f"{v.get('name', '?'):<18} {v.get('vault_type', ''):<10} {v.get('status', ''):<12} "
            f"{'privado' if v.get('visibility') == 'private' else v.get('visibility', '')}"
        )


@vault_app.command("config")
def vault_config(
    name: Annotated[str, typer.Argument(help="Nombre del vault.")],
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Muestra la config por-vault (scope read)."""
    out = _run(lambda c: c.get(f"/v1/vaults/{name}/config"), profile, gateway_url, api_key)
    _dump(out)


@vault_app.command("scaffold")
def vault_scaffold(
    name: Annotated[str, typer.Argument(help="Nombre del vault dossier a scaffoldear.")],
    template: Annotated[str, typer.Option("--template")] = "dossier",
    force: Annotated[bool, typer.Option("--force")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Instancia el template pack en un dossier (scope vault/apply); 409 si ya hay contenido."""
    out = _run(
        lambda c: scaffold_vault(c, name, template=template, force=force),
        profile,
        gateway_url,
        api_key,
    )
    typer.secho(
        f"✓ scaffold '{out['template']}' en '{out['vault']}': "
        f"{out['files_written']} archivos (commit {out['commit_sha'][:8]})",
        fg=typer.colors.GREEN,
    )
    for did in out.get("doc_ids", []):
        typer.echo(f"  {did}")


# --------------------------------------------------------------------------- credential


@credential_app.command("list")
def credential_list(
    owner: Annotated[str | None, typer.Option("--owner", help="Admin: filtra por owner.")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Lista credenciales (incluye las de empresa; scope credential)."""
    out = _run(lambda c: c.get("/v1/credentials", owner=owner), profile, gateway_url, api_key)
    if as_json:
        _dump(out)
        return
    creds = out.get("credentials", out) if isinstance(out, dict) else out
    for cr in creds if isinstance(creds, list) else []:
        typer.echo(
            f"{cr.get('label', '?'):<20} {cr.get('kind', ''):<14} {cr.get('provider') or '-':<10} "
            f"{cr.get('llm_role') or ''}"
        )


@credential_app.command("create")
def credential_create(
    label: Annotated[str, typer.Option("--label")],
    kind: Annotated[str, typer.Option("--kind", help="llm_api_key | git_pat …")],
    secret: Annotated[
        str,
        typer.Option(
            "--secret",
            prompt=True,
            hide_input=True,
            help="Sin el flag se pide OCULTO (recomendado): inline queda en el historial.",
        ),
    ],
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    scope: Annotated[str | None, typer.Option("--scope", help="company (admin) | user")] = None,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Crea una credencial cifrada (scope credential; company exige admin)."""
    body: dict[str, Any] = {"label": label, "kind": kind, "secret": secret}
    if provider:
        body["provider"] = provider
    if scope:
        body["scope"] = scope
    out = _run(lambda c: c.post("/v1/credentials", json=body), profile, gateway_url, api_key)
    typer.secho(f"✓ credencial '{label}' creada (id {out.get('id', '?')})", fg=typer.colors.GREEN)


@credential_app.command("rotate")
def credential_rotate(
    credential_id: Annotated[str, typer.Argument()],
    secret: Annotated[
        str,
        typer.Option(
            "--secret",
            prompt=True,
            hide_input=True,
            help="Sin el flag se pide OCULTO (recomendado): inline queda en el historial.",
        ),
    ],
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Rota el secreto de una credencial (scope credential)."""
    _run(
        lambda c: c.post(f"/v1/credentials/{credential_id}/rotate", json={"secret": secret}),
        profile,
        gateway_url,
        api_key,
    )
    typer.secho(f"✓ credencial {credential_id} rotada", fg=typer.colors.GREEN)


@credential_app.command("delete")
def credential_delete(
    credential_id: Annotated[str, typer.Argument()],
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Borra una credencial (scope credential)."""
    _run(lambda c: c.delete(f"/v1/credentials/{credential_id}"), profile, gateway_url, api_key)
    typer.secho(f"✓ credencial {credential_id} borrada", fg=typer.colors.GREEN)


@credential_app.command("set-role")
def credential_set_role(
    credential_id: Annotated[str, typer.Argument()],
    role: Annotated[str, typer.Option("--role", help="primary | secondary | none")],
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Fija el rol LLM de una credencial (scope credential)."""
    _run(
        lambda c: c.put(f"/v1/credentials/{credential_id}/llm-role", json={"llm_role": role}),
        profile,
        gateway_url,
        api_key,
    )
    typer.secho(f"✓ rol de {credential_id} → {role}", fg=typer.colors.GREEN)


@credential_app.command("assign-vault")
def credential_assign_vault(
    vault: Annotated[str, typer.Argument(help="Nombre del vault.")],
    label: Annotated[str, typer.Option("--credential-label")],
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Asigna una credencial Git a un vault (scope vault)."""
    _run(
        lambda c: c.put(f"/v1/vaults/{vault}/credential", json={"credential_label": label}),
        profile,
        gateway_url,
        api_key,
    )
    typer.secho(f"✓ vault '{vault}' → credencial '{label}'", fg=typer.colors.GREEN)


# --------------------------------------------------------------------------- org / tenant


@org_app.command("create")
def org_create(
    tax_id: Annotated[str, typer.Argument(help="Tax id (9 dígitos).")],
    name: Annotated[str | None, typer.Option("--name")] = None,
    no_provision: Annotated[
        bool, typer.Option("--no-provision", help="No aprovisiona la BD.")
    ] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Crea una organización (scope admin). Por defecto aprovisiona su tenant."""
    body: dict[str, Any] = {"tax_id": tax_id, "provision": not no_provision}
    if name:
        body["name"] = name
    out = _run(lambda c: c.post("/v1/orgs", json=body), profile, gateway_url, api_key)
    typer.secho(f"✓ org {tax_id} creada", fg=typer.colors.GREEN)
    _dump(out)


@tenant_app.command("provision")
def tenant_provision(
    tax_id: Annotated[str, typer.Argument(help="Tax id del tenant a aprovisionar.")],
    org_id: Annotated[str | None, typer.Option("--org-id")] = None,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Aprovisiona la BD + grafo AGE de un tenant (scope admin)."""
    body: dict[str, Any] = {"tax_id": tax_id}
    if org_id:
        body["org_id"] = org_id
    out = _run(lambda c: c.post("/v1/admin/tenants", json=body), profile, gateway_url, api_key)
    typer.secho(f"✓ tenant {tax_id} aprovisionado", fg=typer.colors.GREEN)
    if out.get("bootstrap_sql"):
        typer.secho("  ⚠ faltan pasos de superusuario (bootstrap_sql):", fg=typer.colors.YELLOW)
        typer.echo(out["bootstrap_sql"])


# --------------------------------------------------------------------------- usage


@usage_app.command("summary")
def usage_summary(
    by: Annotated[str, typer.Option("--by", help="agent | model | service | kind | day")] = "model",
    since: Annotated[str | None, typer.Option("--since", help="ISO-8601.")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Resumen de consumo de AI agrupado (scope admin)."""
    out = _run(
        lambda c: c.get("/v1/usage/summary", by=by, since=since, until=until),
        profile,
        gateway_url,
        api_key,
    )
    _dump(out)


@usage_app.command("list")
def usage_list(
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 50,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Lista los registros de consumo de AI (scope admin)."""
    out = _run(
        lambda c: c.get("/v1/usage", since=since, until=until, limit=limit),
        profile,
        gateway_url,
        api_key,
    )
    _dump(out)


# --------------------------------------------------------------------------- agent


def _invite_blob(url: str, key: str) -> str:
    """`focusyn-invite:<base64({url,key})>` — para la otra máquina: `focusyn init --invite`."""
    import base64
    import json as _j

    raw = base64.urlsafe_b64encode(_j.dumps({"url": url.rstrip("/"), "key": key}).encode()).decode()
    return "focusyn-invite:" + raw


@agent_app.command("create")
def agent_create(
    name: Annotated[str, typer.Argument(help="agent_id único ([a-z0-9._-], sin ':').")],
    scopes: Annotated[
        str, typer.Option("--scopes", help="Subconjunto de TUS scopes, separados por coma.")
    ],
    rate_limit: Annotated[
        int, typer.Option("--rate-limit", help="Peticiones/min (0 = ilimitado).")
    ] = 60,
    invite: Annotated[
        bool,
        typer.Option("--invite", help="Imprime un blob focusyn-invite: para la máquina destino."),
    ] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Emite una API key de máquina acotada a un subconjunto de tus scopes (se ve UNA sola vez)."""
    cred = session.credential_for(profile, gateway_url, api_key, need_secret=True)
    body: dict[str, Any] = {
        "name": name,
        "scopes": [s.strip() for s in scopes.split(",") if s.strip()],
        "rate_limit_rpm": rate_limit,
    }
    out = _run(lambda c: c.post("/v1/agents", json=body), profile, gateway_url, api_key)
    typer.secho(
        f"✓ agente '{out['agent_id']}' creado (scopes: {', '.join(out['scopes'])})",
        fg=typer.colors.GREEN,
    )
    if invite:
        typer.echo(_invite_blob(cred.gateway_url, out["api_key"]))
    else:
        typer.secho("API KEY (se muestra UNA sola vez, guardala):", fg=typer.colors.YELLOW)
        typer.echo(out["api_key"])


@agent_app.command("list")
def agent_list(
    owner: Annotated[str | None, typer.Option("--owner", help="Admin: filtra por owner.")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Lista tus agentes (un admin ve todos, o filtra con --owner)."""
    out = _run(lambda c: c.get("/v1/agents", owner=owner), profile, gateway_url, api_key)
    if as_json:
        _dump(out)
        return
    for a in out.get("agents", []) if isinstance(out, dict) else []:
        flag = "" if a.get("active") else " (inactivo)"
        typer.echo(
            f"{a.get('agent_id', '?'):<24} {','.join(a.get('scopes', [])):<30} "
            f"{a.get('key_prefix', ''):<14}{flag}"
        )


@agent_app.command("rotate")
def agent_rotate(
    agent_id: Annotated[str, typer.Argument()],
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Rota la key de un agente tuyo (la nueva se muestra UNA vez)."""
    out = _run(lambda c: c.post(f"/v1/agents/{agent_id}/rotate"), profile, gateway_url, api_key)
    typer.secho(f"✓ key de '{agent_id}' rotada (la vieja deja de valer):", fg=typer.colors.GREEN)
    typer.echo(out["api_key"])


@agent_app.command("disable")
def agent_disable(
    agent_id: Annotated[str, typer.Argument()],
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Desactiva un agente tuyo (sus requests pasan a AUTH_INVALID_KEY)."""
    _run(lambda c: c.post(f"/v1/agents/{agent_id}/disable"), profile, gateway_url, api_key)
    typer.secho(f"✓ agente '{agent_id}' desactivado", fg=typer.colors.GREEN)


__all__ = ["vault_app", "credential_app", "org_app", "tenant_app", "usage_app", "agent_app"]
