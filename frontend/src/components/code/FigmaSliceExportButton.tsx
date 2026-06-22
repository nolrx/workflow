import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Loader2, Scissors } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { FigmaExportDialog } from "@/components/code/FigmaExportDialog"
import { useAgentStore } from "@/stores/agentStore"

/**
 * "Smart slice" export to Figma: starts a `code_figma_slice_generation` agent run
 * that analyses ONE preview thumbnail into an EDITABLE Design IR (text/vector/
 * sliced image) inside a sandboxed container. The run streams into the shared
 * agent timeline; once it completes, this surfaces the pairing-code dialog
 * (export source = "sliced", pinned to this run) so Figma rebuilds adjustable
 * layers instead of a single flat image.
 */
export function FigmaSliceExportButton({
  projectId,
  previewId,
}: {
  projectId: string
  previewId: string
}) {
  const { t } = useTranslation("code")
  const run = useAgentStore((s) => s.run)
  const startRun = useAgentStore((s) => s.startRun)
  const [startedRunId, setStartedRunId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)

  const isMine = !!startedRunId && run?.id === startedRunId
  const running = isMine && (run?.status === "queued" || run?.status === "running")
  const done = isMine && run?.status === "completed"

  const start = async () => {
    setStarting(true)
    try {
      const id = await startRun({
        domain: "code",
        workflow: "code_figma_slice_generation",
        resource_id: projectId,
        config: { preview_id: previewId },
      })
      setStartedRunId(id)
      toast.info(t("figma.sliceStarted"))
    } catch (error) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("figma.sliceFailed")
      toast.error(message)
    } finally {
      setStarting(false)
    }
  }

  if (done && startedRunId) {
    return (
      <FigmaExportDialog
        projectId={projectId}
        source="sliced"
        runId={startedRunId}
        triggerLabel={t("figma.sliceGetCode")}
      />
    )
  }

  return (
    <Button variant="outline" size="sm" onClick={() => void start()} disabled={starting || running}>
      {starting || running ? (
        <>
          <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
          {t("figma.slicing")}
        </>
      ) : (
        <>
          <Scissors className="mr-1.5 h-4 w-4" />
          {t("figma.sliceExport")}
        </>
      )}
    </Button>
  )
}

export default FigmaSliceExportButton
