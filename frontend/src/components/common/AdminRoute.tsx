import { Navigate, useLocation } from "react-router-dom"
import { useAuthStore } from "@/stores/authStore"
import { Loading } from "./Loading"

interface AdminRouteProps {
  children: React.ReactNode
}

/** Guard that requires an authenticated user with the "admin" role.
 *  Non-admins are sent home; unauthenticated users to the login page. */
export function AdminRoute({ children }: AdminRouteProps) {
  const { isAuthenticated, isLoading, user } = useAuthStore()
  const location = useLocation()

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Loading size="lg" />
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  if (user?.role !== "admin") {
    return <Navigate to="/" replace />
  }

  return <>{children}</>
}
