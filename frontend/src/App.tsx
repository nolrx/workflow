import { BrowserRouter, Routes, Route } from "react-router-dom"
import { Toaster } from "sonner"
import { ErrorBoundary } from "@/components/common/ErrorBoundary"
import { AuthInitializer } from "@/components/common/AuthInitializer"
import { ProtectedRoute } from "@/components/common/ProtectedRoute"
import { Login } from "@/pages/auth/Login"
import { Register } from "@/pages/auth/Register"
import { TeamList } from "@/pages/team/TeamList"
import { TeamDashboard } from "@/pages/team/TeamDashboard"
import { Profile } from "@/pages/settings/Profile"
import { Billing } from "@/pages/settings/Billing"
import { Dashboard } from "@/pages/dashboard/Dashboard"
import { CodeStudio } from "@/pages/code/CodeStudio"
import { CodeCanvas } from "@/pages/code/CodeCanvas"
import { AppSpace } from "@/pages/apps/AppSpace"
import { AppDetail } from "@/pages/apps/AppDetail"
import { PromptAdmin } from "@/pages/admin/PromptAdmin"
import { QualityTrends } from "@/pages/admin/QualityTrends"
import { AdminRoute } from "@/components/common/AdminRoute"

/** Wrap a page element in the auth guard (keeps the route list readable). */
function guarded(element: React.ReactNode) {
  return <ProtectedRoute>{element}</ProtectedRoute>
}

function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <AuthInitializer>
          <Routes>
            {/* Auth routes (public) */}
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />

            {/* Code domain (default landing) */}
            <Route path="/" element={guarded(<CodeStudio />)} />
            <Route path="/code" element={guarded(<CodeStudio />)} />
            <Route path="/code/:projectId" element={guarded(<CodeStudio />)} />
            <Route path="/code/:projectId/canvas" element={guarded(<CodeCanvas />)} />

            {/* App Space (应用空间): manage + iterate deployed apps */}
            <Route path="/apps" element={guarded(<AppSpace />)} />
            <Route path="/apps/:projectId" element={guarded(<AppDetail />)} />
            <Route path="/apps/:projectId/iterate" element={guarded(<AppDetail />)} />

            {/* Dashboard */}
            <Route path="/dashboard" element={guarded(<Dashboard />)} />

            {/* Admin: prompt management + generation-quality trends (admin only) */}
            <Route
              path="/admin/prompts"
              element={
                <AdminRoute>
                  <PromptAdmin />
                </AdminRoute>
              }
            />
            <Route
              path="/admin/quality"
              element={
                <AdminRoute>
                  <QualityTrends />
                </AdminRoute>
              }
            />

            {/* Team routes */}
            <Route path="/team" element={guarded(<TeamList />)} />
            <Route path="/team/:slug" element={guarded(<TeamDashboard />)} />

            {/* Settings routes */}
            <Route path="/settings" element={guarded(<Profile />)} />
            <Route path="/settings/billing" element={guarded(<Billing />)} />
          </Routes>
          <Toaster />
        </AuthInitializer>
      </BrowserRouter>
    </ErrorBoundary>
  )
}

export default App
