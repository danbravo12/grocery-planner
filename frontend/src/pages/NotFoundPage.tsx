import { Link } from 'react-router-dom'
import { Card } from '../components/ui'

export function NotFoundPage() {
  return (
    <Card className="mx-auto max-w-xl text-center">
      <h1 className="text-2xl font-semibold">We can&apos;t find that page</h1>
      <p className="mt-2 text-stone-700">
        The link may be wrong, or the run is no longer available. Runs are only kept for a limited time.
      </p>
      <p className="mt-4">
        <Link className="font-medium text-accent-strong underline" to="/">
          Plan a new shop
        </Link>
      </p>
    </Card>
  )
}
