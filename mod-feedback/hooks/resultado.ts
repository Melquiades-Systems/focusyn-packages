// La respuesta de find_code/find_notes, venga como venga del motor: JSON en texto, bloques de
// contenido MCP o el objeto ya parseado.

export type Hit = {
  rank: number
  titulo: string
  ruta: string
  vault?: string
  path?: string
  docId?: string
}

export type Resultado = { queryId: string; tool: 'code' | 'notes'; hits: Hit[]; total: number }

const TOOL = /^mcp__.*focusyn.*__find_(code|notes)$/i

export function herramienta(tool: string): 'code' | 'notes' | null {
  const m = TOOL.exec(tool)
  return m ? (m[1]?.toLowerCase() as 'code' | 'notes') : null
}

function objeto(x: unknown): Record<string, unknown> | null {
  return x !== null && typeof x === 'object' && !Array.isArray(x)
    ? (x as Record<string, unknown>)
    : null
}

function texto(x: unknown): string {
  return typeof x === 'string' ? x : ''
}

function desenvolver(x: unknown, hondura = 0): Record<string, unknown> | null {
  if (hondura > 4) return null
  if (typeof x === 'string') {
    try {
      return desenvolver(JSON.parse(x), hondura + 1)
    } catch {
      return null
    }
  }
  if (Array.isArray(x)) {
    for (const bloque of x) {
      const o = objeto(bloque)
      const r = o && o.type === 'text' ? desenvolver(o.text, hondura + 1) : null
      if (r) return r
    }
    return null
  }
  const o = objeto(x)
  if (!o) return null
  if (Array.isArray(o.results) && typeof o.query_id === 'string') return o
  return (
    desenvolver(o.structuredContent, hondura + 1) ??
    desenvolver(o.content, hondura + 1) ??
    desenvolver(o.result, hondura + 1)
  )
}

export function leerResultado(tool: string, output: unknown): Resultado | null {
  const cual = herramienta(tool)
  const crudo = cual ? desenvolver(output) : null
  if (!cual || !crudo) return null
  const hits: Hit[] = []
  for (const [i, h] of (crudo.results as unknown[]).entries()) {
    const o = objeto(h)
    if (!o) continue
    const path = texto(o.path) || undefined
    const docId = texto(o.doc_id) || undefined
    const vault = texto(o.vault) || undefined
    const hit: Hit =
      cual === 'code'
        ? {
            rank: i + 1,
            titulo: texto(o.symbol),
            ruta: texto(o.location) || path || '',
            vault,
            path,
          }
        : {
            rank: i + 1,
            titulo: texto(o.title) || docId || '',
            ruta: [vault, path].filter(Boolean).join('/'),
            vault,
            docId,
          }
    if (hit.path || hit.docId) hits.push(hit)
  }
  return { queryId: crudo.query_id as string, tool: cual, hits, total: (crudo.results as unknown[]).length }
}

export function argumentos(r: Resultado, hit: Hit, voto: 'up' | 'down'): string[] {
  const argv = ['feedback', r.queryId, voto, '--rank', String(hit.rank), '--json']
  if (hit.path) argv.push('--path', hit.path)
  if (hit.docId) argv.push('--doc-id', hit.docId)
  if (hit.vault) argv.push('--vault', hit.vault)
  return argv
}

// La última línea con texto, sin el recuadro que typer dibuja alrededor del error.
export function motivo(stderr: string): string {
  const lineas = stderr
    .split('\n')
    .map(l => l.replace(/[╭╮╰╯│─]/g, '').replace(/^\s*✗\s*/, '').trim())
    .filter(Boolean)
  return lineas.pop() ?? ''
}
