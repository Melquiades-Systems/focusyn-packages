"""``focusyn`` — el CLI cliente. Habla HTTP con el gateway; el gateway autoriza por scopes.

Comandos (Fase 1):
  help / init / doctor / version  — onboarding y diagnóstico (credencial + hooks + toolchain)
  mcp install|status|uninstall    — emite la key de máquina y registra el MCP en Claude Code
  hooks install|status|uninstall  — automatiza `memory sync` en Claude Code (sin key inline)
  memory sync                     — reconcilia ~/.claude/projects/<proj>/memory/*.md con el corpus
  attachment upload               — sube un binario LOCAL por streaming multipart
  vault scaffold                  — instancia un template pack en un dossier
  deadcode                        — candidatos a dead code (100% local, no toca el gateway)

El de operador (Postgres directo: agent/user/org/tenant/credential/usage/source) es `focusynctl`,
en el paquete del gateway — nunca se distribuye.
"""

from __future__ import annotations

import base64
import binascii
import getpass
import json
import shutil
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from typer.main import get_command

from focusyn_cli import __version__, auth, session
from focusyn_cli import config as cfg
from focusyn_cli import hooks as hooks_mod
from focusyn_cli import mcp as mcp_mod
from focusyn_cli.attachment_upload import AttachmentUploadClient
from focusyn_cli.cmd_admin import (
    agent_app,
    credential_app,
    org_app,
    tenant_app,
    usage_app,
    vault_app,
)
from focusyn_cli.cmd_read import read_app
from focusyn_cli.cmd_write import write_app
from focusyn_cli.config import Credential
from focusyn_cli.deadcode import (
    RepoReport,
    find_repos,
    is_calibrated,
    scan_repos,
    select_projects,
    to_json,
)
from focusyn_cli.http import CliError, GatewayClient, check_url, version_warning
from focusyn_cli.memory_sync import (
    DEFAULT_PROJECTS_ROOT,
    MemorySyncClient,
    collect_memory_docs,
    discover_projects,
)

app = typer.Typer(help="CLI cliente de focusyn (HTTP; autorizado por scopes del gateway).")
memory_app = typer.Typer(help="Sync de memorias de Claude Code al corpus.")
attachment_app = typer.Typer(help="Subida de binarios (adjuntos) por multipart.")
hooks_app = typer.Typer(help="Hooks de Claude Code que automatizan `memory sync`.")
mcp_app = typer.Typer(help="El MCP focusyn en Claude Code: emite la key y lo registra por vos.")
app.add_typer(memory_app, name="memory")
app.add_typer(attachment_app, name="attachment")
app.add_typer(hooks_app, name="hooks")
app.add_typer(mcp_app, name="mcp")
# Administración por HTTP (Fase 2). El gateway autoriza por scopes (403 si no alcanza).
app.add_typer(vault_app, name="vault")
app.add_typer(credential_app, name="credential")
app.add_typer(org_app, name="org")
app.add_typer(tenant_app, name="tenant")
app.add_typer(usage_app, name="usage")
app.add_typer(agent_app, name="agent")
# Lectura y escritura como comandos TOP-LEVEL (`focusyn read/search/ask`, `focusyn propose/apply`):
# se fusionan las command lists en vez de anidar sub-apps (evita `focusyn read read`).
app.registered_commands.extend(read_app.registered_commands)
app.registered_commands.extend(write_app.registered_commands)

_INVITE_PREFIX = "focusyn-invite:"


# --------------------------------------------------------------------------- #
# helpers de credencial
# --------------------------------------------------------------------------- #


def _resolve(
    profile: str | None, gateway_url: str | None, api_key: str | None, *, need_secret: bool
) -> Credential:
    """La credencial efectiva o un error accionable si falta gateway/secreto."""
    cred = cfg.resolve(profile=profile, gateway_url=gateway_url, api_key=api_key)
    if cred is None:
        raise CliError(
            "No hay gateway configurado. Corré `focusyn init` (o pasá --gateway-url / "
            "FOCUSYN_GATEWAY_URL).",
            code=2,
        )
    if need_secret and not cred.has_secret():
        raise CliError(
            "No hay credencial para este gateway. Corré `focusyn init` (o pasá --api-key / "
            "FOCUSYN_API_KEY).",
            code=2,
        )
    return cred


def _fail(exc: CliError) -> None:
    typer.secho(f"✗ {exc.message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=exc.code)


# --------------------------------------------------------------------------- #
# init — configura credencial + perfil
# --------------------------------------------------------------------------- #


def _decode_invite(blob: str) -> tuple[str, str]:
    """`focusyn-invite:<base64({url,key})>` → (url, key). Eleva CliError si no decodifica."""
    raw = blob[len(_INVITE_PREFIX) :] if blob.startswith(_INVITE_PREFIX) else blob
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
        return str(data["url"]), str(data["key"])
    except (binascii.Error, ValueError, KeyError, UnicodeDecodeError) as exc:
        raise CliError(f"--invite inválido (no es un blob focusyn-invite): {exc}") from exc


@app.command("init")
def init(
    invite: Annotated[
        str | None,
        typer.Option(
            "--invite", help="Blob `focusyn-invite:…` con URL + key (evita copiar a mano)."
        ),
    ] = None,
    gateway_url: Annotated[
        str | None, typer.Option("--gateway-url", help="URL del gateway (default: la pública).")
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key", help="Key no-interactiva (scripting/CI); si falta se pide con getpass."
        ),
    ] = None,
    profile: Annotated[
        str, typer.Option("--profile", help="Nombre del perfil a guardar.")
    ] = "default",
    with_mcp: Annotated[
        bool, typer.Option("--with-mcp", help="Registrar además el MCP focusyn en Claude Code.")
    ] = False,
    no_key: Annotated[
        bool, typer.Option("--no-key", help="Guardar sólo la URL (acceso público, sin credencial).")
    ] = False,
) -> None:
    """Crea ``~/.config/focusyn/config.toml`` (0600). Valida la URL ANTES de pedir la key."""
    try:
        key: str | None
        if invite:
            url, key = _decode_invite(invite)
        else:
            url = gateway_url or typer.prompt("Gateway URL", default=cfg.DEFAULT_GATEWAY_URL)
            key = api_key

        # Validar la URL contra /v1/capabilities (público) ANTES de pedir credencial: distingue
        # "URL mal escrita" de "credencial mala".
        caps = check_url(Credential(gateway_url=url))
        typer.secho(
            f"✓ gateway {url} (api {caps.get('api_version', '?')}, "
            f"{len(caps.get('vaults', []))} vaults)",
            fg=typer.colors.GREEN,
        )

        if not invite and not no_key and key is None:
            key = (
                getpass.getpass(
                    "API key (scope según lo que vayas a hacer; vacío = sólo lectura pública): "
                ).strip()
                or None
            )

        config = cfg.load_config()
        config.profiles[profile] = Credential(gateway_url=url, api_key=key)
        if profile == "default" or not config.profiles.get(config.default_profile):
            config.default_profile = profile
        path = cfg.save_config(config)
        typer.secho(f"✓ perfil '{profile}' guardado en {path} (0600)", fg=typer.colors.GREEN)

        warn = version_warning(caps)
        if warn:
            typer.secho(f"  ⚠ {warn}", fg=typer.colors.YELLOW, err=True)

        if with_mcp:
            _register_mcp(url, key)

        typer.echo("Probá `focusyn doctor` para verificar la credencial y los hooks.")
    except CliError as exc:
        _fail(exc)


def _register_mcp(url: str, key: str | None) -> None:
    """Registra el MCP con la key que YA se acaba de configurar (atajo de `init --with-mcp`).

    No emite una key nueva: para eso está `focusyn mcp install`, que pide una de máquina acotada.
    """
    if not key:
        typer.secho(
            "  ⚠ sin API key no registro el MCP (necesita el header). Después de un `login`, "
            "corré `focusyn mcp install` (emite una key de máquina).",
            fg=typer.colors.YELLOW,
        )
        return
    try:
        mcp_url = mcp_mod.add(mcp_mod.DEFAULT_SERVER_NAME, url, key)
    except CliError as exc:
        typer.secho(f"  ⚠ {exc.message}", fg=typer.colors.YELLOW, err=True)
        return
    typer.secho(f"✓ MCP focusyn registrado ({mcp_url})", fg=typer.colors.GREEN)


# --------------------------------------------------------------------------- #
# login — usuario de la web (JWT)
# --------------------------------------------------------------------------- #


@app.command("login")
def login(
    username: Annotated[
        str | None, typer.Option("--username", "-u", help="Usuario de la web.")
    ] = None,
    gateway_url: Annotated[
        str | None, typer.Option("--gateway-url", envvar="FOCUSYN_GATEWAY_URL")
    ] = None,
    profile: Annotated[str, typer.Option("--profile")] = "default",
) -> None:
    """Login con el usuario de la web (JWT). Guarda access+refresh en el perfil (0600).

    Un Bearer lleva tus scopes y tu tenant → hacés lo que tus scopes permiten (403 donde no). El
    access se renueva solo con el refresh; para el hook de memory sync usá una API key (`init`).
    """
    try:
        config = cfg.load_config()
        base = config.profiles.get(profile)
        url = (
            gateway_url
            or (base.gateway_url if base else None)
            or typer.prompt("Gateway URL", default=cfg.DEFAULT_GATEWAY_URL)
        )
        check_url(Credential(gateway_url=url))  # valida la URL antes de pedir credenciales
        user = username or typer.prompt("Usuario")
        password = getpass.getpass("Contraseña: ")
        tokens = auth.login(url, user, password)
    except CliError as exc:
        _fail(exc)
        return

    entry = config.profiles.get(profile) or Credential(gateway_url=url)
    entry.gateway_url = url
    entry.access_token = tokens.access_token
    entry.refresh_token = tokens.refresh_token
    entry.api_key = None  # login y api_key son excluyentes en un perfil
    config.profiles[profile] = entry
    if not config.profiles.get(config.default_profile):
        config.default_profile = profile
    cfg.save_config(config)
    typer.secho(f"✓ login OK como '{user}'  → perfil '{profile}' (0600)", fg=typer.colors.GREEN)
    typer.echo("Probá `focusyn doctor` para ver tus scopes.")


# --------------------------------------------------------------------------- #
# doctor — diagnóstico
# --------------------------------------------------------------------------- #


@app.command("doctor")
def doctor(
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    gateway_url: Annotated[str | None, typer.Option("--gateway-url")] = None,
) -> None:
    """Chequea URL, credencial+scopes, versión, hooks y toolchain de deadcode."""
    ok = True
    cred = cfg.resolve(profile=profile, gateway_url=gateway_url)
    if cred is None:
        typer.secho("✗ sin gateway configurado → corré `focusyn init`", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # 1. URL (público)
    try:
        caps = check_url(cred)
        typer.secho(
            f"✓ gateway {cred.gateway_url} (api {caps.get('api_version', '?')}, "
            f"{len(caps.get('vaults', []))} vaults)",
            fg=typer.colors.GREEN,
        )
    except CliError as exc:
        typer.secho(f"✗ gateway: {exc.message}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    # 2. credencial + scopes (whoami; degradado si el gateway no lo tiene aún)
    if not cred.has_secret():
        typer.secho("• credencial: ninguna (sólo acceso público)", fg=typer.colors.YELLOW)
    else:
        try:
            # session.client_for cablea el auto-refresh del JWT (un access expirado se renueva).
            with session.client_for(profile, gateway_url, need_secret=True) as client:
                who = client.whoami()
            scopes = ", ".join(who.get("scopes", [])) or "(sin scopes)"
            kind = who.get("kind", "")
            typer.secho(
                f"✓ credencial {cred.key_prefix()} ({kind})  scopes: {scopes}"
                if kind
                else f"✓ credencial {cred.key_prefix()}  scopes: {scopes}",
                fg=typer.colors.GREEN,
            )
        except CliError as exc:
            # 404 = el gateway no expone whoami todavía (Fase 3): degradar, no fallar.
            if "HTTP 404" in exc.message:
                typer.secho(
                    f"• credencial {cred.key_prefix()} (no puedo verificar scopes: el gateway "
                    "no expone /v1/auth/whoami todavía)",
                    fg=typer.colors.YELLOW,
                )
            else:
                typer.secho(f"✗ credencial: {exc.message}", fg=typer.colors.RED)
                ok = False

    # 3. versión
    warn = version_warning(caps)
    if warn:
        typer.secho(f"⚠ versión: {warn}", fg=typer.colors.YELLOW)
    else:
        typer.secho(f"✓ cliente {__version__} (compatible)", fg=typer.colors.GREEN)

    # 4. hooks
    installed = hooks_mod.current()
    if installed:
        evs = ", ".join(sorted({e for e, _ in installed}))
        typer.secho(f"✓ hooks de memory sync: {evs}", fg=typer.colors.GREEN)
    else:
        typer.secho(
            "• hooks de memory sync: no instalados → `focusyn hooks install`",
            fg=typer.colors.YELLOW,
        )

    # 5. toolchain de deadcode (informativo — deadcode marca 'omitido' lo que falte)
    tools = {t: shutil.which(t) for t in ("vulture", "go", "npx")}
    present = [t for t, p in tools.items() if p]
    missing = [t for t, p in tools.items() if not p]
    typer.echo(
        f"• deadcode: {'presentes ' + ', '.join(present) if present else 'sin toolchain'}"
        + (f" · faltan {', '.join(missing)}" if missing else "")
    )

    if not ok:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# version
# --------------------------------------------------------------------------- #


@app.command("version")
def version(
    profile: Annotated[str | None, typer.Option("--profile")] = None,
) -> None:
    """Versión del cliente (+ del gateway si hay uno configurado)."""
    typer.echo(f"focusyn-cli {__version__}")
    cred = cfg.resolve(profile=profile)
    if cred is None:
        return
    try:
        with GatewayClient(cred) as client:
            caps = client.capabilities()
    except CliError:
        return
    typer.echo(f"gateway {cred.gateway_url}  api {caps.get('api_version', '?')}")
    warn = version_warning(caps)
    if warn:
        typer.secho(f"⚠ {warn}", fg=typer.colors.YELLOW, err=True)


# --------------------------------------------------------------------------- #
# hooks
# --------------------------------------------------------------------------- #


@hooks_app.command("install")
def hooks_install(
    events: Annotated[
        str, typer.Option("--events", help="Eventos separados por coma (SessionEnd,PreCompact).")
    ] = "SessionEnd,PreCompact",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Muestra qué haría, sin escribir.")
    ] = False,
) -> None:
    """Escribe los hooks en ~/.claude/settings.json SIN la key adentro (migra el legacy inline)."""
    evs = tuple(e.strip() for e in events.split(",") if e.strip())
    try:
        result = hooks_mod.install(evs, dry_run=dry_run)
    except FileNotFoundError as exc:
        typer.secho(f"✗ {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    verb = "instalaría" if dry_run else "instalados"
    typer.secho(
        f"✓ hooks {verb}: {', '.join(result.events)}  (binario {result.binary})",
        fg=typer.colors.GREEN,
    )
    if result.removed_legacy:
        typer.echo(
            f"  · removidos {result.removed_legacy} handler(s) previo(s) "
            "(incluye el legacy con key inline)"
        )
    if result.backup_path:
        typer.echo(f"  · backup: {result.backup_path}")
    if dry_run:
        typer.echo(f"  · comando: {hooks_mod.build_command(result.binary, result.events[0])}")
    else:
        typer.echo("  · si no surte efecto, abrí `/hooks` una vez o reiniciá la sesión.")


@hooks_app.command("status")
def hooks_status() -> None:
    """Muestra los hooks de memory sync instalados."""
    installed = hooks_mod.current()
    if not installed:
        typer.secho("• sin hooks de memory sync instalados", fg=typer.colors.YELLOW)
        return
    typer.secho(f"Hooks en {hooks_mod.settings_path()}:", bold=True)
    for event, command in installed:
        typer.echo(f"  {event}: {command}")


@hooks_app.command("uninstall")
def hooks_uninstall(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Quita todos nuestros hooks de memory sync (backup + atómico)."""
    result = hooks_mod.uninstall(dry_run=dry_run)
    if not result.removed_legacy:
        typer.secho("• no había hooks de memory sync que quitar", fg=typer.colors.YELLOW)
        return
    verb = "quitaría" if dry_run else "quitados"
    typer.secho(f"✓ {result.removed_legacy} hook(s) {verb}", fg=typer.colors.GREEN)
    if result.backup_path:
        typer.echo(f"  · backup: {result.backup_path}")


# --------------------------------------------------------------------------- #
# mcp — emite la key de máquina y registra el MCP focusyn en Claude Code
# --------------------------------------------------------------------------- #


def _granted_scopes(client: GatewayClient, wanted: list[str]) -> list[str]:
    """Los scopes que el gateway REALMENTE va a otorgar: los pedidos ∩ los tuyos (admin: todos).

    Pedir de más no es un aviso, es un 403: el gateway aplica no-escalada. Mejor intersecar acá y
    decirlo, que fallar con un error que el usuario no puede accionar.
    """
    try:
        who = client.whoami()
    except CliError as exc:
        if "HTTP 404" in exc.message:
            raise CliError(
                "Este gateway todavía no expone el self-service de keys (`/v1/auth/whoami` y "
                "`POST /v1/agents`). Registrá el MCP con una key ya emitida: "
                "`focusyn mcp install --use-key <api-key>`.",
                code=2,
            ) from exc
        raise
    mine = [str(s) for s in who.get("scopes", [])]
    if "admin" in mine:  # admin es comodín: satisface cualquier scope
        return wanted
    granted = [s for s in wanted if s in mine]
    if "read" not in granted:
        raise CliError(
            f"Tu principal ({who.get('principal', '?')}) no tiene el scope 'read' "
            f"(tiene: {', '.join(mine) or 'ninguno'}). Sin 'read' el MCP no sirve de nada — "
            "pedile al owner una credencial con los scopes que necesitás.",
            code=2,
        )
    return granted


def _issue_key(
    client: GatewayClient, name: str, scopes: list[str], rate_limit: int, *, rotate: bool
) -> tuple[str, str]:
    """La API key de máquina: la del agente nuevo, o la rotada si ya existía. → (key, acción)."""
    agents = client.get("/v1/agents")
    existing = next(
        (a for a in agents.get("agents", []) if a.get("agent_id") == name), None
    ) if isinstance(agents, dict) else None

    if existing is not None:
        if not rotate:
            raise CliError(
                f"Ya existe un agente '{name}'. Para emitirle una key nueva y re-registrar el MCP: "
                "`focusyn mcp install --rotate` (la key vieja deja de valer). O usá otro --name.",
                code=2,
            )
        out = client.post(f"/v1/agents/{name}/rotate")
        return str(out["api_key"]), "rotada"

    out = client.post(
        "/v1/agents",
        json={"name": name, "scopes": scopes, "rate_limit_rpm": rate_limit},
    )
    return str(out["api_key"]), "emitida"


@mcp_app.command("install")
def mcp_install(
    name: Annotated[
        str | None,
        typer.Option("--name", help="agent_id de la key de máquina (default: mcp-<hostname>)."),
    ] = None,
    scopes: Annotated[
        str, typer.Option("--scopes", help="Scopes de la key; se intersecan con los tuyos.")
    ] = ",".join(mcp_mod.DEFAULT_SCOPES),
    rate_limit: Annotated[
        int, typer.Option("--rate-limit", help="Peticiones/min de la key (0 = ilimitado).")
    ] = 60,
    rotate: Annotated[
        bool,
        typer.Option("--rotate", help="Si el agente ya existe: rota su key y re-registra el MCP."),
    ] = False,
    use_key: Annotated[
        str | None,
        typer.Option("--use-key", help="Registra con una key YA emitida (no emite ninguna)."),
    ] = None,
    server_name: Annotated[
        str, typer.Option("--server-name", help="Nombre del server en Claude Code.")
    ] = mcp_mod.DEFAULT_SERVER_NAME,
    claude_scope: Annotated[
        str, typer.Option("--claude-scope", help="user (todos los proyectos) | local | project.")
    ] = mcp_mod.DEFAULT_CLAUDE_SCOPE,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Muestra qué haría; no emite key ni toca Claude Code.")
    ] = False,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    gateway_url: Annotated[
        str | None, typer.Option("--gateway-url", envvar="FOCUSYN_GATEWAY_URL")
    ] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", envvar="FOCUSYN_API_KEY")] = None,
) -> None:
    """Emite una API key de máquina y registra el MCP focusyn en Claude Code, de una.

    Autenticate primero (`focusyn login`, o una key con `init`): la key del MCP se emite acotada a
    un subconjunto de TUS scopes (no-escalada). Es una key por máquina, distinta de tu credencial
    personal → rotarla (`--rotate`) no te desloguea a vos.
    """
    wanted = [s.strip() for s in scopes.split(",") if s.strip()]
    agent_name = name or mcp_mod.default_agent_name()

    try:
        cred = session.credential_for(
            profile, gateway_url, api_key, need_secret=use_key is None
        )
        url = mcp_mod.mcp_url(cred.gateway_url)

        if dry_run:
            typer.echo(f"MCP '{server_name}' → {url}  (scope Claude Code: {claude_scope})")
            if use_key:
                typer.echo(f"  key: la pasada en --use-key ({use_key[:12]}…)")
            else:
                typer.echo(
                    f"  key: emitiría el agente '{agent_name}' "
                    f"con scopes {','.join(wanted)} (∩ los tuyos)"
                )
            return

        key = use_key
        action = "reusada"
        if key is None:
            with session.client_for(profile, gateway_url, api_key) as client:
                granted = _granted_scopes(client, wanted)
                key, action = _issue_key(client, agent_name, granted, rate_limit, rotate=rotate)
            dropped = sorted(set(wanted) - set(granted))
            if dropped:
                typer.secho(
                    f"  ⚠ no tenés {', '.join(dropped)} → la key va sin esos scopes.",
                    fg=typer.colors.YELLOW,
                )
            typer.secho(
                f"✓ key {action} para '{agent_name}' (scopes: {', '.join(granted)})",
                fg=typer.colors.GREEN,
            )

        registered = mcp_mod.add(server_name, cred.gateway_url, key, claude_scope=claude_scope)
    except CliError as exc:
        _fail(exc)
        return

    typer.secho(f"✓ MCP '{server_name}' registrado → {registered}", fg=typer.colors.GREEN)
    typer.echo("  · reiniciá Claude Code (o `/mcp`) para que tome el server.")
    typer.echo("  · verificá con `focusyn mcp status`.")


@mcp_app.command("status")
def mcp_status(
    server_name: Annotated[str, typer.Option("--server-name")] = mcp_mod.DEFAULT_SERVER_NAME,
) -> None:
    """Muestra el MCP registrado en Claude Code y valida que su key siga viva contra el gateway."""
    try:
        reg = mcp_mod.get(server_name)
    except CliError as exc:
        _fail(exc)
        return
    if reg is None:
        typer.secho(
            f"• MCP '{server_name}': no registrado → `focusyn mcp install`", fg=typer.colors.YELLOW
        )
        return

    typer.secho(f"✓ MCP '{server_name}' → {reg.url}", fg=typer.colors.GREEN)
    typer.echo(f"  scope: {reg.scope_flag() or '?'}  ·  key: {reg.key_prefix()}")

    if not reg.api_key or not reg.url:
        return
    # La key del header es la verdad: validarla contra el gateway distingue "registrado" de "sirve".
    base = reg.url.removesuffix("/mcp/").removesuffix("/mcp")
    try:
        with GatewayClient(Credential(gateway_url=base, api_key=reg.api_key)) as client:
            who = client.whoami()
    except CliError as exc:
        if "HTTP 404" in exc.message:
            typer.secho(
                "  • no puedo verificar la key: el gateway no expone /v1/auth/whoami todavía.",
                fg=typer.colors.YELLOW,
            )
            return
        typer.secho(
            f"✗ la key registrada no sirve: {exc.message}\n"
            "  → `focusyn mcp install --rotate` emite una nueva y re-registra.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from exc
    typer.secho(
        f"✓ key válida ({who.get('principal', '?')}, scopes: {', '.join(who.get('scopes', []))})",
        fg=typer.colors.GREEN,
    )


@mcp_app.command("uninstall")
def mcp_uninstall(
    server_name: Annotated[str, typer.Option("--server-name")] = mcp_mod.DEFAULT_SERVER_NAME,
    claude_scope: Annotated[str, typer.Option("--claude-scope")] = mcp_mod.DEFAULT_CLAUDE_SCOPE,
) -> None:
    """Desregistra el MCP de Claude Code. La key de máquina sigue viva (`focusyn agent disable`)."""
    try:
        removed = mcp_mod.remove(server_name, claude_scope=claude_scope)
    except CliError as exc:
        _fail(exc)
        return
    if not removed:
        typer.secho(
            f"• no había un MCP '{server_name}' en scope {claude_scope}", fg=typer.colors.YELLOW
        )
        return
    typer.secho(f"✓ MCP '{server_name}' desregistrado", fg=typer.colors.GREEN)
    typer.echo("  · la API key sigue activa: para revocarla, `focusyn agent disable <name>`.")


# --------------------------------------------------------------------------- #
# memory sync
# --------------------------------------------------------------------------- #


@memory_app.command("sync")
def memory_sync(
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key", envvar="FOCUSYN_INGEST_KEY", help="Override de la key (scope ingest)."
        ),
    ] = None,
    gateway_url: Annotated[
        str | None, typer.Option("--gateway-url", envvar="FOCUSYN_GATEWAY_URL")
    ] = None,
    projects_root: Annotated[
        Path, typer.Option("--projects-root", help="Raíz de los proyectos de Claude Code.")
    ] = DEFAULT_PROJECTS_ROOT,
    project: Annotated[
        list[str] | None, typer.Option("--project", help="Limita a estos slugs (repetible).")
    ] = None,
    no_prune: Annotated[bool, typer.Option("--no-prune", help="No borra los ausentes.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Lista sin llamar al gateway.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", help="Imprime sólo si hubo cambios o error (para el hook).")
    ] = False,
) -> None:
    """Reconcilia ``~/.claude/projects/<proj>/memory/*.md`` con la partición corpus.

    La credencial (scope ``ingest``) sale del perfil; --api-key/--gateway-url la sobrescriben.
    Re-correr es no-op; borrar una memoria local la borra del índice en el próximo sync.
    """
    # dry-run es 100% local (lista archivos): no necesita gateway ni credencial.
    projects = discover_projects(projects_root)
    if project:
        only = set(project)
        for slug in sorted(only - {p.slug for p in projects}):
            if not quiet:
                typer.secho(
                    f"  ⚠ proyecto sin memory/: {slug} (omitido)", fg=typer.colors.YELLOW, err=True
                )
        projects = [p for p in projects if p.slug in only]

    if not projects:
        if not quiet:
            typer.secho("No hay proyectos con memory/ que sincronizar.", fg=typer.colors.YELLOW)
        return

    prune = not no_prune
    if dry_run:
        for p in projects:
            typer.echo(f"{p.slug}/  → {len(collect_memory_docs(p))} .md (prune={prune})")
        return

    try:
        cred = _resolve(profile, gateway_url, api_key, need_secret=True)
    except CliError as exc:
        _fail(exc)
        return
    if not cred.api_key:
        _fail(
            CliError(
                "memory sync necesita una API key con scope 'ingest' "
                "(el JWT no sirve para el hook)."
            )
        )
        return

    created = updated = unchanged = deleted = err = 0
    assert cred.api_key is not None
    with MemorySyncClient(cred.gateway_url, cred.api_key) as client:
        for p in projects:
            try:
                r = client.sync_project(p, prune=prune)
            except httpx.HTTPStatusError as exc:
                typer.secho(
                    f"  ✗ {p.slug}: HTTP {exc.response.status_code} {exc.response.text[:200]}",
                    fg=typer.colors.RED,
                    err=True,
                )
                err += 1
                continue
            except (httpx.HTTPError, OSError, ValueError) as exc:
                typer.secho(f"  ✗ {p.slug}: {exc}", fg=typer.colors.RED, err=True)
                err += 1
                continue
            created += r.created
            updated += r.updated
            unchanged += r.unchanged
            deleted += r.deleted
            err += len(r.errors)
            changed = r.created or r.updated or r.deleted or r.errors
            if not quiet or changed:
                line = (
                    f"  {p.slug}/  sent={r.sent} created={r.created} updated={r.updated} "
                    f"unchanged={r.unchanged} deleted={r.deleted}"
                )
                if r.errors:
                    line += f" errors={len(r.errors)}"
                typer.echo(line)

    if not quiet or created or updated or deleted or err:
        typer.echo(
            f"Total: created={created} updated={updated} unchanged={unchanged} "
            f"deleted={deleted} errors={err}"
        )
    if err:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# attachment upload
# --------------------------------------------------------------------------- #


@attachment_app.command("upload")
def attachment_upload(
    file: Annotated[
        Path,
        typer.Option(
            "--file", exists=True, dir_okay=False, readable=True, help="Binario LOCAL a subir."
        ),
    ],
    vault: Annotated[
        str, typer.Option("--vault", help="Vault destino (debe existir y ser escribible).")
    ],
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", envvar="FOCUSYN_APPLY_KEY", help="Override (scope apply)."),
    ] = None,
    gateway_url: Annotated[
        str | None, typer.Option("--gateway-url", envvar="FOCUSYN_GATEWAY_URL")
    ] = None,
    doc_id: Annotated[
        str | None, typer.Option("--doc-id", help="doc_id de la nota que lo referencia.")
    ] = None,
    alt: Annotated[
        str | None, typer.Option("--alt", help="Texto alternativo del contenido.")
    ] = None,
    content_type: Annotated[
        str | None, typer.Option("--content-type", help="MIME explícito.")
    ] = None,
) -> None:
    """Sube un binario LOCAL por streaming multipart y muestra su ``markdown_ref`` (scope apply)."""
    try:
        cred = _resolve(profile, gateway_url, api_key, need_secret=True)
    except CliError as exc:
        _fail(exc)
        return
    if not cred.api_key:
        _fail(CliError("attachment upload necesita una API key con scope 'apply'."))
        return

    try:
        with AttachmentUploadClient(cred.gateway_url, cred.api_key) as client:
            out = client.upload(file, vault, doc_id=doc_id, alt=alt, content_type=content_type)
    except httpx.HTTPStatusError as exc:
        typer.secho(
            f"✗ HTTP {exc.response.status_code} {exc.response.text[:200]}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except (httpx.HTTPError, OSError, ValueError) as exc:
        typer.secho(f"✗ {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"✓ {out.status}  file_id={out.file_id}  ({out.content_type}, {out.size_bytes} bytes)",
        fg=typer.colors.GREEN,
    )
    typer.echo(out.markdown_ref)


# --------------------------------------------------------------------------- #
# deadcode (100% local — ni gateway ni BD ni red)
# --------------------------------------------------------------------------- #


def _deadcode_targets(repos: list[Path], workspace: Path | None) -> list[Path]:
    """Los repos a analizar, sin duplicados y en orden estable. Sin nada pedido: el cwd."""
    targets = list(repos)
    if workspace is not None:
        targets.extend(find_repos(workspace))
    if not targets:
        targets = [Path()]
    seen: set[Path] = set()
    unique: list[Path] = []
    for target in targets:
        resolved = target.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(target)
    return unique


def _deadcode_list(targets: list[Path], lang: str) -> None:
    """Qué se analizaría y qué está calibrado — sin correr un solo detector."""
    for target in targets:
        projects = select_projects(target, lang)
        if not projects:
            typer.secho(f"{target.name or target.resolve().name}: —", fg=typer.colors.YELLOW)
            continue
        detail = ", ".join(
            f"{p.label()} "
            f"{'✔ calibrado' if is_calibrated(p.root(target), p.lang) else '⚠ sin config'}"
            for p in projects
        )
        typer.echo(f"{target.name or target.resolve().name}: {detail}")


def _deadcode_render(report: RepoReport, *, header: bool) -> None:
    """Imprime los avisos y los candidatos de un repo (los paths, relativos a SU raíz)."""
    if header:
        typer.secho(f"\n── {report.name} ({report.path})", fg=typer.colors.BLUE, bold=True)
    for warning in report.result.warnings:
        typer.secho(f"  ⚠ {warning}", fg=typer.colors.YELLOW, err=True)
    for f in report.result.findings:
        typer.echo(f"{f.path}:{f.line}: {f.kind} '{f.symbol}' ({f.confidence}%)")


def _deadcode_summary(reports: list[RepoReport]) -> None:
    """El resumen del ecosistema: candidatos por repo y —sobre todo— cuáles NO se revisaron."""
    typer.secho(f"\nResumen ({len(reports)} repos):", bold=True)
    for report in reports:
        if not report.reviewed():
            omitted = ", ".join(p.label() for p in report.result.skipped)
            reason = f"omitidos: {omitted}" if omitted else report.result.warnings[0]
            typer.secho(f"  ✖ {report.name:<24} SIN REVISAR ({reason})", fg=typer.colors.RED)
            continue
        scanned = ", ".join(p.label() for p in report.result.analyzed)
        skipped = report.result.skipped
        tail = f" · omitidos: {', '.join(p.label() for p in skipped)}" if skipped else ""
        count = len(report.result.findings)
        color = typer.colors.CYAN if count else typer.colors.GREEN
        typer.secho(f"  ✔ {report.name:<24} {count:>4} candidatos · {scanned}{tail}", fg=color)


@app.command("deadcode")
def deadcode(
    repo: Annotated[
        list[Path] | None,
        typer.Option(
            "--repo",
            exists=True,
            file_okay=False,
            help="Repo a analizar; repetible (default: cwd).",
        ),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option(
            "--workspace",
            exists=True,
            file_okay=False,
            help="Analiza los repos git hijos de este dir.",
        ),
    ] = None,
    lang: Annotated[
        str, typer.Option("--lang", help="auto | python | go | ts ('auto' detecta por marcadores).")
    ] = "auto",
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Vuelca el reporte a un JSON.")
    ] = None,
    all_kinds: Annotated[
        bool,
        typer.Option(
            "--all", help="Incluye variables/atributos (casi todo falso positivo en ORM)."
        ),
    ] = False,
    list_only: Annotated[
        bool, typer.Option("--list", help="Sólo lista repos/proyectos y si están calibrados.")
    ] = False,
) -> None:
    """Candidatos a dead code de uno o varios repos, con el detector NATIVO de cada lenguaje.

    No corre en el gateway ni usa el índice: analiza el **working tree local**. Con ``--workspace``
    barre los clones locales de ese directorio (ADR MEL-DEC-198). Devuelve **candidatos**: el
    veredicto es humano. No instala nada — si falta el toolchain, marca el proyecto 'omitido'.
    """
    targets = _deadcode_targets(list(repo or []), workspace)
    explicit_single = workspace is None and len(targets) == 1

    if list_only:
        _deadcode_list(targets, lang)
        return

    reports = scan_repos(targets, lang=lang, all_kinds=all_kinds, force_lang=explicit_single)
    multi = len(reports) > 1
    for report in reports:
        _deadcode_render(report, header=multi)

    reviewed = [r for r in reports if r.reviewed()]
    if not reviewed:
        typer.secho(
            "No se pudo analizar NINGÚN proyecto (ver los avisos): esto no es código limpio, "
            "es código sin revisar.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    findings = sum(len(r.result.findings) for r in reports)
    if multi:
        _deadcode_summary(reports)
        unreviewed = len(reports) - len(reviewed)
        tail = f" {unreviewed} SIN REVISAR." if unreviewed else ""
        typer.secho(
            f"\n{findings} candidatos en {len(reviewed)} repos revisados.{tail} "
            "Son candidatos, no sentencias: leé cada símbolo antes de borrarlo.",
            fg=typer.colors.CYAN,
        )
    else:
        result = reports[0].result
        scanned = ", ".join(p.label() for p in result.analyzed)
        omitted = (
            f" Omitidos: {', '.join(p.label() for p in result.skipped)}." if result.skipped else ""
        )
        if not findings:
            typer.secho(f"Sin candidatos ({scanned}).{omitted}", fg=typer.colors.GREEN)
        else:
            plural = "candidato" if findings == 1 else "candidatos"
            typer.secho(
                f"\n{findings} {plural} ({scanned}).{omitted} "
                "Son candidatos, no sentencias: leé cada símbolo antes de borrarlo.",
                fg=typer.colors.CYAN,
            )

    if json_out is not None:
        to_json(reports, json_out)
        typer.echo(f"JSON → {json_out}")


# --------------------------------------------------------------------------- #
# help — la guía por tarea (`--help` sólo lista comandos en orden de registro)
# --------------------------------------------------------------------------- #

_GUIDE = """\
focusyn — el cliente del gateway. Todo va por HTTP; el gateway autoriza por tus scopes.

EMPEZAR DESDE CERO
  focusyn login                  autenticate con tu usuario de la web (JWT, se renueva solo)
  focusyn init                   o pegá una API key / un blob `focusyn-invite:` (--invite)
  focusyn mcp install            emite la key de máquina y registra el MCP en Claude Code
  focusyn hooks install          sincroniza tus memorias de Claude Code al corpus, solo
  focusyn doctor                 gateway + credencial + scopes + hooks, en una pantalla

LEER
  read · list · search · ask · map        (search/ask son notas y ADR, no código:
                                           para código está el MCP `find_code`)
ESCRIBIR
  propose → apply                        proponer y commitear (el gateway es el único escritor)
  delete · link                          borrar · enlazar dos documentos

ADMINISTRAR (403 limpio si te falta el scope)
  vault · credential · agent · org · tenant · usage

LOCAL (no toca el gateway)
  deadcode                       candidatos a dead code con el detector nativo de cada lenguaje

Ayuda de cualquier comando:  focusyn help <comando> [subcomando]   (o `focusyn <comando> --help`)
"""


@app.command(
    "help",
    context_settings={"ignore_unknown_options": True},
)
def help_cmd(
    command: Annotated[
        list[str] | None,
        typer.Argument(help="Comando (y subcomando) del que querés la ayuda. Vacío: la guía."),
    ] = None,
) -> None:
    """La guía por tarea, o la ayuda de un comando concreto (`focusyn help mcp install`)."""
    root = get_command(app)
    if not command:
        typer.echo(_GUIDE)
        return

    # El árbol se navega por PROTOCOLO (`get_command`/`list_commands`/`context_class`), no por
    # `isinstance(..., click.Group)` ni `click.Context(...)`: typer >=0.26 VENDORIZA click
    # (`typer._click`), así que el TyperGroup que devuelve `get_command()` no es instancia del
    # `click.Group` importable y su `get_help()` exige el Context de SU dialecto. Con isinstance,
    # `focusyn help mcp install` moría con "no es un comando" en cualquier instalación fresca
    # (typer sin techo de versión). `context_class` sale del propio comando → sirve con el click
    # vendorizado o con el externo.
    ctx: Any = root.context_class(root, info_name="focusyn")
    current: Any = root
    for token in command:
        group = current if hasattr(current, "get_command") else None
        sub = group.get_command(ctx, token) if group else None
        if sub is None:
            known = ", ".join(sorted(group.list_commands(ctx))) if group else ""
            typer.secho(
                f"✗ '{token}' no es un comando de `{ctx.info_name}`."
                + (f" Hay: {known}." if known else ""),
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        ctx = sub.context_class(sub, info_name=f"{ctx.info_name} {token}", parent=ctx)
        current = sub
    typer.echo(current.get_help(ctx))


if __name__ == "__main__":
    app()
