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
import PPTHome from "@/pages/ppt/Home"
import PPTHistory from "@/pages/ppt/History"
import PPTEditor from "@/pages/ppt/Editor"
import OutlineEditor from "@/pages/ppt/OutlineEditor"
import DetailEditor from "@/pages/ppt/DetailEditor"
import SlidePreview from "@/pages/ppt/SlidePreview"
import RedBookHome from "@/pages/redbook/Home"
import RedBookHistory from "@/pages/redbook/History"
import TaskDetail from "@/pages/redbook/TaskDetail"

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

            {/* Dashboard */}
            <Route path="/dashboard" element={guarded(<Dashboard />)} />

            {/* PPT domain */}
            <Route path="/ppt" element={guarded(<PPTHome />)} />
            <Route path="/ppt/history" element={guarded(<PPTHistory />)} />
            <Route path="/ppt/project/:projectId" element={guarded(<PPTEditor />)} />
            <Route path="/ppt/project/:projectId/outline" element={guarded(<OutlineEditor />)} />
            <Route path="/ppt/project/:projectId/detail" element={guarded(<DetailEditor />)} />
            <Route path="/ppt/project/:projectId/preview" element={guarded(<SlidePreview />)} />

            {/* RedBook domain */}
            <Route path="/redbook" element={guarded(<RedBookHome />)} />
            <Route path="/redbook/history" element={guarded(<RedBookHistory />)} />
            <Route path="/redbook/task/:id" element={guarded(<TaskDetail />)} />

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
