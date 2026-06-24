import { lazy, Suspense, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  AppWindow,
  CheckCircle2,
  ChevronDown,
  FileCode2,
  FileText,
  Loader2,
  Palette,
  Wand2,
  Workflow,
  type LucideIcon,
} from "lucide-react"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import { MarkdownPreview } from "@/components/code/MarkdownPreview"
import { SelectionReviseTextarea } from "@/components/code/SelectionReviseTextarea"
import { StageHistoryDialog } from "@/components/code/StageHistoryDialog"
import { useCodeStore, type ReviseSectionArgs } from "@/stores/codeStore"

// The app preview pulls in an iframe + history lookups, so only load it when a
// card actually mounts it (after the build completes).
const CodeAppPreview = lazy(() => import("@/components/code/CodeAppPreview"))
// Full-stack pipeline (frontend + backend + middleware) lives alongside the
// frontend-only preview in the same "app" stage; lazy for the same reason.
const CodeFullstackPanel = lazy(() => import("@/components/code/CodeFullstackPanel"))

/** The stages that can surface as an inline artifact card in the conversation. */
export type ArtifactStage = "requirements" | "flow" | "documents" | "style" | "app"

/** Lifecycle of the card relative to the run: under review, settled, or neutral. */
export type ArtifactCardState = "review" | "done" | "idle"

const STAGE_ICON: Record<ArtifactStage, LucideIcon> = {
  requirements: FileText,
  flow: Workflow,
  documents: FileCode2,
  style: Palette,
  app: AppWindow,
}

interface StageArtifactCardProps {
  stage: ArtifactStage
  open: boolean
  onToggle: () => void
  state?: ArtifactCardState
}

/**
 * A collapsible artifact card embedded inline in the conversation. The header is
 * always shown (stage icon + title + status + chevron); expanding reveals the
 * editable / interactive content for that stage — a document editor, the UI
 * style picker with thumbnails, or the live app preview. Content is mounted only
 * while open so heavy panes (the app iframe) stay cheap when collapsed.
 *
 * It reads the Code project straight from the store, mirroring the artifact
 * editors that used to live in the right-hand preview pane.
 */
export function StageArtifactCard({ stage, open, onToggle, state = "idle" }: StageArtifactCardProps) {
  const { t } = useTranslation("code")
  const { t: tc } = useTranslation("common")

  const project = useCodeStore((s) => s.project)
  const styles = useCodeStore((s) => s.styles)
  const selectedStyleIds = useCodeStore((s) => s.selectedStyleIds)
  const isLoading = useCodeStore((s) => s.isLoading)
  const activeAction = useCodeStore((s) => s.activeAction)
  const updateProject = useCodeStore((s) => s.updateProject)
  const updateProjectDraft = useCodeStore((s) => s.updateProjectDraft)
  const setCurrentProject = useCodeStore((s) => s.setCurrentProject)
  const updateDocument = useCodeStore((s) => s.updateDocument)
  const updateDocumentDraft = useCodeStore((s) => s.updateDocumentDraft)
  const toggleStyle = useCodeStore((s) => s.toggleStyle)
  const generateStylePrompt = useCodeStore((s) => s.generateStylePrompt)
  const generatePreviews = useCodeStore((s) => s.generatePreviews)
  const reviseSection = useCodeStore((s) => s.reviseSection)

  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null)

  // View mode for Markdown document stages: default to rendered preview so the
  // user can read formatted headings/lists, switch to edit to make changes.
  const [viewMode, setViewMode] = useState<
    Record<"requirements" | "flow" | "documents" | "style", "edit" | "preview">
  >({
    requirements: "preview",
    flow: "preview",
    documents: "preview",
    style: "preview",
  })

  // Inline partial revision: rewrite only the user-selected span (asynchronous),
  // toast the outcome, and hand the changed range back so the textarea highlights
  // exactly what moved.
  const handleReviseSection = async (
    base: Pick<ReviseSectionArgs, "stage" | "documentId">,
    args: { selectedText: string; instruction: string; start: number; end: number }
  ) => {
    const res = await reviseSection({
      ...base,
      selectedText: args.selectedText,
      instruction: args.instruction,
      selectionStart: args.start,
      selectionEnd: args.end,
    })
    if (res.ok) {
      if (res.change) toast.success(t("partialRevise.applied"))
      else toast.info(t("partialRevise.unchanged"))
    } else {
      toast.error(res.message || t("partialRevise.failed"))
    }
    return { ok: res.ok, change: res.change }
  }

  const Icon = STAGE_ICON[stage]
  const subtitle =
    stage === "app"
      ? t("conversation.appCardHint")
      : state === "review"
        ? t("conversation.cardReviewHint")
        : t("conversation.cardDoneHint")

  const saveProjectField = async (data: Parameters<typeof updateProject>[0]) => {
    await updateProject(data)
    toast.success(t("toast.saved"))
  }

  const emptyState = (
    <div className="flex min-h-32 items-center justify-center rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
      {t("workspace.empty")}
    </div>
  )

  // --- per-stage body renderers (ported from the old preview pane) -----------

  const renderDoc = (
    value: string | null | undefined,
    onDraft: (next: string) => void,
    onSave: () => Promise<void>,
    historyStage: "requirements" | "flow"
  ) => {
    if (!project || !value) return emptyState
    const mode = viewMode[historyStage]
    return (
      <div className="space-y-3 sm:space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex gap-1">
            <Button
              variant={mode === "edit" ? "default" : "ghost"}
              size="sm"
              onClick={() =>
                setViewMode((prev) => ({ ...prev, [historyStage]: "edit" }))
              }
            >
              {tc("buttons.edit")}
            </Button>
            <Button
              variant={mode === "preview" ? "default" : "ghost"}
              size="sm"
              onClick={() =>
                setViewMode((prev) => ({ ...prev, [historyStage]: "preview" }))
              }
            >
              {tc("buttons.preview")}
            </Button>
          </div>
          <StageHistoryDialog projectId={project.id} stage={historyStage} onRestored={setCurrentProject} />
        </div>
        {mode === "edit" ? (
          <SelectionReviseTextarea
            value={value}
            onChange={(event) => onDraft(event.target.value)}
            rows={18}
            className="font-mono text-sm"
            disabled={isLoading}
            onReviseSelection={(args) => handleReviseSection({ stage: historyStage }, args)}
          />
        ) : (
          <div className="rounded-lg bg-muted/50 p-3 sm:p-4">
            <MarkdownPreview>{value}</MarkdownPreview>
          </div>
        )}
        {mode === "edit" && (
          <div className="flex justify-end">
            <Button onClick={() => void onSave()} disabled={isLoading}>
              {activeAction === "saveProject" && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {tc("buttons.saveChanges")}
            </Button>
          </div>
        )}
      </div>
    )
  }

  const renderDocuments = () => {
    if (!project?.documents?.length) return emptyState
    const selectedDocument =
      project.documents.find((document) => document.id === selectedDocumentId) ||
      project.documents[0] ||
      null
    return (
      <div className="space-y-3">
        <div className="flex justify-end">
          <StageHistoryDialog projectId={project.id} stage="documents" onRestored={setCurrentProject} />
        </div>
        <div className="grid gap-4 sm:grid-cols-[180px_minmax(0,1fr)]">
          <div className="flex gap-2 overflow-x-auto pb-1 sm:flex-col sm:overflow-visible sm:pb-0">
            {project.documents.map((document) => (
              <button
                key={document.id}
                type="button"
                onClick={() => setSelectedDocumentId(document.id)}
                className={cn(
                  "shrink-0 rounded-md border px-3 py-2 text-left text-sm transition-colors sm:w-full",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
                  selectedDocument?.id === document.id
                    ? "border-primary bg-primary/10 text-primary"
                    : "hover:bg-muted"
                )}
              >
                <span className="block truncate font-medium">{document.title}</span>
                <span className="block truncate text-xs text-muted-foreground">
                  {document.document_type}
                </span>
              </button>
            ))}
          </div>
          {selectedDocument && (
            <div className="min-w-0 space-y-3">
              <div className="flex items-center gap-2 text-sm font-medium">
                <FileCode2 className="h-4 w-4 shrink-0 text-primary" />
                <span className="truncate">{selectedDocument.title}</span>
              </div>
              <Tabs defaultValue="preview">
                <TabsList>
                  <TabsTrigger value="preview">{t("documents.preview")}</TabsTrigger>
                  <TabsTrigger value="content">{t("documents.content")}</TabsTrigger>
                  <TabsTrigger value="prompt">{t("documents.promptExpert")}</TabsTrigger>
                </TabsList>
                <TabsContent value="preview">
                  <div className="rounded-lg bg-muted/50 p-3 sm:p-4">
                    <MarkdownPreview>{selectedDocument.content}</MarkdownPreview>
                  </div>
                </TabsContent>
                <TabsContent value="content">
                  <SelectionReviseTextarea
                    value={selectedDocument.content}
                    onChange={(event) =>
                      updateDocumentDraft(selectedDocument.id, { content: event.target.value })
                    }
                    rows={14}
                    className="font-mono text-sm"
                    disabled={isLoading}
                    onReviseSelection={(args) =>
                      handleReviseSection(
                        { stage: "documents", documentId: selectedDocument.id },
                        args
                      )
                    }
                  />
                </TabsContent>
                <TabsContent value="prompt">
                  <Textarea
                    value={selectedDocument.prompt_expert}
                    onChange={(event) =>
                      updateDocumentDraft(selectedDocument.id, { prompt_expert: event.target.value })
                    }
                    rows={14}
                    className="font-mono text-sm"
                  />
                </TabsContent>
              </Tabs>
              <div className="flex justify-end">
                <Button
                  onClick={async () => {
                    await updateDocument(selectedDocument.id, selectedDocument)
                    toast.success(t("toast.documentSaved"))
                  }}
                  disabled={isLoading}
                >
                  {activeAction === "saveDocument" && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  {tc("buttons.saveChanges")}
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    )
  }

  const renderStyle = () => {
    if (!project) return emptyState
    const mode = viewMode.style
    return (
      <div className="space-y-4 sm:space-y-6">
        {/* History */}
        <div className="flex flex-wrap justify-end gap-2">
          <StageHistoryDialog
            projectId={project.id}
            stage="style"
            onRestored={setCurrentProject}
            triggerLabel={t("versions.styleHistory")}
          />
        </div>

        {/* Style selection */}
        <div className="grid gap-3 sm:grid-cols-2">
          {styles.map((style) => (
            <Label key={style.id} className="flex cursor-pointer gap-3 rounded-md border bg-card p-3">
              <Checkbox
                checked={selectedStyleIds.includes(style.id)}
                onCheckedChange={() => toggleStyle(style.id)}
              />
              <span className="min-w-0">
                <span className="block font-medium">{style.name}</span>
                <span className="block text-sm text-muted-foreground">{style.description}</span>
              </span>
            </Label>
          ))}
        </div>

        {/* Actions */}
        <div className="flex flex-wrap gap-3 border-t border-dashed pt-4">
          <Button
            variant="outline"
            onClick={() => void generateStylePrompt()}
            disabled={isLoading || selectedStyleIds.length === 0}
          >
            {activeAction === "style" && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t("style.generate")}
          </Button>
          <Button
            onClick={async () => {
              // When the image upstream is down the backend skips previews, adopts
              // the style prompt as the UI baseline and returns previewSkipped.
              const skipped = await generatePreviews()
              if (skipped) toast.info(t("preview.skipped"))
            }}
            disabled={isLoading || !project.style_prompt?.trim()}
          >
            {activeAction === "preview" ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Wand2 className="mr-2 h-4 w-4" />
            )}
            {t("preview.generate")}
          </Button>
        </div>

        {/* Style document editor / preview */}
        <div className="rounded-lg bg-muted/50 p-3 sm:p-4">
          <div className="mb-3 flex items-center justify-between">
            <span className="text-sm font-medium text-foreground">{t("style.documentTitle")}</span>
            <div className="inline-flex rounded-md bg-muted p-1">
              <Button
                variant={mode === "edit" ? "secondary" : "ghost"}
                size="sm"
                className="rounded-sm"
                onClick={() => setViewMode((prev) => ({ ...prev, style: "edit" }))}
              >
                {tc("buttons.edit")}
              </Button>
              <Button
                variant={mode === "preview" ? "secondary" : "ghost"}
                size="sm"
                className="rounded-sm"
                onClick={() => setViewMode((prev) => ({ ...prev, style: "preview" }))}
              >
                {tc("buttons.preview")}
              </Button>
            </div>
          </div>
          {mode === "edit" ? (
            <SelectionReviseTextarea
              value={project.style_prompt || ""}
              onChange={(event) => updateProjectDraft({ style_prompt: event.target.value })}
              placeholder={t("style.placeholder")}
              rows={8}
              className="font-mono text-sm"
              disabled={isLoading}
              onReviseSelection={(args) => handleReviseSection({ stage: "style" }, args)}
            />
          ) : (
            <MarkdownPreview className="max-h-[40vh]">
              {project.style_prompt || t("style.placeholder")}
            </MarkdownPreview>
          )}
        </div>
        {/* Generated preview thumbnails now render in the right-hand rail
            (PreviewThumbnailPanel), not inline under the conversation. */}
      </div>
    )
  }

  const renderBody = () => {
    switch (stage) {
      case "requirements":
        return renderDoc(
          project?.requirements_doc,
          (next) => updateProjectDraft({ requirements_doc: next }),
          () => saveProjectField({ requirements_doc: project?.requirements_doc }),
          "requirements"
        )
      case "flow":
        return renderDoc(
          project?.development_flow,
          (next) => updateProjectDraft({ development_flow: next }),
          () => saveProjectField({ development_flow: project?.development_flow }),
          "flow"
        )
      case "documents":
        return renderDocuments()
      case "style":
        return renderStyle()
      case "app":
        return (
          <Suspense
            fallback={
              <div className="flex min-h-48 items-center justify-center">
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              </div>
            }
          >
            <div className="space-y-4">
              <CodeAppPreview />
              <div className="border-t pt-4">
                <CodeFullstackPanel />
              </div>
            </div>
          </Suspense>
        )
    }
  }

  return (
    <div className="overflow-hidden rounded-xl border bg-card shadow-sm">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className={cn(
          "flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-muted/60",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
        )}
      >
        <span
          className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-full",
            state === "review" ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"
          )}
        >
          <Icon className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-foreground">
            {t(`workspace.tabs.${stage}`)}
          </span>
          <span className="block truncate text-xs text-muted-foreground">{subtitle}</span>
        </span>
        {state === "review" ? (
          <Badge variant="default" className="shrink-0">
            {t("conversation.reviewBadge")}
          </Badge>
        ) : state === "done" ? (
          <Badge variant="outline" className="shrink-0 gap-1 text-muted-foreground">
            <CheckCircle2 className="h-3 w-3 text-emerald-500" />
            {t("conversation.doneBadge")}
          </Badge>
        ) : null}
        <ChevronDown
          className={cn(
            "h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200",
            open && "rotate-180"
          )}
        />
      </button>
      {open && <div className="border-t p-3 sm:p-4">{renderBody()}</div>}
    </div>
  )
}

export default StageArtifactCard
