import { tokenManager } from "@/api/client"

/**
 * Open a deployed app's frontend in a new tab via the session-bound preview route
 * `/preview/<projectId>/`. A one-shot `?token=` proves ownership on entry; the
 * backend pins it into a path-scoped cookie and redirects to a token-less URL, so
 * the JWT never lingers in the address bar while relative asset/API requests stay
 * authenticated. Mirrors the helper used inside the Code Studio panels.
 */
export function openDeployedApp(projectId: string): void {
  const token = tokenManager.getAccessToken() ?? ""
  const url = `/preview/${encodeURIComponent(projectId)}/?token=${encodeURIComponent(token)}`
  window.open(url, "_blank", "noopener,noreferrer")
}
