# focusyn — plugin de Claude Code

Instala de una sola vez el **MCP `focusyn`** (las 27 tools del gateway: leer/escribir el vault, buscar
notas y código, ADRs, grafo) y los **hooks de auto-captura de memorias**.

El plugin **no lleva servidor adentro**: el MCP corre en el gateway (`/mcp`, Streamable HTTP) y esto es
la configuración que lo apunta. Por eso instalar el plugin no baja dependencias de Python ni abre
procesos: es un cliente HTTP con un header.

## Instalación

```bash
# 1. el CLI (emite tu key de máquina y guarda la credencial en ~/.config/focusyn/config.toml 0600)
uv tool install "git+https://github.com/Melquiades-Systems/focusyn-packages#subdirectory=cli"
focusyn login                 # tu usuario de la web
focusyn agent create mcp-$(hostname) --scopes read,propose,apply,sync   # → imprime la API key

# 2. la key, en el entorno (el plugin la lee de ahí; NO se escribe en ningún JSON versionado)
echo 'export FOCUSYN_MCP_KEY=a2a_…' >> ~/.bashrc && . ~/.bashrc

# 3. la key del sync de memorias (scope ingest; el JWT del `login` NO sirve para el hook)
focusyn hooks install --emit-ingest-key    # emite la key ingest y la guarda en el config 0600
#   (o `focusyn hooks install --ingest-key <key>` si el owner te dio una; o export FOCUSYN_INGEST_KEY)

# 4. el plugin
claude plugin marketplace add Melquiades-Systems/focusyn-packages
claude plugin install focusyn@melquiades
```

En Claude Code, lo mismo se teclea `/plugin marketplace add …` + `/plugin install focusyn@melquiades`.

> **¿Sin plugin?** `focusyn mcp install` registra el MCP directamente (emite la key de máquina y corre
> el `claude mcp add` por vos). El plugin agrega, sobre eso, los hooks de memorias y la actualización
> por git. Las dos vías son equivalentes para el MCP; **no uses las dos a la vez** o tendrás el
> servidor `focusyn` registrado dos veces.

## Qué trae

| Componente | Archivo | Qué hace |
|---|---|---|
| MCP remoto | `.claude-plugin/plugin.json` (`mcpServers`) | `type: http` → `${FOCUSYN_GATEWAY_URL:-https://focusyn.melquiades.systems}/mcp/` con header `X-Agent-Key: ${FOCUSYN_MCP_KEY}` |
| Hooks | `hooks/hooks.json` | `SessionEnd` + `PreCompact` (ambos `async`) → `scripts/memory-sync.sh` |
| Sync | `scripts/memory-sync.sh` | Llama `focusyn memory sync --quiet`; log en `~/.claude/focusyn-memory-sync.log` |

## Variables de entorno

| Var | Obligatoria | Default | Para qué |
|---|---|---|---|
| `FOCUSYN_MCP_KEY` | **sí** (para el MCP) | — | API key de máquina (scopes `read,propose,apply,sync`). Si NO está definida en el entorno del proceso `claude`, el header viaja como el **literal** `${FOCUSYN_MCP_KEY}`: el MCP figura `✔ Connected` pero **toda tool da 401** (el gateway ya lo rechaza en el handshake con `AUTH_UNEXPANDED_PLACEHOLDER`). |
| `FOCUSYN_GATEWAY_URL` | no | `https://focusyn.melquiades.systems` | apuntar a otro gateway (p. ej. `http://localhost:7415` en dev) |
| `FOCUSYN_INGEST_KEY` | no | — | key del **sync de memorias** (scope `ingest`). Alternativa a guardarla en el config con `focusyn hooks install --emit-ingest-key`/`--ingest-key`. El hook NO puede usar el JWT del `login`. |

> **El sync de memorias necesita su propia key (scope `ingest`), distinta de `FOCUSYN_MCP_KEY`.**
> `focusyn login` deja un **JWT** en el config, y el hook de `memory sync` **no puede autenticar con
> JWT** → sin una key `ingest` el sync falla en silencio en cada compactación (es `async` y loguea a
> `~/.claude/focusyn-memory-sync.log`). Conseguila con `focusyn hooks install --emit-ingest-key` (la
> emite self-service y la guarda en `~/.config/focusyn/config.toml` 0600) o exportá `FOCUSYN_INGEST_KEY`.
> `focusyn doctor` avisa si los hooks están puestos pero la key ingest falta.

## Gotchas

- **La URL lleva barra final** (`/mcp/`): sin ella el gateway responde `307` y algunos clientes no
  siguen el redirect con el header puesto.
- **Los hooks corren con PATH mínimo** → `memory-sync.sh` busca el binario `focusyn` en
  `~/.local/bin` y compañía antes de rendirse; si no lo encuentra, lo dice en el log y sale 0 (un
  `PreCompact` que sale ≠ 0 **bloquearía la compactación**).
- El sync es **por máquina**: cada host empuja sus propios proyectos y el prune está acotado a sus
  slugs — las memorias de otra máquina no se pisan.
