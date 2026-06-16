import { StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import '@/i18n'
import App from './App.tsx'
import { PageLoading } from '@/components/common/Loading'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Suspense fallback={<PageLoading />}>
      <App />
    </Suspense>
  </StrictMode>,
)
