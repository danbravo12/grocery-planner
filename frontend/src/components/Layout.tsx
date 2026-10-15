import { Link, Outlet } from 'react-router-dom'

export function Layout() {
  return (
    <div className="flex min-h-screen flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-10 focus:rounded focus:bg-white focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <header className="border-b border-stone-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <Link to="/" className="flex items-center gap-2 font-semibold text-stone-900">
            <span aria-hidden className="grid size-7 place-items-center rounded-md bg-accent text-sm text-white">
              G
            </span>
            Grocery Planner
          </Link>
          <p className="hidden text-sm text-stone-600 sm:block">Where is it worth shopping this week?</p>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-5xl flex-1 px-4 py-6 sm:py-10">
        <Outlet />
      </main>
      <footer className="border-t border-stone-200 py-6 text-center text-sm text-stone-600">
        A portfolio project. Prices are seeded and fictional.
      </footer>
    </div>
  )
}
