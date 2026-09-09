import { Route, Routes } from 'react-router-dom'
import { Dashboard } from './pages/Dashboard'
import { RunDetail } from './pages/RunDetail'

function App() {
  return (
    <Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="/runs/:runId" element={<RunDetail />} />
    </Routes>
  )
}

export default App
