# focusyn-packages

Todo lo que **se instala en la máquina del cliente** para hablar con [focusyn](https://github.com/Melquiades-Systems/focusyn),
el gateway de los vaults del ecosistema.

| Paquete | Qué es | Instalación |
|---|---|---|
| [`cli/`](cli/) | **`focusyn`**, el CLI cliente (Python, HTTP puro) | `uv tool install "git+ssh://git@github.com/Melquiades-Systems/focusyn-packages#subdirectory=cli"` |
| [`mcp/`](mcp/) | **plugin de Claude Code**: el MCP remoto + los hooks de memorias | `/plugin marketplace add Melquiades-Systems/focusyn-packages` → `/plugin install focusyn@melquiades` |

## Por qué existe este repo, y qué NO vive acá

La frontera es de **confianza y de despliegue**, no de conveniencia:

- **`focusyn` (el otro repo) es el SERVIDOR**: FastAPI + GraphRAG + Postgres/AGE, las credenciales, el
  commit+push a los vaults… y **la superficie MCP** (`src/focusyn/mcp_app.py`), que es un router más de
  ese ASGI — las tools llaman al gateway *in-process*, sin socket.
- **`focusyn-packages` (este repo) es el CLIENTE**: lo que un usuario instala en su laptop. Habla HTTP
  con el gateway y **el gateway autoriza por scopes** — un usuario normal y un admin corren el mismo
  binario; lo que cada uno puede hacer lo deciden sus scopes (403 donde no alcanza).

**El servidor MCP no está acá, y no debería estarlo.** El plugin de Claude Code no *contiene* un
servidor MCP: contiene la **configuración** que apunta al endpoint remoto (`type: http`, `url`,
`headers`). El servidor corre en el gateway. Moverlo a este repo obligaría al Dockerfile del gateway a
instalar un repo privado por `git+ssh` en el build y a versionar dos repos para tocar una tool.

Lo que sí garantiza la frontera, de forma ejecutable, es **`make cli-check`**: el wheel del cliente no
puede arrastrar ninguna dependencia del gateway (FastAPI, SQLAlchemy, pgvector, pygit2, gRPC, `mcp`…).
Si alguien mete un import del servidor en el cliente, el guard falla.

## Desarrollo

```bash
make install     # .venv en la raíz (workspace uv: los miembros comparten un lock)
make check       # ruff + mypy strict + pytest + cli-check + plugin-check
```

> **El lock de este repo NO lo comparte el gateway** — a propósito. Cuando `focusyn-cli` era miembro del
> workspace de focusyn, aquel `uv.lock` le pinneaba `typer 0.25`, mientras que cualquier `uv tool install`
> real resolvía `typer 0.26` (que **vendoriza click**) y rompía `focusyn help <comando>` sin que ningún
> test lo viera. Los artefactos de cliente se testean con las deps que resuelve **quien los instala**.
