import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { router } from './router'
import { useTranslation } from 'react-i18next'

import { ThemeProvider } from 'next-themes'

const queryClient = new QueryClient()

function App() {
  const { i18n } = useTranslation()
  
  // Update document language for accessibility
  if (typeof document !== 'undefined') {
    document.documentElement.lang = i18n.language === 'zh-CN' ? 'zh' : 'en'
  }

  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>
  )
}

export default App
