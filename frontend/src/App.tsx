import { Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { FormPage } from './pages/FormPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { RunPage } from './pages/RunPage'

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<FormPage />} />
        <Route path="runs/:id" element={<RunPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}
