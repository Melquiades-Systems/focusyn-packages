# focusyn-cli

El CLI cliente de **focusyn**. Habla HTTP con el gateway; el gateway autoriza por scopes — un
usuario normal y un admin corren el mismo binario, y lo que cada uno puede hacer lo deciden sus
scopes (403 donde no alcanza).

Paquete **delgado** (`httpx` + `typer` + `pydantic` + `tomli-w`): **no** arrastra las dependencias
del gateway (FastAPI, SQLAlchemy, pgvector, pygit2, gRPC…). El `deadcode` es análisis estático 100%
local, no toca el gateway.

## Instalación (sin clonar el repo)

```bash
# 1. uv (no necesita Python previo)
curl -LsSf https://astral.sh/uv/install.sh | sh
#    Windows: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. el CLI, desde el repo de paquetes cliente (subdirectorio `cli/`)
uv tool install "git+ssh://git@github.com/Melquiades-Systems/focusyn-packages#subdirectory=cli"

# 3. autenticarte: con tu usuario de la web…
focusyn login
#    …o con una API key / el blob `focusyn-invite:…` que te pasa el owner
focusyn init --invite focusyn-invite:...

# 4. verificar
focusyn doctor

# 5. el MCP focusyn en Claude Code: emite la key de máquina y lo registra por vos
focusyn mcp install

# 6. automatizar el memory sync en Claude Code (sin key inline, sin editar JSON)
focusyn hooks install
```

¿Perdido? `focusyn help` es la guía por tarea; `focusyn help <comando>` la ayuda de cada uno.

Actualizar: `uv tool upgrade focusyn-cli`.

## Comandos

Todo lo que habla con el gateway lo **autoriza el gateway por scopes**: el mismo binario sirve a un
usuario normal y a un admin; donde no alcanza el scope, respondés `403`.

| Comando | Qué hace | Necesita |
|---|---|---|
| `focusyn help [comando]` | Guía por tarea; con argumento, la ayuda de ese (sub)comando | — |
| `focusyn init` | Configura credencial (API key) + perfil (`~/.config/focusyn/config.toml`, 0600) | URL del gateway |
| `focusyn login` | Login con el usuario de la web (JWT: tus scopes + tenant, auto-refresh) | usuario/contraseña |
| `focusyn doctor` | Diagnóstico: URL, credencial+scopes, versión, hooks, toolchain | — |
| `focusyn version` | Versión del cliente (+ del gateway) | — |
| `focusyn mcp install\|status\|uninstall` | Emite la key de máquina y registra el MCP focusyn en Claude Code | estar autenticado |
| `focusyn hooks install\|status\|uninstall` | Automatiza `memory sync` en Claude Code | binario en el PATH |
| `focusyn memory sync` | Reconcilia `~/.claude/projects/<proj>/memory/*.md` con el corpus | key scope `ingest` |
| `focusyn attachment upload` | Sube un binario local por streaming multipart | key scope `apply` |
| `focusyn read\|list\|search\|ask\|map` | Lectura del vault | scope `read` |
| `focusyn propose\|apply\|delete\|link` | Escritura del vault (para scripts/CI) | scope `propose`/`apply` |
| `focusyn vault {create,list,config,scaffold}` | Vaults | scope `vault`/`read` |
| `focusyn credential …` · `org create` · `tenant provision` · `usage …` | Administración | scope `credential`/`admin` |
| `focusyn deadcode` | Candidatos a dead code (100% local, no toca el gateway) | vulture/go/npx según el repo |

Para el hook de memory sync va una **API key** (`init`), no un login: el JWT es de vida corta y el
hook corre desatendido. Para uso interactivo, `login` es más cómodo (no tenés que emitir una key).

## `focusyn mcp install` — la key y el registro, de una

`focusyn mcp install` emite una **API key de máquina** (`POST /v1/agents`, acotada a un subconjunto
de *tus* scopes: no-escalada) y con ella corre el `claude mcp add` por vos. Es una key **por máquina**,
distinta de tu credencial personal: rotarla no te desloguea, y desactivarla (`focusyn agent disable
<name>`) apaga sólo esa máquina.

```bash
focusyn mcp install                 # agente `mcp-<hostname>`, scopes read,propose,apply,sync ∩ los tuyos
focusyn mcp install --rotate        # el agente ya existía: emite key nueva y re-registra
focusyn mcp install --use-key K     # registrar con una key ya emitida (no emite ninguna)
focusyn mcp status                  # qué hay registrado + valida esa key contra el gateway
focusyn mcp uninstall               # desregistra el server (la key sigue viva)
```

Si te faltan scopes, la key se emite **sin ellos** y el CLI te lo dice — pedir de más al gateway no es
un aviso, es un `403`. Tras instalar, reiniciá Claude Code (o abrí `/mcp`) para que tome el server.

## Configuración

`~/.config/focusyn/config.toml` (permisos 0600):

```toml
default_profile = "default"

[profiles.default]
gateway_url = "https://focusyn.melquiades.systems"
api_key = "a2a_…"           # opcional (larga vida; lo usa el hook de memory sync)
# access_token / refresh_token  # tras `focusyn login` (usuario de la web)
# tenant = "…"                  # multi-tenant
```

Precedencia: flag del comando > variable de entorno (`FOCUSYN_GATEWAY_URL`, `FOCUSYN_API_KEY`,
y por compat `FOCUSYN_INGEST_KEY`/`FOCUSYN_APPLY_KEY`) > perfil del config.

## `deadcode` — el detalle que importa

`deadcode` **nunca instala nada** ni corre en el gateway: analiza el working tree local con el
detector nativo de cada lenguaje (`vulture` para Python, `deadcode` de golang.org/x/tools para Go,
`knip` para TS). Si falta el toolchain, marca el proyecto como *omitido* — nunca presenta como
limpio lo que no revisó. Los backends de Go y TS **construyen** el proyecto (`go mod download` /
`npm install`), por eso este comando corre en tu máquina, no en la nube.
