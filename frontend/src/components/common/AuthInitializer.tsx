import { useEffect, useState } from "react"
import { useAuthStore } from "@/stores/authStore"
import { tokenManager } from "@/api/client"
import { Loading } from "./Loading"

interface AuthInitializerProps {
  children: React.ReactNode
}

export function AuthInitializer({ children }: AuthInitializerProps) {
  const [isInitialized, setIsInitialized] = useState(false)
  const { fetchUser } = useAuthStore()

  useEffect(() => {
    const initAuth = async () => {
      const token = tokenManager.getAccessToken()
      if (token) {
        await fetchUser()
      }
      setIsInitialized(true)
    }

    initAuth()
  }, [fetchUser])

  if (!isInitialized) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Loading size="lg" />
      </div>
    )
  }

  return <>{children}</>
}
