#!/usr/bin/env bash
# Empuja las memorias locales de Claude Code (~/.claude/projects/<proj>/memory/*.md) al corpus
# `claude-memory` del gateway. Lo dispara el plugin en SessionEnd y PreCompact (ambos `async`).
#
# Tres cosas que este script existe para resolver, y que un `"command": "focusyn memory sync"` pelado
# NO resuelve:
#   1. Los hooks corren con un PATH mínimo: `focusyn` (instalado por `uv tool`) suele NO estar en él.
#      Por eso se busca el binario en las ubicaciones reales antes de rendirse.
#   2. Nunca debe fallar ruidosamente: PreCompact con exit != 0 bloquea la compactación. Sale 0 pase
#      lo que pase; el diagnóstico va al log.
#   3. La credencial NO viaja acá: el CLI la lee de ~/.config/focusyn/config.toml (0600), que escribe
#      `focusyn login` / `focusyn init`. Este script no toca claves.
set -uo pipefail

EVENT="${1:-hook}"
LOG="${HOME}/.claude/focusyn-memory-sync.log"
mkdir -p "$(dirname "$LOG")"

log() { printf '[%s %s] %s\n' "$EVENT" "$(date '+%F %T')" "$*" >>"$LOG"; }

find_focusyn() {
  if command -v focusyn >/dev/null 2>&1; then command -v focusyn; return; fi
  for candidate in "${HOME}/.local/bin/focusyn" "${HOME}/.cargo/bin/focusyn" /usr/local/bin/focusyn; do
    [ -x "$candidate" ] && { printf '%s' "$candidate"; return; }
  done
  return 1
}

BIN="$(find_focusyn)" || {
  log "✗ no encuentro el binario 'focusyn'. Instalalo: uv tool install \"git+ssh://git@github.com/Melquiades-Systems/focusyn-packages#subdirectory=cli\""
  exit 0
}

log "sync con $BIN"
"$BIN" memory sync --quiet >>"$LOG" 2>&1 || log "✗ el sync falló (ver arriba)"
exit 0
