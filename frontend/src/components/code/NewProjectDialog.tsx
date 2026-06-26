import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router-dom"
import { toast } from "sonner"
import { Bot, Loader2, Pencil, Sparkles, Wand2 } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { useAgentStore } from "@/stores/agentStore"
import { useCodeStore } from "@/stores/codeStore"
import { useTeamStore } from "@/stores/teamStore"

interface NewProjectDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function NewProjectDialog({ open, onOpenChange }: NewProjectDialogProps) {
  const { t } = useTranslation("code")
  const { t: ta } = useTranslation("agent")
  const navigate = useNavigate()
  const startAgentRun = useAgentStore((state) => state.startRun)
  const fetchProjects = useCodeStore((state) => state.fetchProjects)
  const teams = useTeamStore((state) => state.teams)
  const fetchTeams = useTeamStore((state) => state.fetchTeams)

  const [title, setTitle] = useState("")
  const [requirement, setRequirement] = useState("")
  const [isSubmitting, setIsSubmitting] = useState(false)
  // Where THIS session is created — a per-creation choice, NOT the global App Space
  // browsing scope (`scopeTeamId`). Defaults to 个人/独立 on every open and never
  // mutates the global scope, so each new session is independent unless the user
  // explicitly picks a team here. Reusing the sticky global scope made new sessions
  // silently land in whatever team the user last *browsed*, mixing them in with
  // existing members' sessions.
  const [targetTeamId, setTargetTeamId] = useState<string | null>(null)

  // Load teams when the dialog opens so the "create to" scope has options.
  useEffect(() => {
    if (open) void fetchTeams()
  }, [open, fetchTeams])

  const resetSession = () => {
    setTitle("")
    setRequirement("")
    setIsSubmitting(false)
    // Each new session starts independent (个人) — don't inherit the last choice.
    setTargetTeamId(null)
    useCodeStore.setState({ project: null, selectedStyleIds: [] })
    useAgentStore.getState().reset()
  }

  const handleOpenChange = (next: boolean) => {
    if (next) resetSession()
    if (!isSubmitting) onOpenChange(next)
  }

  const handleSubmit = async () => {
    const trimmed = requirement.trim()
    if (!trimmed) {
      toast.error(ta("swarm.requirementRequired"))
      return
    }

    setIsSubmitting(true)
    try {
      await startAgentRun({
        domain: "code",
        workflow: "code_full_generation",
        team_id: targetTeamId,
        config: { requirement: trimmed, title: title.trim() || undefined },
      })
      // Refresh the sidebar list so the new session appears as soon as the
      // backend creates the project; the CodeStudio auto-switch will also load
      // it once the run binds to a resource_id.
      void fetchProjects()
      onOpenChange(false)
      navigate("/code")
    } catch (error) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        ta("toast.startFailed")
      toast.error(message)
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault()
      void handleSubmit()
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="new-project-dialog-content animated-dialog-content gap-0 overflow-hidden border-0 p-0 sm:max-w-2xl"
        onPointerDownOutside={(event) => isSubmitting && event.preventDefault()}
        onEscapeKeyDown={(event) => isSubmitting && event.preventDefault()}
      >
        {/* Decorative top gradient — doubles as a visual anchor for the entrance. */}
        <div className="dialog-enter dialog-enter-1 relative h-2 bg-gradient-to-r from-primary via-purple-500 to-primary" />

        <div className="dialog-enter dialog-enter-2 grid gap-5 p-6 sm:p-8">
          <DialogHeader className="gap-3 text-center sm:text-left">
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10 shadow-sm sm:mx-0">
              <Sparkles className="h-6 w-6 text-primary" />
            </div>
            <div className="space-y-1.5">
              <DialogTitle className="text-xl font-semibold tracking-tight sm:text-2xl">
                {t("newProject.title")}
              </DialogTitle>
              <DialogDescription className="text-sm leading-relaxed">
                {t("newProject.description")}
              </DialogDescription>
            </div>
          </DialogHeader>

          <div className="dialog-enter dialog-enter-3 space-y-4">
            {teams.length > 0 && (
              <div className="space-y-2">
                <Label className="text-sm font-medium">
                  {t("newProject.scopeLabel")}
                </Label>
                <Select
                  value={targetTeamId ?? "personal"}
                  onValueChange={(v) =>
                    setTargetTeamId(v === "personal" ? null : v)
                  }
                  disabled={isSubmitting}
                >
                  <SelectTrigger className="rounded-lg border-input/80 bg-muted/30 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="personal">
                      {t("newProject.scopePersonal")}
                    </SelectItem>
                    {teams.map((team) => (
                      <SelectItem key={team.id} value={team.id}>
                        {team.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {t("newProject.scopeHelper")}
                </p>
              </div>
            )}
            <div className="space-y-2">
              <Label htmlFor="new-project-title" className="text-sm font-medium">
                {t("newProject.titleLabel")}
              </Label>
              <div className="relative">
                <Pencil className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="new-project-title"
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  placeholder={t("newProject.titlePlaceholder")}
                  disabled={isSubmitting}
                  className="rounded-lg border-input/80 bg-muted/30 pl-9 text-sm transition-colors focus:bg-background"
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="new-project-requirement" className="text-sm font-medium">
                {t("newProject.requirementLabel")}
              </Label>
              <Textarea
                id="new-project-requirement"
                value={requirement}
                onChange={(event) => setRequirement(event.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={t("newProject.placeholder")}
                rows={5}
                disabled={isSubmitting}
                className="resize-none rounded-lg border-input/80 bg-muted/30 text-sm transition-colors focus:bg-background"
                autoFocus
              />
            </div>
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Wand2 className="h-3.5 w-3.5" />
              {t("newProject.helper")}
            </p>
          </div>

          <div className="dialog-enter dialog-enter-4 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={isSubmitting}
              className="sm:w-auto"
            >
              {t("newProject.cancel")}
            </Button>
            <Button
              onClick={() => void handleSubmit()}
              disabled={!requirement.trim() || isSubmitting}
              className="sm:w-auto"
            >
              {isSubmitting ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Bot className="mr-2 h-4 w-4" />
              )}
              {isSubmitting ? t("newProject.starting") : t("newProject.start")}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default NewProjectDialog
