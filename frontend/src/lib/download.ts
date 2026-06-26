import { agentApi } from "@/api/agent"

/**
 * Download an artifact (source zip, etc.) through the authenticated API client.
 *
 * NOT via window.open(`?token=`): the artifact endpoint is @jwt_required with
 * JWT_TOKEN_LOCATION=[headers, json], so a query-string token is rejected (401).
 * apiClient injects the bearer header, so fetching as a blob + a synthetic anchor
 * click is the only thing that actually authenticates the download.
 */
export async function downloadArtifact(artifactId: string, filename: string): Promise<void> {
  const blob = await agentApi.downloadArtifact(artifactId)
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
