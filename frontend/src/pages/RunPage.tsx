import { useEffect, useReducer, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError, type RunView, type Step } from '../api/client'
import { followRun } from '../api/events'
import { ProgressView } from '../components/ProgressView'
import { ResultView } from '../components/result/ResultView'
import { Card } from '../components/ui'
import { initialProgress, progressReducer } from '../lib/progress'
import { NotFoundPage } from './NotFoundPage'

type Load =
  | { phase: 'loading' }
  | { phase: 'notfound' }
  | { phase: 'error'; message: string }
  | { phase: 'ready'; run: RunView; steps: Step[] | null }

const LIVE = new Set(['queued', 'running'])

export function RunPage() {
  const { id = '' } = useParams()
  const [load, setLoad] = useState<Load>({ phase: 'loading' })
  const [progress, dispatch] = useReducer(progressReducer, undefined, initialProgress)

  useEffect(() => {
    let live = true
    let stop: (() => void) | undefined

    async function fetchAll(): Promise<Load> {
      const run = await api.getRun(id)
      // A stopped run says which items failed and why; that detail is in its steps.
      const steps = run.status === 'stopped' ? (await api.getSteps(id).catch(() => null))?.steps ?? null : null
      return { phase: 'ready', run, steps }
    }
    const fail = (e: unknown): Load =>
      e instanceof ApiError && e.status === 404
        ? { phase: 'notfound' }
        : { phase: 'error', message: e instanceof ApiError ? e.message : 'Something went wrong. Please try again.' }

    fetchAll()
      .then((result) => {
        if (!live) return
        setLoad(result)
        if (result.phase === 'ready' && LIVE.has(result.run.status)) {
          stop = followRun(id, {
            onEvent: (event) => dispatch({ kind: 'event', event }),
            onQueued: ({ position }) => dispatch({ kind: 'queued', position }),
            onTerminal: () => {
              fetchAll()
                .then((done) => live && setLoad(done))
                .catch((e) => live && setLoad(fail(e)))
            },
          })
        }
      })
      .catch((e) => live && setLoad(fail(e)))

    return () => {
      live = false
      stop?.()
    }
  }, [id])

  if (load.phase === 'loading') {
    return (
      <p role="status" className="text-center text-stone-600">
        Loading…
      </p>
    )
  }
  if (load.phase === 'notfound') return <NotFoundPage />
  if (load.phase === 'error') {
    return (
      <Card role="alert" className="mx-auto max-w-xl text-center">
        <h1 className="text-xl font-semibold">We couldn&apos;t load this run</h1>
        <p className="mt-2 text-stone-700">{load.message}</p>
        <p className="mt-4">
          <Link className="font-medium text-accent-strong underline" to="/">
            Start a new plan
          </Link>
        </p>
      </Card>
    )
  }
  if (LIVE.has(load.run.status)) return <ProgressView progress={progress} status={load.run.status} />
  return <ResultView run={load.run} steps={load.steps} />
}
