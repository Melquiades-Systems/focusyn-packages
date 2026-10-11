"""Comandos de ADMINISTRACIÓN por HTTP (gated por scopes vault/credential/admin del gateway).

vault (create/list/config/scaffold) · credential (list/create/rotate/set-expiry/delete/set-role/
assign) ·
org create · tenant provision · usage (summary/list). El gateway autoriza: un usuario sin el scope
recibe 403 limpio. Los comandos de identidad de USUARIOS (crear/desactivar) NO están acá — quedan en
el operador ``focusynctl`` (Postgres), que es lo que el IdP absorberá (MEL-DEC-193).
"""

from __future__ import annotations

import json as _json
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Any

import typer

from focusyn_cli import session
from focusyn_cli.http import CliError, GatewayClient
from focusyn_cli.scaffold import scaffold_vault

vault_app = typer.Typer(help="Vaults por HTTP (scope vault/read): create/list/config/scaffold.")
config_app = typer.Typer(help="Perfil GLOBAL del pipeline del tenant (scope read/admin).")
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


def _coerce(valor: str) -> Any:
    """Convierte el valor de la línea de comandos al tipo que el gateway espera.

    Una shell entrega todo como texto, y el gateway valida con pydantic: mandar ``"true"`` donde va
    un bool da 422. El orden importa — ``true``/``false`` antes que el número, y el número antes
    que el texto—, y ``null`` existe para poder distinguir "poné null" de la cadena "null".

    Lo que no encaje en ninguno viaja como texto, y una lista se escribe con ``--json``: adivinar
    listas desde comas rompería cualquier valor que legítimamente lleve una.
    """
    bajo = valor.strip().lower()
    if bajo in ("true", "false"):
        return bajo == "true"
    if bajo == "null":
        return None
    try:
        return int(valor)
    except ValueError:
        pass
    try:
        return float(valor)
    except ValueError:
        return valor


def _set_path(raiz: dict[str, Any], clave: str, valor: Any) -> None:
    """Escribe ``clave`` (dotted, p. ej. ``groundedness.sample_rate``) dentro de ``raiz``."""
    partes = clave.split(".")
    nodo = raiz
    for parte in partes[:-1]:
        hijo = nodo.get(parte)
        if not isinstance(hijo, dict):
            hijo = {}
            nodo[parte] = hijo
        nodo = hijo
    nodo[partes[-1]] = valor


def _unset_path(raiz: dict[str, Any], clave: str) -> None:
    """Borra ``clave`` y poda los submodelos que queden vacíos.

    Podar no es cosmético: un ``{"groundedness": {}}`` es no-op para el gateway, pero deja el
    override persistido diciendo que hay algo configurado ahí. El panel lo mostraría como tocado.
    """
    partes = clave.split(".")
    cadena: list[tuple[dict[str, Any], str]] = []
    nodo = raiz
    for parte in partes[:-1]:
        hijo = nodo.get(parte)
        if not isinstance(hijo, dict):
            return
        cadena.append((nodo, parte))
        nodo = hijo
    nodo.pop(partes[-1], None)
    for padre, parte in reversed(cadena):
        if not padre.get(parte):
            padre.pop(parte, None)


def _editar_override(
    actual: dict[str, Any] | None, clave: str, valor: str | None, *, unset: bool, como_json: bool
) -> dict[str, Any]:
    """El override completo que hay que mandar: se LEE, se muta y se manda entero.

    ⚠️ El ``PATCH`` de config **reemplaza el override entero**, no lo mergea. Mandar sólo la clave
    tocada borraría en silencio todo lo demás que el vault tuviera configurado — que es el modo de
    falla que este rodeo (GET → mutar → PATCH) existe para evitar.
    """
    override: dict[str, Any] = dict(actual or {})
    if unset:
        _unset_path(override, clave)
        return override
    if valor is None:
        raise CliError("Falta el VALOR (o usá --unset para quitar la clave).", code=2)
    _set_path(override, clave, _json.loads(valor) if como_json else _coerce(valor))
    return override


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


@vault_app.command("config-set")
def vault_config_set(
    name: Annotated[str, typer.Argument(help="Nombre del vault.")],
    clave: Annotated[
        str,
        typer.Argument(
            help="Clave en notación de punto, p. ej. `groundedness.sample_rate` o "
            "`retrieval.top_k_vector`."
        ),
    ],
    valor: Annotated[str | None, typer.Argument(help="Valor. Se omite con --unset.")] = None,
    unset: Annotated[
        bool, typer.Option("--unset", help="Quita la clave (vuelve al default heredado).")
    ] = False,
    como_json: Annotated[
        bool, typer.Option("--json", help="Interpreta VALOR como JSON (listas, objetos).")
    ] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Cambia UNA clave de la config del vault, conservando el resto (scope vault).

    Lee el override actual, lo muta y lo manda entero: el `PATCH` REEMPLAZA el override, así que
    mandar sólo la clave tocada borraría todo lo demás.
    """

    def _op(c: GatewayClient) -> Any:
        actual = c.get(f"/v1/vaults/{name}/config")
        override = _editar_override(
            actual.get("override"), clave, valor, unset=unset, como_json=como_json
        )
        return c.patch(f"/v1/vaults/{name}/config", json=override)

    out = _run(_op, profile, gateway_url, api_key)
    destino = "quitada" if unset else f"= {valor}"
    typer.secho(f"✓ {name}: {clave} {destino}", fg=typer.colors.GREEN)
    _dump(out.get("effective", out))


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


# --------------------------------------------------------------------------- config global


@config_app.command("show")
def config_show(
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Muestra el perfil global del pipeline: override, efectivo y default de fábrica."""
    out = _run(lambda c: c.get("/v1/config"), profile, gateway_url, api_key)
    _dump(out)


@config_app.command("set")
def config_set(
    clave: Annotated[
        str,
        typer.Argument(help="Clave en notación de punto, p. ej. `groundedness.sample_rate`."),
    ],
    valor: Annotated[str | None, typer.Argument(help="Valor. Se omite con --unset.")] = None,
    unset: Annotated[bool, typer.Option("--unset", help="Quita la clave.")] = False,
    como_json: Annotated[
        bool, typer.Option("--json", help="Interpreta VALOR como JSON (listas, objetos).")
    ] = False,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Cambia UNA clave del perfil global, conservando el resto (scope admin).

    El perfil global sólo admite un subconjunto (scope/tuning): lo estructural va por env de fábrica
    o por vault. Una clave fuera de ese subconjunto la rechaza el gateway con 422 y te dice cuál.
    """

    def _op(c: GatewayClient) -> Any:
        actual = c.get("/v1/config")
        override = _editar_override(
            actual.get("override"), clave, valor, unset=unset, como_json=como_json
        )
        return c.patch("/v1/config", json=override)

    out = _run(_op, profile, gateway_url, api_key)
    destino = "quitada" if unset else f"= {valor}"
    typer.secho(f"✓ global: {clave} {destino}", fg=typer.colors.GREEN)
    _dump(out.get("effective", out))


# --------------------------------------------------------------------------- credential


_NUNCA = {"nunca", "never", "no vence"}
_DESCONOCIDO = {"desconocido", "unknown", "no sé", "no se", "?"}
_EXPIRES_HELP = (
    "Cuándo VENCE el secreto: YYYY-MM-DD, 'nunca' o 'desconocido'. Sin esto se pregunta (y sin "
    "terminal queda 'desconocido', con aviso)."
)


def _parse_vencimiento(texto: str, *, permitir_pasada: bool) -> dict[str, str | bool | None]:
    """El vencimiento como lo espera el gateway, desde lo que escribió el usuario.

    Una sola opción con tres respuestas —y no tres flags— para que no haya combinación
    contradictoria. ``ValueError`` si no se entiende; también si la fecha ya pasó y el secreto es
    NUEVO (el gateway lo rechazaría con 422, después de que el usuario pegó el secreto).
    """
    t = texto.strip().lower()
    if t in _NUNCA:
        return {"secret_expires_at": None, "secret_never_expires": True}
    if t in _DESCONOCIDO:
        return {"secret_expires_at": None, "secret_never_expires": False}
    try:
        fecha = date.fromisoformat(t)
    except ValueError as exc:
        raise ValueError(
            f"no entiendo {texto!r}: va YYYY-MM-DD (p. ej. 2027-04-30), 'nunca' o 'desconocido'"
        ) from exc
    # El día del gateway es UTC: con el local, de noche en Colombia se aceptaría una fecha que el
    # gateway ya considera vencida.
    if not permitir_pasada and fecha <= datetime.now(tz=UTC).date():
        raise ValueError(f"{fecha.isoformat()} ya pasó: un secreto nuevo no puede estar vencido")
    return {"secret_expires_at": fecha.isoformat(), "secret_never_expires": False}


def _validar_nuevo(value: str | None) -> str | None:
    """Callback de ``--expires`` en create/rotate: corre ANTES del prompt del secreto, así un typo
    en la fecha no cuesta el secreto ya pegado."""
    if value is not None:
        try:
            _parse_vencimiento(value, permitir_pasada=False)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    return value


def _validar_cualquiera(value: str | None) -> str | None:
    """Callback de ``--expires`` en set-expiry: el secreto guardado sí puede estar vencido."""
    if value is not None:
        try:
            _parse_vencimiento(value, permitir_pasada=True)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    return value


_ExpiresNuevo = Annotated[
    str | None, typer.Option("--expires", help=_EXPIRES_HELP, callback=_validar_nuevo)
]
_ExpiresGuardado = Annotated[
    str | None, typer.Option("--expires", help=_EXPIRES_HELP, callback=_validar_cualquiera)
]


def _hay_terminal() -> bool:
    """¿Hay alguien que conteste un prompt? (Aparte para poder fijarlo en los tests.)"""
    return sys.stdin.isatty()


def _vencimiento(expires: str | None, *, permitir_pasada: bool) -> dict[str, str | bool | None]:
    """El vencimiento del secreto: el de ``--expires`` o, si no vino, preguntado.

    Se PIDE siempre que no venga: el client secret de Entra y el PAT que se cayeron sin aviso
    tenían su fecha al crearlos y nadie la guardó. Una respuesta que no se entiende se vuelve a
    preguntar (el secreto ya está pegado). Sin terminal —el secreto por tubería, un script— no hay a
    quién preguntarle: queda desconocido y se avisa por stderr, en vez de abortar un comando que
    antes funcionaba.
    """
    if expires is not None:
        return _parse_vencimiento(expires, permitir_pasada=permitir_pasada)
    if not _hay_terminal():
        typer.secho(
            "⚠ sin --expires y sin terminal: el vencimiento queda DESCONOCIDO",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return {"secret_expires_at": None, "secret_never_expires": False}
    while True:
        resp = typer.prompt("¿Cuándo vence el secreto? (YYYY-MM-DD, 'nunca' o 'desconocido')")
        try:
            return _parse_vencimiento(resp, permitir_pasada=permitir_pasada)
        except ValueError as exc:
            typer.secho(f"✗ {exc}", fg=typer.colors.RED, err=True)


def _vence_legible(cr: dict[str, Any]) -> str:
    """El vencimiento que calculó el gateway, en una columna.

    Sin ``secret_expiry_status`` en la respuesta el gateway es anterior a la fecha de vencimiento
    (sin desplegar, o viejo): se dice así y no «DESCONOCIDO», que significa otra cosa —que nadie la
    declaró— y escondería que la fecha que se acaba de mandar no se guardó.
    """
    if "secret_expiry_status" not in cr:
        return "N/D (el gateway no informa vencimientos)"
    estado = cr.get("secret_expiry_status") or "unknown"
    fecha = cr.get("secret_expires_at") or ""
    dias = cr.get("secret_days_left")
    if estado == "never":
        return "no vence"
    if estado == "unknown":
        return "DESCONOCIDO"
    if estado == "expired":
        return f"{fecha} VENCIDO"
    return f"{fecha} ({dias} d)" if estado in ("warning", "critical") else fecha


def _eco_vencimiento(out: Any, enviado: dict[str, str | bool | None]) -> None:
    """Muestra lo que el gateway GUARDÓ, y avisa si no guardó lo que se mandó."""
    if not isinstance(out, dict):
        return
    typer.echo(f"  vence: {_vence_legible(out)}")
    declarado = enviado.get("secret_expires_at") or enviado.get("secret_never_expires")
    if declarado and "secret_expiry_status" not in out:
        typer.secho(
            "⚠ el gateway ignoró el vencimiento (es anterior a la migración 0026): no quedó "
            "guardado. Volvé a declararlo con `focusyn credential set-expiry` cuando se actualice.",
            fg=typer.colors.YELLOW,
            err=True,
        )


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
            f"{cr.get('llm_role') or '-':<10} {_vence_legible(cr)}"
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
    expires: _ExpiresNuevo = None,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Crea una credencial cifrada (scope credential; company exige admin).

    Junto al secreto va su vencimiento: con él, el panel de Credenciales avisa 30 y 7 días antes.
    """
    vence = _vencimiento(expires, permitir_pasada=False)
    body: dict[str, Any] = {"label": label, "kind": kind, "secret": secret, **vence}
    if provider:
        body["provider"] = provider
    if scope:
        body["scope"] = scope
    out = _run(lambda c: c.post("/v1/credentials", json=body), profile, gateway_url, api_key)
    typer.secho(f"✓ credencial '{label}' creada (id {out.get('id', '?')})", fg=typer.colors.GREEN)
    _eco_vencimiento(out, vence)


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
    expires: _ExpiresNuevo = None,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Reemplaza el secreto de una credencial (scope credential), con su vencimiento.

    Conserva el id y el resto de la credencial (el client/tenant id de un origen externo). El
    vencimiento es del secreto NUEVO: sin declararlo, la credencial queda «desconocido» en vez de
    heredar la fecha del que se reemplaza. Para un client secret de Entra va el **Value**, no el
    Secret ID.
    """
    vence = _vencimiento(expires, permitir_pasada=False)
    body = {"secret": secret, **vence}
    out = _run(
        lambda c: c.post(f"/v1/credentials/{credential_id}/rotate", json=body),
        profile,
        gateway_url,
        api_key,
    )
    typer.secho(f"✓ credencial {credential_id} rotada", fg=typer.colors.GREEN)
    _eco_vencimiento(out, vence)


@credential_app.command("set-expiry")
def credential_set_expiry(
    credential_id: Annotated[str, typer.Argument()],
    expires: _ExpiresGuardado = None,
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Declara cuándo vence el secreto que YA está guardado, sin reemplazarlo (scope credential).

    Para las credenciales anteriores a la fecha de vencimiento. Acepta una fecha pasada: el
    secreto guardado puede estar vencido, y decirlo es justamente el punto.
    """
    body = _vencimiento(expires, permitir_pasada=True)
    out = _run(
        lambda c: c.put(f"/v1/credentials/{credential_id}/secret-expiry", json=body),
        profile,
        gateway_url,
        api_key,
    )
    typer.secho(f"✓ vencimiento de {credential_id} declarado", fg=typer.colors.GREEN)
    _eco_vencimiento(out, body)


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


#: Roles del eje LLM que acepta el gateway. Los dos primeros eligen quién RESPONDE; los dos
#: últimos, quién EVALÚA lo respondido. Una credencial toma UNO solo: marcarle un rol nuevo le
#: quita el anterior, y por eso el juez nunca puede ser además el generador.
_LLM_ROLES = ("primary", "secondary", "judge", "judge_secondary")
#: Alias que el usuario escribe para QUITAR el rol. Viajan como ``null``, no como el string.
_ROLE_NINGUNO = ("none", "null", "-")


@credential_app.command("set-role")
def credential_set_role(
    credential_id: Annotated[str, typer.Argument()],
    role: Annotated[
        str,
        typer.Option("--role", help="primary | secondary | judge | judge_secondary | none"),
    ],
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Fija el rol LLM de una credencial (scope credential).

    ``judge``/``judge_secondary`` son el eje del JUEZ, que mide la fidelidad de lo que generó el
    otro eje. Sin rol ``judge`` no se juzga: no cae al primario, porque sería el generador
    evaluándose a sí mismo.
    """
    if role.lower() in _ROLE_NINGUNO:
        payload: dict[str, str | None] = {"role": None}
    elif role.lower() in _LLM_ROLES:
        payload = {"role": role.lower()}
    else:
        # Se valida ACÁ y no sólo en el gateway porque el 422 remoto no dice cuáles son los
        # válidos, y este comando ya tenía un modo de falla silencioso: ver el aviso de abajo.
        esperado = " | ".join((*_LLM_ROLES, "none"))
        typer.secho(
            f"✗ rol inválido: {role!r} (esperado {esperado})", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=2)
    _run(
        lambda c: c.put(f"/v1/credentials/{credential_id}/llm-role", json=payload),
        profile,
        gateway_url,
        api_key,
    )
    destino = "sin rol" if payload["role"] is None else payload["role"]
    typer.secho(f"✓ rol de {credential_id} → {destino}", fg=typer.colors.GREEN)


@credential_app.command("assign-vault")
def credential_assign_vault(
    vault: Annotated[str, typer.Argument(help="Nombre del vault.")],
    label: Annotated[
        str,
        typer.Option(
            "--credential-label", help="Label de la credencial, o 'none' para desasociar."
        ),
    ],
    profile: _Profile = None,
    gateway_url: _Url = None,
    api_key: _Key = None,
) -> None:
    """Asigna una credencial Git a un vault (scope vault).

    El gateway identifica la credencial por **id**, así que el label se resuelve acá contra
    ``GET /v1/credentials``. Un label que no existe se rechaza ANTES de llamar al PUT: mandarlo
    igual desasociaría la credencial del vault, que es lo contrario de lo que se pidió.
    """
    quitar = label.lower() in _ROLE_NINGUNO

    def _asignar(c: GatewayClient) -> Any:
        credential_id: str | None = None
        if not quitar:
            out = c.get("/v1/credentials")
            creds = out.get("credentials", out) if isinstance(out, dict) else out
            match = [
                cr
                for cr in (creds if isinstance(creds, list) else [])
                if cr.get("label") == label and cr.get("kind") == "git_pat"
            ]
            if not match:
                raise CliError(
                    f"no existe una credencial git_pat con label '{label}' "
                    f"(mirá `focusyn credential list`)",
                    code=2,
                )
            credential_id = str(match[0].get("id"))
        return c.put(f"/v1/vaults/{vault}/credential", json={"credential_id": credential_id})

    _run(_asignar, profile, gateway_url, api_key)
    destino = "desasociada" if quitar else f"credencial '{label}'"
    typer.secho(f"✓ vault '{vault}' → {destino}", fg=typer.colors.GREEN)


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
