import { useEffect, useRef, useState } from "react"
import { useParams } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { Bot, Loader2, Plus, Sparkles } from "lucide-react"
import { toast } from "sonner"
import { AgentRunPanel } from "@/components/agent/AgentRunPanel"
import { CodeAgentTimeline } from "@/components/code/CodeAgentTimeline"
import { CodePreviewPane } from "@/components/code/CodePreviewPane"
import { PROGRESS_TAB, isFrontendWorkflow, type PreviewTab } from "@/components/code/previewTabs"
import { AppLayout } from "@/components/layout/AppLayout"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Textarea } from "@/components/ui/textarea"
import { useAgentStore } from "@/stores/agentStore"
import { useCodeStore } from "@/stores/codeStore"

export function CodeStudio() {
  const { t } = useTranslation("code")
  const { t: ta } = useTranslation("agent")

  const project = useCodeStore((state) => state.project)
  const selectedStyleIds = useCodeStore((state) => state.selectedStyleIds)
  const error = useCodeStore((state) => state.error)
  const fetchStyles = useCodeStore((state) => state.fetchStyles)
  const loadProject = useCodeStore((state) => state.loadProject)
  const updateProjectDraft = useCodeStore((state) => state.updateProjectDraft)
  const clearError = useCodeStore((state) => state.clearError)

  const startAgentRun = useAgentStore((state) => state.startRun)
  const agentRun = useAgentStore((state) => state.run)
  const isStreaming = useAgentStore((state) => state.isStreaming)

  const [requirementInput, setRequirementInput] = useState("")
  const [manualTab, setManualTab] = useState<PreviewTab | null>(null)
  const loadedRunProjectRef = useRef<string | null>(null)

  const { projectId } = useParams<{ projectId?: string }>()

  useEffect(() => {
    fetchStyles()
  }, [fetchStyles])

  // Deep-link / session switch: when opened at /code/:projectId, load that project.
  useEffect(() => {
    if (projectId && project?.id !== projectId) {
      void loadProject(projectId)
    }
  }, [projectId, project?.id, loadProject])

  useEffect(() => {
    if (!error) return
    toast.error(error)
    clearError()
  }, [error, clearError])

  // While the run streams, the preview follows the active step; once it settles
  // the user can switch tabs freely. Derived state — no effect/setState needed.
  const currentStep = agentRun?.progress?.current_step
  const isFrontendRun = isFrontendWorkflow(agentRun?.workflow)
  const followTab: PreviewTab = isFrontendRun
    ? "app"
    : currentStep && PROGRESS_TAB[currentStep]
      ? PROGRESS_TAB[currentStep]
      : "requirements"
  const activeTab: PreviewTab = isStreaming ? followTab : manualTab ?? followTab

  // When a run finishes and produced a Code project, load it into the editor.
  useEffect(() => {
    if (
      agentRun &&
      agentRun.domain === "code" &&
      agentRun.resource_id &&
      (agentRun.status === "completed" || agentRun.status === "partial") &&
      loadedRunProjectRef.current !== agentRun.resource_id
    ) {
      loadedRunProjectRef.current = agentRun.resource_id
      void loadProject(agentRun.resource_id)
    }
  }, [agentRun, loadProject])

  const requirementValue = project?.requirement_input ?? requirementInput

  const handleRequirementChange = (value: string) => {
    if (project) updateProjectDraft({ requirement_input: value })
    else setRequirementInput(value)
  }

  const handleAgentGenerate = async () => {
    const requirement = requirementValue.trim()
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
        config: {
          requirement,
          title: project?.title,
          style_ids: selectedStyleIds,
        },
      })
      toast.success(ta("toast.started"))
    } catch (err) {
      const message =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        ta("toast.startFailed")
      toast.error(message)
    }
  }

  const handleNewProject = () => {
    loadedRunProjectRef.current = null
    useCodeStore.setState({ project: null, selectedStyleIds: [] })
    useAgentStore.setState({
      run: null,
      events: [],
      streamingByStep: {},
      selectedStepId: null,
      panelOpen: false,
    })
    setRequirementInput("")
    setManualTab(null)
  }

  return (
    <AppLayout title={t("title")}>
      <AgentRunPanel />
      <div className="grid gap-6 lg:h-[calc(100vh-7.5rem)] lg:grid-cols-[minmax(360px,400px)_minmax(0,1fr)]">
        {/* Left: requirement input + crewAI execution timeline */}
        <div className="flex min-h-0 flex-col gap-6 lg:overflow-y-auto">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-base">
                <Sparkles className="h-4 w-4 text-primary" />
                {t("input.title")}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <Textarea
                value={requirementValue}
                onChange={(event) => handleRequirementChange(event.target.value)}
                placeholder={t("input.placeholder")}
                rows={6}
                className="resize-none text-sm"
                disabled={isStreaming}
              />
              <div className="flex gap-2">
                <Button
                  className="flex-1"
                  onClick={handleAgentGenerate}
                  disabled={!requirementValue.trim() || isStreaming}
                >
                  {isStreaming ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <Bot className="mr-2 h-4 w-4" />
                  )}
                  {project || agentRun ? ta("swarm.rerun") : ta("swarm.button")}
                </Button>
                {(project || agentRun) && (
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={handleNewProject}
                    disabled={isStreaming}
                    title={t("workspace.newProject")}
                  >
                    <Plus className="h-4 w-4" />
                  </Button>
                )}
              </div>
              <p className="text-xs text-muted-foreground">{ta("swarm.description")}</p>
            </CardContent>
          </Card>

          <Card className="min-h-0">
            <CardHeader className="pb-3">
              <CardTitle className="text-base">{ta("panel.title")}</CardTitle>
            </CardHeader>
            <CardContent>
              <CodeAgentTimeline onSelectTab={setManualTab} />
            </CardContent>
          </Card>
        </div>

        {/* Right: live preview / editable artifacts */}
        <Card className="flex min-h-0 flex-col">
          <CardHeader className="flex flex-row items-center justify-between gap-2 pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              {t("workspace.previewTitle")}
            </CardTitle>
            <div className="flex items-center gap-2">
              {project && <Badge variant="secondary">{t(`status.${project.status}`)}</Badge>}
              {agentRun && !project && (
                <Badge variant="outline">
                  {ta(`status.${agentRun.status}`, { defaultValue: agentRun.status })}
                </Badge>
              )}
            </div>
          </CardHeader>
          <CardContent className="min-h-0 flex-1">
            <CodePreviewPane activeTab={activeTab} onTabChange={setManualTab} />
          </CardContent>
        </Card>
      </div>
    </AppLayout>
  )
}

export default CodeStudio
