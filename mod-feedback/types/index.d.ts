export type FocusynFeedbackVoto = 'up' | 'down'

export type FocusynFeedbackEstado = {
  voto: FocusynFeedbackVoto
  estado: 'enviando' | 'anotado' | 'repetido' | 'error'
  detalle?: string
  desde?: number
}

// Una entrada por puesto del hit (`"3"`), por fila de resultado.
export type FocusynFeedbackVotos = Record<string, FocusynFeedbackEstado>

declare module 'claude-code' {
  interface PluginState {
    'focusyn-feedback': { votos: StateFamily<Shaped<FocusynFeedbackVotos>> }
  }
}
