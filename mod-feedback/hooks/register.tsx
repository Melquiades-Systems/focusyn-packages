import { atom, memberOf, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderInput } from 'claude-code'

import type { FocusynFeedbackEstado, FocusynFeedbackVoto } from '../types'
import { argumentos, leerResultado, motivo } from './resultado'
import type { Hit, Resultado } from './resultado'

// Cambiar el tag cuando cambie la forma de un voto: un reload conserva lo guardado.
const votos = atom({ plugin: 'focusyn-feedback', key: 'votos' } as const, {}, { shape: 'votos-2' })

const LIMITE_MS = 30_000
// Un «enviando» más viejo que esto quedó huérfano (una recarga a mitad del voto): se puede reintentar.
const HUERFANO_MS = 60_000

const ETIQUETA: Record<FocusynFeedbackEstado['estado'], string> = {
  enviando: 'enviando…',
  anotado: 'anotado',
  repetido: 'ya estaba anotado',
  error: 'no se pudo',
}

// Primero el de `uv tool install`: el `focusyn` del PATH puede ser el de un venv activado,
// fijado a una versión sin `feedback`.
async function binarios($: EngineInterface): Promise<string[]> {
  const home = await $.env.get('HOME')
  return home ? [`${home}/.local/bin/focusyn`, 'focusyn'] : ['focusyn']
}

// El CLI ya tiene la URL y la credencial: el mod no ve ningún secreto.
async function registrar(
  $: EngineInterface,
  argv: string[],
  voto: FocusynFeedbackVoto,
): Promise<FocusynFeedbackEstado> {
  let viejo = false
  for (const bin of await binarios($)) {
    const t0 = await $.clock.now()
    let corrida
    try {
      corrida = await $.process.run([bin, ...argv], { timeoutMs: LIMITE_MS })
    } catch {
      // Un timeout no es «no está»: el voto pudo llegar, y reintentarlo con otro binario lo repite.
      if ((await $.clock.now()) - t0 >= LIMITE_MS) {
        return { voto, estado: 'error', detalle: `el CLI no respondió en ${LIMITE_MS / 1000} s` }
      }
      continue
    }
    const detalle = motivo(corrida.stderr)
    if (/No such command/.test(detalle)) {
      viejo = true
      continue
    }
    if (corrida.exitCode !== 0) {
      return { voto, estado: 'error', detalle: detalle || `salió ${corrida.exitCode}` }
    }
    try {
      const out = JSON.parse(corrida.stdout) as { status?: string }
      return { voto, estado: out.status === 'already_recorded' ? 'repetido' : 'anotado' }
    } catch {
      return { voto, estado: 'anotado' }
    }
  }
  return {
    voto,
    estado: 'error',
    detalle: viejo
      ? 'el CLI `focusyn` no tiene `feedback`: uv tool upgrade focusyn-cli'
      : 'no encuentro el CLI `focusyn` (uv tool install …)',
  }
}

// `pre` distingue las claves cuando un grupo dibuja varias listas.
async function lista($: EngineInterface, e: RenderInput, r: Resultado, requestId: string, pre = '') {
  const fila = memberOf(votos, { requestId })
  const estados = await read($, fila)
  const ahora = await $.clock.now()
  const enCurso = (s: FocusynFeedbackEstado | undefined, t: number) =>
    s?.estado === 'enviando' && t - (s.desde ?? 0) < HUERFANO_MS
  const { Box, Text, Button } = $.ui.resolve(e)
  // El revelado con el puntero sólo funciona en la terminal fullscreen (medido en B4): en VS Code
  // y en la terminal sin puntero los botones quedan a la vista, tenues hasta el hover.
  const revelar = e.surface === 'terminal' && e.viewport?.isFullscreen === true
  const sinMouse = e.surface === 'terminal' && !revelar

  const votar = (hit: Hit, voto: FocusynFeedbackVoto) => async () => {
    const clave = String(hit.rank)
    let ocupado = false
    const t = await $.clock.now()
    const enviando: FocusynFeedbackEstado = { voto, estado: 'enviando', desde: t }
    await update($, fila, v => {
      ocupado = enCurso(v[clave], t)
      return ocupado ? v : { ...v, [clave]: enviando }
    })
    if (ocupado) return
    const hecho = await registrar($, argumentos(r, hit, voto), voto)
    await update($, fila, v => ({ ...v, [clave]: hecho }))
  }

  const botones = (hit: Hit) => (
    <Box
      gap={1}
      display={revelar ? 'none' : 'flex'}
      hover={revelar ? { display: 'flex' } : undefined}
    >
      <Button key={`${pre}up-${hit.rank}`} plain dimColor label="👍" onPress={votar(hit, 'up')} />
      <Button key={`${pre}down-${hit.rank}`} plain dimColor label="👎" onPress={votar(hit, 'down')} />
    </Box>
  )

  const marca = (hit: Hit) => {
    const s = estados[String(hit.rank)]
    if (!s || s.estado === 'error' || (s.estado === 'enviando' && !enCurso(s, ahora))) {
      const detalle = s?.estado === 'enviando' ? 'sin respuesta' : s?.detalle
      return (
        <Box gap={1}>
          {s && (
            <Text color="error" wrap="truncate-end">
              ✗ {detalle ?? ETIQUETA.error}
            </Text>
          )}
          {botones(hit)}
        </Box>
      )
    }
    const pulgar = s.voto === 'up' ? '👍' : '👎'
    return (
      <Text color={s.estado === 'enviando' ? 'inactive' : 'success'}>
        {pulgar} {ETIQUETA[s.estado]}
      </Text>
    )
  }

  const pista = r.tool === 'code' ? '--path <ruta>' : '--doc-id <id>'
  return (
    <Box key={`${pre}lista`} flexDirection="column">
      {r.hits.map(hit => (
        <Box key={`${pre}hit-${hit.rank}`} gap={1}>
          <Text dimColor tabular>
            {String(hit.rank).padStart(2)}
          </Text>
          <Box flexShrink={1}>
            <Text wrap="truncate-middle">
              {hit.titulo ? `${hit.titulo}  ` : ''}
              <Text mono dimColor>
                {hit.ruta}
              </Text>
            </Text>
          </Box>
          {marca(hit)}
        </Box>
      ))}
      <Text dimColor>
        {r.total} resultados · query {r.queryId.slice(0, 8)}
      </Text>
      {sinMouse && (
        <Text dimColor wrap="truncate-end">
          sin mouse: focusyn feedback {r.queryId} up|down --rank N {pista}
        </Text>
      )}
    </Box>
  )
}

export const register: Register = on => {
  on('ui.render', { component: 'ToolResult' }, async ($, e, next) => {
    const r = e.props.isErrored ? null : leerResultado(e.props.tool, e.props.output)
    if (!r || r.hits.length === 0) return next(e)
    const { Box } = $.ui.resolve(e)
    // El nodo del motor va intacto (medido en B4): sin él VS Code no dibuja nada, reescribir su
    // salida marca la tarjeta como editada, y bajo un Box con `display` el motor rechaza el árbol.
    return (
      <Box flexDirection="column">
        {await next(e)}
        {await lista($, e, r, e.props.tool_use_id)}
      </Box>
    )
  })

  // La terminal pliega las búsquedas en una línea («called focusyn»): sin ToolResult que dibujar.
  // VS Code muestra cada llamada en su tarjeta: ahí dibuja el hook de arriba.
  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => {
    if (e.surface !== 'terminal' || e.props.isExpanded) return next(e)
    const busquedas = e.props.calls.flatMap(c => {
      const r = c.isErrored || !c.tool_use_id ? null : leerResultado(c.tool, c.output)
      return r && r.hits.length > 0 && c.tool_use_id ? [{ r, id: c.tool_use_id }] : []
    })
    if (busquedas.length === 0) return next(e)
    const { Box } = $.ui.resolve(e)
    return (
      <Box flexDirection="column">
        {await next(e)}
        {await Promise.all(busquedas.map(({ r, id }, i) => lista($, e, r, id, `${i}-`)))}
      </Box>
    )
  })
}
