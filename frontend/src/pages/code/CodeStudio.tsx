import { useEffect, useRef, useState } from "react"
import { useParams } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { AgentRunPanel } from "@/components/agent/AgentRunPanel"
import { CodeStepper } from "@/components/code/CodeStepper"
import { ConversationRail } from "@/components/code/ConversationRail"
import { FigmaExportDialog } from "@/components/code/FigmaExportDialog"
import { FigmaImportDialog } from "@/components/code/FigmaImportDialog"
import { GitHubRepoCard } from "@/components/code/GitHubRepoCard"
import { PreviewThumbnailPanel } from "@/components/code/PreviewThumbnailPanel"
import { AppLayout } from "@/components/layout/AppLayout"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { useAgentStore } from "@/stores/agentStore"
import { useCodeStore } from "@/stores/codeStore"

/**
 * Single-column conversational Code workspace: a stepper header tracks the
 * stages and the conversation below is the whole surface — the build is driven
 * through chat, and the live preview is folded into the transcript as inline,
 * collapsible artifact cards (streaming output, the UI-style picker with
 * thumbnails, and the app preview). The workflow pauses after each reviewed
 * document; the user approves or adjusts inline.
 */
export function CodeStudio() {
  const { t } = useTranslation("code")
  const { t: ta } = useTranslation("agent")

  const project = useCodeStore((state) => state.project)
  const selectedStyleIds = useCodeStore((state) => state.selectedStyleIds)
  const activeAction = useCodeStore((state) => state.activeAction)
  const error = useCodeStore((state) => state.error)
  const fetchStyles = useCodeStore((state) => state.fetchStyles)
  const loadProject = useCodeStore((state) => state.loadProject)
  const clearError = useCodeStore((state) => state.clearError)

  const startAgentRun = useAgentStore((state) => state.startRun)
  const resumeRun = useAgentStore((state) => state.resumeRun)
  const openLatestRunForResource = useAgentStore((state) => state.openLatestRunForResource)
  const resetAgentRun = useAgentStore((state) => state.reset)
  const openPanel = useAgentStore((state) => state.openPanel)
  const agentRun = useAgentStore((state) => state.run)
  const events = useAgentStore((state) => state.events)

  const [requirementInput, setRequirementInput] = useState("")

  const { projectId } = useParams<{ projectId?: string }>()
  // Which session's agent run is currently bound to the workspace. Keyed by a ref
  // (not a reactive guard on run.resource_id) because openRun briefly nulls the
  // run mid-load, which would otherwise re-fire the replay.
  const boundRunResourceRef = useRef<string | null>(null)

  useEffect(() => {
    fetchStyles()
  }, [fetchStyles])

  // Deep-link / session switch: when opened at /code/:projectId, load that project.
  useEffect(() => {
    if (projectId && project?.id !== projectId) {
      void loadProject(projectId)
    }
  }, [projectId, project?.id, loadProject])

  // ...and replay that session's latest agent run. The whole transcript (and the
  // inline document cards rendered inside it) is event-sourced off the run, so
  // without this a historical session would open to an empty conversation even
  // though loadProject already fetched its document content. On a blank studio
  // clear the ref so reopening a session reprocesses it; when already bound to
  // this session's live run (e.g. just started inline) keep the live run.
  useEffect(() => {
    if (!projectId) {
      boundRunResourceRef.current = null
      return
    }
    if (boundRunResourceRef.current === projectId) return
    if (agentRun?.resource_id === projectId) {
      boundRunResourceRef.current = projectId
      return
    }
    boundRunResourceRef.current = projectId
    void openLatestRunForResource(projectId).then((found) => {
      // Legacy / run-less project: drop any prior session's run so its transcript
      // doesn't bleed through onto this one.
      if (!found && boundRunResourceRef.current === projectId) resetAgentRun()
    })
  }, [projectId, agentRun?.resource_id, openLatestRunForResource, resetAgentRun])

  useEffect(() => {
    if (!error) return
    toast.error(error)
    clearError()
  }, [error, clearError])

  // Load the project's editable content into the cards whenever the run produces
  // or revises a document (a new awaiting-review event arrives) or settles — so
  // the inline artifacts always reflect the latest generated content.
  const reviewTick = events.filter((event) => event.event_type === "step_awaiting_review").length
  useEffect(() => {
    const rid = agentRun?.resource_id
    const status = agentRun?.status
    if (rid && (status === "paused" || status === "completed" || status === "partial")) {
      void loadProject(rid)
    }
  }, [agentRun?.resource_id, agentRun?.status, reviewTick, loadProject])

  const handleStart = async () => {
    const requirement = requirementInput.trim()
    if (!requirement) {
      toast.error(ta("swarm.requirementRequired"))
      return
    }
    try {
      await startAgentRun({
        domain: "code",
        workflow: "code_full_generation",
        resource_type: project ? "code_project" : undefined,
        resource_id: project?.id,
        config: { requirement, title: project?.title, style_ids: selectedStyleIds },
      })
    } catch (err) {
      const message =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        ta("toast.startFailed")
      toast.error(message)
    }
  }

  const handleApprove = async () => {
    try {
      await resumeRun("approve")
    } catch {
      toast.error(ta("toast.startFailed"))
    }
  }

  const handleRevise = async (instruction: string) => {
    try {
      await resumeRun("revise", instruction)
    } catch {
      toast.error(ta("toast.startFailed"))
    }
  }

  const handleNewProject = () => {
    useCodeStore.setState({ project: null, selectedStyleIds: [] })
    boundRunResourceRef.current = null
    resetAgentRun()
    setRequirementInput("")
  }

  // The preview thumbnails live in a right-hand rail that slides in once images
  // exist (or are being generated) — no longer folded under the conversation.
  const showThumbnails = (project?.preview_images?.length ?? 0) > 0 || activeAction === "preview"

  // Figma belongs to the UI-generation stage: only surface it once the project
  // has reached the preview / UI-baseline stage, so it never clutters or
  // interrupts the document stages (requirements / flow / documents / style).
  const inUiStage =
    !!project && ["preview_ready", "ui_confirmed"].includes(project.status)

  return (
    <AppLayout title={t("title")}>
      <AgentRunPanel />
      <div
        className={`mx-auto flex h-[calc(100vh-7.5rem)] min-h-0 w-full flex-col gap-3 transition-[max-width] duration-300 ${
          showThumbnails ? "max-w-6xl" : "max-w-4xl"
        }`}
      >
        {/* Stage progress header */}
        <div className="flex items-center gap-3 rounded-xl border bg-card px-3 py-2.5 sm:px-4 sm:py-3">
          <div className="min-w-0 flex-1">
            <CodeStepper />
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {/* GitHub auto-sync status (read-only): repo link + latest push. Renders
                nothing until the session has a repo or a sync event. */}
            {project && <GitHubRepoCard projectId={project.id} />}
            {/* Figma belongs to the UI stage: attach a whole design to drive the
                multi-file project generation. Hidden during the document stages. */}
            {inUiStage && <FigmaImportDialog projectId={project!.id} />}
            {/* Figma export: push the generated HTML into Figma as editable layers. */}
            {inUiStage && (
              <FigmaExportDialog
                projectId={project!.id}
                source="html"
                triggerLabel={t("figma.exportHtml")}
              />
            )}
            {agentRun && (
              <>
                <Badge variant={agentRun.status === "paused" ? "default" : "outline"}>
                  {ta(`status.${agentRun.status}`, { defaultValue: agentRun.status })}
                </Badge>
                <Button variant="ghost" size="sm" onClick={openPanel}>
                  {ta("panel.viewDetail")}
                </Button>
              </>
            )}
          </div>
        </div>

        {/* Two-column: left conversation, right preview thumbnails (slides in). */}
        <div className="flex min-h-0 flex-1 flex-col gap-3 lg:flex-row">
          <Card className="flex min-h-0 flex-1 flex-col overflow-hidden p-0">
            <ConversationRail
              requirementDraft={requirementInput}
              onRequirementChange={setRequirementInput}
              onStart={handleStart}
              onApprove={handleApprove}
              onRevise={handleRevise}
              onNewProject={handleNewProject}
            />
          </Card>
          {showThumbnails && (
            <div className="min-h-0 max-h-[45vh] shrink-0 duration-300 animate-in fade-in slide-in-from-right-8 lg:h-full lg:max-h-none lg:w-[22rem]">
              <PreviewThumbnailPanel />
            </div>
          )}
        </div>
      </div>
    </AppLayout>
  )
}

export default CodeStudio
