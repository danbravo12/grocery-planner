import type { RunEvent } from '../api/events'

export type StepState = 'pending' | 'running' | 'done' | 'failed'
export type AgentName = 'ingredient' | 'price' | 'planner'

export interface StepProgress {
  state: StepState
  summary: string | null
}

export interface Progress {
  queuePosition: number | null
  steps: Record<AgentName, StepProgress>
  /** Set when the planner sent doubtful matches back to the price agent. */
  loopback: { items: number; reprice: StepProgress; planner2: StepProgress } | null
  /** The latest thing that happened, in words, for the aria-live region. */
  announcement: string
}

export const AGENTS: { name: AgentName; label: string; doing: string }[] = [
  { name: 'ingredient', label: 'Ingredient agent', doing: 'Building your shopping list' },
  { name: 'price', label: 'Price agent', doing: 'Matching products and prices at Coles and Woolworths' },
  { name: 'planner', label: 'Planner', doing: 'Deciding where it is worth shopping' },
]

const pending = (): StepProgress => ({ state: 'pending', summary: null })

export function initialProgress(): Progress {
  return {
    queuePosition: null,
    steps: { ingredient: pending(), price: pending(), planner: pending() },
    loopback: null,
    announcement: 'Waiting to start.',
  }
}

export type Action = { kind: 'event'; event: RunEvent } | { kind: 'queued'; position: number }

export function progressReducer(state: Progress, action: Action): Progress {
  if (action.kind === 'queued') {
    const where =
      action.position > 0 ? `Your plan is number ${action.position} in the queue.` : 'Your plan is starting.'
    return { ...state, queuePosition: action.position, announcement: where }
  }
  const { event } = action
  const agent = AGENTS.find((a) => a.name === event.name)
  const set = (name: AgentName, step: StepProgress, text: string): Progress => ({
    ...state,
    queuePosition: null,
    steps: { ...state.steps, [name]: step },
    announcement: text,
  })

  switch (event.type) {
    case 'step_started':
      if (agent) return set(agent.name, { state: 'running', summary: null }, `${agent.doing}.`)
      if (event.name === 'reprice' && state.loopback) {
        return {
          ...state,
          loopback: { ...state.loopback, reprice: { state: 'running', summary: null } },
          announcement: 'Re-checking the doubtful matches.',
        }
      }
      if (event.name === 'planner_2' && state.loopback) {
        return {
          ...state,
          loopback: { ...state.loopback, planner2: { state: 'running', summary: null } },
          announcement: 'Deciding again with the re-checked prices.',
        }
      }
      return state
    case 'step_completed':
      if (agent) {
        return set(agent.name, { state: 'done', summary: event.summary }, `${agent.label} finished.`)
      }
      if (event.name === 'reprice' && state.loopback) {
        return {
          ...state,
          loopback: { ...state.loopback, reprice: { state: 'done', summary: event.summary } },
          announcement: 'The doubtful matches have been re-checked.',
        }
      }
      if (event.name === 'planner_2' && state.loopback) {
        return {
          ...state,
          loopback: { ...state.loopback, planner2: { state: 'done', summary: event.summary } },
          announcement: 'Planner finished.',
        }
      }
      return state
    case 'loopback': {
      const ids = event.data['item_ids']
      return {
        ...state,
        loopback: { items: Array.isArray(ids) ? ids.length : 0, reprice: pending(), planner2: pending() },
        announcement: event.summary ?? 'Re-checking doubtful matches.',
      }
    }
    case 'run_completed':
      return { ...state, announcement: 'Your plan is ready.' }
    case 'run_failed':
      return { ...state, announcement: event.summary ?? 'The run did not finish.' }
  }
}
