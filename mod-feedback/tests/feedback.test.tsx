import type { On, ToolGroupCall } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

import { motivo } from '../hooks/resultado'

const QID = '5af7e692-59db-4948-95cf-050c3b6afb52'

const CODE = {
  query_id: QID,
  results: [
    { path: 'src/a.py', location: 'src/a.py:10-20', symbol: 'uno', vault: 'focusyn' },
    { path: 'src/b.py', location: 'src/b.py:1-5', symbol: null, vault: 'focusyn' },
  ],
}

const NOTES = {
  query_id: QID,
  results: [{ doc_id: 'DOC-DEC-001', path: 'proyectos/x.md', title: 'La línea', vault: 'notas' }],
}

function resultado(tool: string, output: unknown, requestId = 'tu-1') {
  return {
    plugin: 'focusyn-feedback',
    component: 'ToolResult',
    requestId,
    props: { tool_use_id: requestId, tool, output, isErrored: false },
  } as const
}

const SUPERFICIES = ['terminal', 'desktop', 'vscode'] as const

// Hace de motor: dibuja su propio nodo, que el mod mete en el árbol.
function motor(on: On) {
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>motor</Text>
  })
  return mock.clock(on, { now: 1_000_000 })
}

function corrida(exitCode: number, stdout: string, stderr = '') {
  return { value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } }
}

test('dibuja un renglón por hit, con la ruta y el puesto, en cada superficie', async ($, on) => {
  motor(on)
  for (const surface of SUPERFICIES) {
    const output = [{ type: 'text', text: JSON.stringify(CODE) }]
    const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', output), surface })
    const textos = await ui.findAll({ type: 'Text', text: /src\/a\.py:10-20/ })
    expect(textos.some(t => t.props.mono === true && t.text === 'src/a.py:10-20')).toBe(true)
    expect(await ui.find({ key: 'up-2' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /2 resultados · query 5af7e692/ })).toBeDefined()
    await ui.unmount()
  }
})

test('acepta el resultado como texto JSON, objeto o structuredContent, y del plugin', async ($, on) => {
  motor(on)
  for (const [tool, output] of [
    ['mcp__focusyn__find_notes', JSON.stringify(NOTES)],
    ['mcp__plugin_focusyn_focusyn__find_notes', NOTES],
    ['mcp__focusyn__find_notes', { content: [], structuredContent: NOTES }],
  ] as const) {
    const ui = await $.ui.mount({ ...resultado(tool, output), surface: 'desktop' })
    expect(await ui.find({ type: 'Text', text: /notas\/proyectos\/x\.md/ })).toBeDefined()
    await ui.unmount()
  }
})

test('otra tool, un error o una salida sin query_id quedan como los dibuja el motor', async ($, on) => {
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>motor</Text>
  })
  const casos = [
    resultado('mcp__focusyn__search_notes', CODE),
    resultado('mcp__focusyn__find_code', { results: [] }),
    { ...resultado('mcp__focusyn__find_code', CODE), props: { ...resultado('mcp__focusyn__find_code', CODE).props, isErrored: true } },
  ]
  for (const caso of casos) {
    const ui = await $.ui.mount({ ...caso, surface: 'desktop' })
    expect(await ui.find({ type: 'Text', text: 'motor' })).toBeDefined()
    expect(await ui.find({ key: 'up-1' })).toBeUndefined()
    await ui.unmount()
  }
})

test('el 👍 de find_code llama al CLI con el path y confirma en la fila', async ($, on) => {
  motor(on)
  mock.env(on, { HOME: '/home/x' })
  const llamadas: string[][] = []
  on('process.run', (_, e) => {
    llamadas.push([...e.argv])
    return corrida(0, '{"status":"recorded"}')
  })
  const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface: 'vscode' })
  await ui.press({ key: 'up-1' })
  expect(llamadas).toEqual([
    ['/home/x/.local/bin/focusyn', 'feedback', QID, 'up', '--rank', '1', '--json', '--path', 'src/a.py', '--vault', 'focusyn'],
  ])
  expect(await ui.find({ type: 'Text', text: '👍 anotado' })).toBeDefined()
  expect(await ui.find({ key: 'up-1' })).toBeUndefined()
  expect(await ui.find({ key: 'up-2' })).toBeDefined()
  await ui.unmount()
})

test('el 👎 de find_notes manda el doc_id y dice si ya estaba', async ($, on) => {
  motor(on)
  mock.env(on, {})
  const llamadas: string[][] = []
  on('process.run', (_, e) => {
    llamadas.push([...e.argv])
    return corrida(0, '{"status":"already_recorded"}')
  })
  const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_notes', NOTES), surface: 'terminal' })
  await ui.press({ key: 'down-1' })
  expect(llamadas[0]).toEqual([
    'focusyn', 'feedback', QID, 'down', '--rank', '1', '--json', '--doc-id', 'DOC-DEC-001', '--vault', 'notas',
  ])
  expect(await ui.find({ type: 'Text', text: '👎 ya estaba anotado' })).toBeDefined()
  await ui.unmount()
})

test('prueba primero el CLI de ~/.local/bin; sin él, el del PATH', async ($, on) => {
  motor(on)
  mock.env(on, { HOME: '/home/x' })
  const probados: string[] = []
  on('process.run', (_, e) => {
    probados.push(e.argv[0] ?? '')
    if (e.argv[0] !== 'focusyn') throw new Error('ENOENT')
    return corrida(0, '{"status":"recorded"}')
  })
  const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface: 'desktop' })
  await ui.press({ key: 'up-2' })
  expect(probados).toEqual(['/home/x/.local/bin/focusyn', 'focusyn'])
  expect(await ui.find({ type: 'Text', text: '👍 anotado' })).toBeDefined()
  await ui.unmount()
})

const SIN_COMANDO = [
  "Usage: focusyn [OPTIONS] COMMAND [ARGS]...",
  "╭─ Error ─────────────╮",
  "│ No such command 'feedback'. │",
  "╰─────────────────────╯",
].join('\n')

test('un CLI sin `feedback` (un venv viejo en el PATH) se salta, y si no hay otro lo dice', async ($, on) => {
  motor(on)
  mock.env(on, { HOME: '/home/x' })
  on('process.run', () => corrida(2, '', SIN_COMANDO))
  const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface: 'terminal' })
  await ui.press({ key: 'down-1' })
  expect(await ui.find({ type: 'Text', text: /uv tool upgrade focusyn-cli/ })).toBeDefined()
  await ui.unmount()
})

test('el motivo es la última línea con texto, sin el recuadro de typer', async () => {
  expect(motivo(SIN_COMANDO)).toBe("No such command 'feedback'.")
  expect(motivo('✗ 403: falta el scope\n')).toBe('403: falta el scope')
  expect(motivo('')).toBe('')
})

test('un fallo del CLI se lee en la fila y los botones quedan para reintentar', async ($, on) => {
  motor(on)
  mock.env(on, {})
  on('process.run', () => corrida(3, '', '✗ 403: te falta el scope propose\n'))
  const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface: 'desktop' })
  await ui.press({ key: 'up-1' })
  expect(await ui.find({ type: 'Text', text: '✗ 403: te falta el scope propose' })).toBeDefined()
  expect(await ui.find({ key: 'up-1' })).toBeDefined()
  await ui.unmount()
})

test('sin ningún CLI lo dice', async ($, on) => {
  motor(on)
  mock.env(on, {})
  on('process.run', () => {
    throw new Error('ENOENT')
  })
  const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface: 'desktop' })
  await ui.press({ key: 'down-1' })
  expect(await ui.find({ type: 'Text', text: /no encuentro el CLI/ })).toBeDefined()
  await ui.unmount()
})

test('los botones se revelan con el puntero sólo en la terminal fullscreen', async ($, on) => {
  motor(on)
  const casos = [
    ['terminal', { columns: 120, rows: 40, isFullscreen: true }, true],
    ['terminal', undefined, false],
    ['vscode', undefined, false],
  ] as const
  for (const [surface, viewport, oculto] of casos) {
    const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface, viewport })
    const fila = JSON.stringify((await ui.find({ key: 'hit-1' }))?.children)
    expect(fila.includes('"display":"none"')).toBe(oculto)
    await ui.unmount()
  }
})

test('el nodo del motor va siempre en el árbol: sin él VS Code no dibuja la fila', async ($, on) => {
  motor(on)
  for (const surface of SUPERFICIES) {
    const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface })
    expect(await ui.find({ type: 'Text', text: 'motor' })).toBeDefined()
    expect(await ui.find({ key: 'hit-1' })).toBeDefined()
    await ui.unmount()
  }
})

function grupo(calls: ToolGroupCall[], isExpanded = false) {
  return {
    plugin: 'focusyn-feedback',
    component: 'ToolGroup',
    requestId: 'g-1',
    props: { calls, isActive: false, isExpanded },
  } as const
}

function llamada(id: string, tool: string, output: unknown): ToolGroupCall {
  return { tool_use_id: id, tool, input: {}, isRunning: false, isErrored: false, isInterrupted: false, output }
}

test('en la terminal, el grupo plegado deja su línea y dibuja una lista por búsqueda', async ($, on) => {
  motor(on)
  mock.env(on, {})
  const llamadas: string[][] = []
  on('process.run', (_, e) => {
    llamadas.push([...e.argv])
    return corrida(0, '{"status":"recorded"}')
  })
  const calls = [
    llamada('tu-a', 'Grep', 'x'),
    llamada('tu-b', 'mcp__focusyn__find_code', JSON.stringify(CODE)),
    llamada('tu-c', 'mcp__focusyn__find_notes', JSON.stringify(NOTES)),
  ]
  const ui = await $.ui.mount({ ...grupo(calls), surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: 'motor' })).toBeDefined()
  expect(await ui.find({ key: '0-hit-2' })).toBeDefined()
  expect(await ui.find({ key: '1-hit-1' })).toBeDefined()
  await ui.press({ key: '1-up-1' })
  expect(llamadas[0]?.slice(0, 4)).toEqual(['focusyn', 'feedback', QID, 'up'])
  expect(llamadas[0]).toContain('DOC-DEC-001')
  await ui.unmount()
})

test('el grupo desplegado, sin búsquedas o fuera de la terminal queda como lo dibuja el motor', async ($, on) => {
  motor(on)
  const conBusqueda = [llamada('tu-b', 'mcp__focusyn__find_code', JSON.stringify(CODE))]
  for (const [calls, isExpanded, surface] of [
    [conBusqueda, true, 'terminal'],
    [[llamada('tu-a', 'Grep', 'x')], false, 'terminal'],
    [conBusqueda, false, 'vscode'],
  ] as const) {
    const ui = await $.ui.mount({ ...grupo([...calls], isExpanded), surface })
    expect(await ui.find({ type: 'Text', text: 'motor' })).toBeDefined()
    expect(await ui.find({ key: '0-hit-1' })).toBeUndefined()
    await ui.unmount()
  }
})

test('un timeout no se reintenta con el otro binario: el voto pudo haber llegado', async ($, on) => {
  const reloj = motor(on)
  mock.env(on, { HOME: '/home/x' })
  const probados: string[] = []
  on('process.run', async (_, e) => {
    probados.push(e.argv[0] ?? '')
    await reloj.advance(30_000)
    throw new Error('timeout')
  })
  const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface: 'desktop' })
  await ui.press({ key: 'up-1' })
  expect(probados).toEqual(['/home/x/.local/bin/focusyn'])
  expect(await ui.find({ type: 'Text', text: /no respondió en 30 s/ })).toBeDefined()
  await ui.unmount()
})

test('un «enviando» huérfano de una recarga devuelve los botones', async ($, on) => {
  motor(on)
  const huerfano = { '1': { voto: 'up', estado: 'enviando', desde: 1_000_000 - 120_000 } }
  on('state.get', () => ({ value: { value: { shape: 'votos-2', value: huerfano }, version: 1 } }))
  const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: /sin respuesta/ })).toBeDefined()
  expect(await ui.find({ key: 'up-1' })).toBeDefined()
  await ui.unmount()
})

test('reconoce la tool por el conector de claude.ai, sin importar mayúsculas', async ($, on) => {
  motor(on)
  const ui = await $.ui.mount({ ...resultado('mcp__claude_ai_Focusyn__find_code', CODE), surface: 'desktop' })
  expect(await ui.find({ key: 'hit-1' })).toBeDefined()
  await ui.unmount()
})

test('el nodo del motor llega intacto y sin caja que lo oculte, en cada superficie', async ($, on) => {
  mock.clock(on)
  const vistas: Record<string, unknown> = {}
  on('ui.render', ($, e) => {
    if (e.component === 'ToolResult') vistas[e.surface] = e.props.output
    const { Text } = $.ui.resolve(e)
    return <Text>motor</Text>
  })
  for (const surface of SUPERFICIES) {
    const ui = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface })
    const caja = JSON.stringify((await ui.findAll({ type: 'Box' }))[1]?.props ?? {})
    expect(caja.includes('"display"')).toBe(false)
    await ui.unmount()
  }
  for (const surface of SUPERFICIES) expect(vistas[surface]).toEqual(CODE)
})

test('la terminal sin puntero muestra el comando para votar sin mouse', async ($, on) => {
  motor(on)
  const term = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface: 'terminal' })
  expect(await term.find({ type: 'Text', text: `sin mouse: focusyn feedback ${QID} up|down --rank N --path <ruta>` })).toBeDefined()
  await term.unmount()
  const vs = await $.ui.mount({ ...resultado('mcp__focusyn__find_code', CODE), surface: 'vscode' })
  expect(await vs.find({ type: 'Text', text: /sin mouse/ })).toBeUndefined()
  await vs.unmount()
})
