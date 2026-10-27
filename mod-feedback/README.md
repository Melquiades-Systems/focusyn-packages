# focusyn-feedback

Mod de Claude Code: bajo cada resultado de `find_code` y `find_notes` del MCP focusyn dibuja un
renglón por hit con 👍/👎. El clic corre `focusyn feedback <query_id> up|down --rank N`, que registra
el voto con tu credencial del CLI. El mod no ve ningún secreto.

## Instalar

```
/plugin marketplace add Melquiades-Systems/focusyn-packages
/plugin install focusyn-feedback@melquiades
```

Funciona con el MCP registrado por el plugin `focusyn` o por `focusyn mcp install`: el mod no
registra el MCP, así que no lo duplica.

Necesita el CLI con el comando `feedback` (`uv tool upgrade focusyn-cli`), logueado con un usuario
o una key con scope `propose`. Busca primero `~/.local/bin/focusyn` y después el del `PATH`.

## Dónde dibuja

| Superficie | Cómo |
|---|---|
| Panel de VS Code | en la tarjeta, debajo del JSON del resultado; 👍/👎 siempre a la vista, tenues |
| Terminal fullscreen | debajo de la línea plegada («Called focusyn»); 👍/👎 al pasar el puntero |
| Terminal sin fullscreen | igual, con 👍/👎 a la vista y al pie el comando para votar sin mouse |

## Desarrollo

```
claude plugin validate mod-feedback
claude plugin test mod-feedback
claude --plugin-dir mod-feedback      # lo carga y lo recarga al guardar
```

`tsconfig.json` hereda de `.claude-plugin/types/`, que escribe el motor al cargar el mod (y que git
ignora): para que `tsc` y el editor lo tipen, cargalo una vez con `--plugin-dir`.
