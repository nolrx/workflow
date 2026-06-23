/** The right-pane live-preview tabs and their mapping to agent step keys. */
export type PreviewTab = "requirements" | "flow" | "documents" | "style" | "app"

export const PREVIEW_TABS: PreviewTab[] = ["requirements", "flow", "documents", "style", "app"]

/**
 * Workflows whose output lands in the "app" preview tab (the file-generation
 * stage). The current path is the containerized agent that builds a complete
 * multi-file project. The legacy single-file HTML workflow can no longer be
 * created (removed), but is kept here so older runs still replay onto this tab.
 */
export const FRONTEND_WORKFLOWS = new Set<string>([
  "code_frontend_project_generation", // containerized multi-file project (current)
  "code_frontend_generation", // legacy single-file HTML — creation removed, replay-only
])

export const isFrontendWorkflow = (workflow: string | null | undefined): boolean =>
  !!workflow && FRONTEND_WORKFLOWS.has(workflow)

/** agent step key -> the preview tab it produces (other steps have no tab). */
export const STEP_TAB: Record<string, PreviewTab> = {
  requirements: "requirements",
  flow: "flow",
  documents: "documents",
  style: "style",
  // frontend (file-generation) steps all focus the live app preview
  fe_planner: "app",
  fe_project_build: "app",
  fe_publish: "app",
  fe_build: "app",
  fe_critic: "app",
  fe_repair: "app",
}

/** Workflow progress.current_step -> preview tab (used to auto-follow the run). */
export const PROGRESS_TAB: Record<string, PreviewTab> = {
  requirements: "requirements",
  flow: "flow",
  documents: "documents",
  style: "style",
  preview: "style",
  // frontend generation streaming steps -> the live app preview tab
  build: "app",
  critic: "app",
  repair: "app",
  publish: "app",
}
