/** The right-pane live-preview tabs and their mapping to agent step keys. */
export type PreviewTab = "requirements" | "flow" | "documents" | "style" | "app"

export const PREVIEW_TABS: PreviewTab[] = ["requirements", "flow", "documents", "style", "app"]

/** agent step key -> the preview tab it produces (other steps have no tab). */
export const STEP_TAB: Record<string, PreviewTab> = {
  requirements: "requirements",
  flow: "flow",
  documents: "documents",
  style: "style",
}

/** Workflow progress.current_step -> preview tab (used to auto-follow the run). */
export const PROGRESS_TAB: Record<string, PreviewTab> = {
  requirements: "requirements",
  flow: "flow",
  documents: "documents",
  style: "style",
  preview: "style",
  // code_frontend_generation streaming steps -> the live app preview tab
  build: "app",
  critic: "app",
  repair: "app",
}
