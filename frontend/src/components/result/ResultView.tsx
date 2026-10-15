import type { RunView, Step } from '../../api/client'
import { ComparisonTable } from './ComparisonTable'
import { Notes } from './Notes'
import { RecommendationCard } from './RecommendationCard'
import { RunFooter } from './RunFooter'
import { ShoppingList } from './ShoppingList'
import { StoppedView } from './StoppedView'

/** A finished run: the recommendation first, then the evidence, then the list, like a receipt. */
export function ResultView({ run, steps }: { run: RunView; steps: Step[] | null }) {
  if (run.status === 'stopped' || run.status === 'failed' || !run.recommendation) {
    return <StoppedView run={run} steps={steps} />
  }
  return (
    <div className="space-y-6">
      <RecommendationCard run={run} />
      <ComparisonTable run={run} />
      {run.ingredient_list && <ShoppingList items={run.ingredient_list.items} />}
      <Notes run={run} />
      <RunFooter run={run} />
    </div>
  )
}
