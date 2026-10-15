import type { components } from './types'

type S = components['schemas']
export type RunView = S['RunView']
export type StepsView = S['StepsView']
export type Step = S['Step']
export type Recommendation = S['Recommendation']
export type Pick = S['Pick']
export type ComparisonItem = S['ComparisonItem']
export type IngredientItem = S['IngredientItem']
export type Flag = S['Flag']
export type Availability = S['Availability']
export type CreateRun = S['CreateRun']
export type PlannerConfig = S['PlannerConfig']
export type Condition = S['Condition']

export interface FieldError {
  field: string
  message: string
}

/** An error response from the API ({code, message}, plus fields on a 422). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly fields: FieldError[] = [],
    readonly extra: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    throw new ApiError(0, 'network', 'Could not reach the server. Check your connection and try again.')
  }
  if (response.ok) return (await response.json()) as T
  let body: Record<string, unknown> = {}
  try {
    body = (await response.json()) as Record<string, unknown>
  } catch {
    /* not JSON: fall through to a generic message */
  }
  const { code, message, fields, ...extra } = body
  throw new ApiError(
    response.status,
    typeof code === 'string' ? code : 'http_error',
    typeof message === 'string' ? message : `The server answered ${response.status}.`,
    Array.isArray(fields) ? (fields as FieldError[]) : [],
    extra,
  )
}

export const api = {
  availability: () => request<Availability>('/api/availability'),
  createRun: (body: CreateRun) =>
    request<S['RunAccepted']>('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  getRun: (id: string) => request<RunView>(`/api/runs/${encodeURIComponent(id)}`),
  getSteps: (id: string) => request<StepsView>(`/api/runs/${encodeURIComponent(id)}/steps`),
}
