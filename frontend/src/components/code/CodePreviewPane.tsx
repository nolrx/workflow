import { lazy, Suspense, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, FileCode2, Loader2, Wand2 } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import { StreamingText } from "@/components/code/StreamingText"
import { DocumentSplitThinking } from "@/components/code/DocumentSplitThinking"
import { PREVIEW_TABS, type PreviewTab } from "@/components/code/previewTabs"
import { useAgentStore } from "@/stores/agentStore"
import { useCodeStore } from "@/stores/codeStore"

// Lazy-loaded: Sandpack's in-browser bundler is heavy (~1.3MB), so it only
// loads when the user actually opens the app preview tab.
const CodeAppPreview = lazy(() => import("@/components/code/CodeAppPreview"))

interface CodePreviewPaneProps {
  activeTab: PreviewTab
  onTabChange: (tab: PreviewTab) => void
}

/**
 * Right-pane live preview. While an agent step is running, the matching tab
 * shows the model output streaming token by token; once the run lands the
 * artifacts in the CodeProject, the same tab becomes an editable workspace.
 */
export function CodePreviewPane({ activeTab, onTabChange }: CodePreviewPaneProps) {
  const { t } = useTranslation("code")
  const { t: tc } = useTranslation("common")

  const run = useAgentStore((state) => state.run)
  const isStreaming = useAgentStore((state) => state.isStreaming)
  const streamingByStep = useAgentStore((state) => state.streamingByStep)

  const project = useCodeStore((state) => state.project)
  const styles = useCodeStore((state) => state.styles)
  const selectedStyleIds = useCodeStore((state) => state.selectedStyleIds)
  const isLoading = useCodeStore((state) => state.isLoading)
  const activeAction = useCodeStore((state) => state.activeAction)
  const updateProject = useCodeStore((state) => state.updateProject)
  const updateProjectDraft = useCodeStore((state) => state.updateProjectDraft)
  const updateDocument = useCodeStore((state) => state.updateDocument)
  const updateDocumentDraft = useCodeStore((state) => state.updateDocumentDraft)
  const toggleStyle = useCodeStore((state) => state.toggleStyle)
  const generateStylePrompt = useCodeStore((state) => state.generateStylePrompt)
  const generatePreviews = useCodeStore((state) => state.generatePreviews)
  const confirmPreview = useCodeStore((state) => state.confirmPreview)

  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null)

  /** Live streamed text for a step that is currently running, else null. */
  const liveFor = (key: PreviewTab): string | null => {
    const step = run?.steps?.find((item) => item.agent_key === key)
    if (step && step.status === "running") {
      return streamingByStep[step.id] ?? ""
    }
    return null
  }

  const saveProjectField = async (data: Parameters<typeof updateProject>[0]) => {
    await updateProject(data)
    toast.success(t("toast.saved"))
  }

  const selectedDocument =
    project?.documents.find((document) => document.id === selectedDocumentId) ||
    project?.documents[0] ||
    null

  const emptyState = (
    <div className="flex h-full min-h-48 items-center justify-center rounded-md border border-dashed p-10 text-center text-sm text-muted-foreground">
      {t("workspace.empty")}
    </div>
  )

  const streamingView = (text: string) => (
    <StreamingText text={text || t("workspace.streamingHint")} active={isStreaming} />
  )

  const renderRequirements = () => {
    const live = liveFor("requirements")
    if (live !== null) return streamingView(live)
    if (!project?.requirements_doc) return emptyState
    return (
      <div className="space-y-3">
        <Textarea
          value={project.requirements_doc || ""}
          onChange={(event) => updateProjectDraft({ requirements_doc: event.target.value })}
          rows={20}
          className="font-mono text-sm"
        />
        <div className="flex justify-end">
          <Button
            onClick={() => void saveProjectField({ requirements_doc: project.requirements_doc })}
            disabled={isLoading}
          >
            {activeAction === "saveProject" && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {tc("buttons.saveChanges")}
          </Button>
        </div>
      </div>
    )
  }

  const renderFlow = () => {
    const live = liveFor("flow")
    if (live !== null) return streamingView(live)
    if (!project?.development_flow) return emptyState
    return (
      <div className="space-y-3">
        <Textarea
          value={project.development_flow || ""}
          onChange={(event) => updateProjectDraft({ development_flow: event.target.value })}
          rows={20}
          className="font-mono text-sm"
        />
        <div className="flex justify-end">
          <Button
            onClick={() => void saveProjectField({ development_flow: project.development_flow })}
            disabled={isLoading}
          >
            {tc("buttons.saveChanges")}
          </Button>
        </div>
      </div>
    )
  }

  const renderDocuments = () => {
    // The split step streams raw JSON (noise to the user), so while it runs we
    // show a thinking animation instead of the live token text.
    if (liveFor("documents") !== null) return <DocumentSplitThinking />
    if (!project?.documents?.length) return emptyState
    return (
      <div className="grid gap-4 md:grid-cols-[200px_minmax(0,1fr)]">
        <div className="space-y-2">
          {project.documents.map((document) => (
            <button
              key={document.id}
              type="button"
              onClick={() => setSelectedDocumentId(document.id)}
              className={cn(
                "w-full rounded-md border px-3 py-2 text-left text-sm transition-colors",
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
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-sm font-medium">
              <FileCode2 className="h-4 w-4 text-primary" />
              {selectedDocument.title}
            </div>
            <Tabs defaultValue="content">
              <TabsList>
                <TabsTrigger value="content">{t("documents.content")}</TabsTrigger>
                <TabsTrigger value="prompt">{t("documents.promptExpert")}</TabsTrigger>
              </TabsList>
              <TabsContent value="content">
                <Textarea
                  value={selectedDocument.content}
                  onChange={(event) =>
                    updateDocumentDraft(selectedDocument.id, { content: event.target.value })
                  }
                  rows={16}
                  className="font-mono text-sm"
                />
              </TabsContent>
              <TabsContent value="prompt">
                <Textarea
                  value={selectedDocument.prompt_expert}
                  onChange={(event) =>
                    updateDocumentDraft(selectedDocument.id, { prompt_expert: event.target.value })
                  }
                  rows={16}
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
    )
  }

  const renderStyle = () => {
    const live = liveFor("style")
    if (live !== null) return streamingView(live)
    if (!project) return emptyState
    return (
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          {styles.map((style) => (
            <Label key={style.id} className="flex cursor-pointer gap-3 rounded-md border p-3">
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
        <div className="flex flex-wrap gap-3">
          <Button
            variant="outline"
            onClick={() => void generateStylePrompt()}
            disabled={isLoading || selectedStyleIds.length === 0}
          >
            {activeAction === "style" && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t("style.generate")}
          </Button>
          <Button
            onClick={() => void generatePreviews()}
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
        <Textarea
          value={project.style_prompt || ""}
          onChange={(event) => updateProjectDraft({ style_prompt: event.target.value })}
          placeholder={t("style.placeholder")}
          rows={10}
          className="font-mono text-sm"
        />
        {project.preview_images.length > 0 && (
          <div className="grid gap-3 sm:grid-cols-2">
            {project.preview_images.map((image) => (
              <div key={image.id} className="overflow-hidden rounded-md border">
                <img
                  src={image.url}
                  alt={t("preview.imageAlt")}
                  className="aspect-square w-full object-cover"
                />
                <div className="p-2">
                  <Button
                    className="w-full"
                    size="sm"
                    variant={project.confirmed_preview_url === image.url ? "secondary" : "default"}
                    onClick={() => void confirmPreview(image.url)}
                    disabled={isLoading}
                  >
                    {project.confirmed_preview_url === image.url ? (
                      <>
                        <CheckCircle2 className="mr-1.5 h-4 w-4" />
                        {t("preview.confirmed")}
                      </>
                    ) : (
                      t("preview.confirm")
                    )}
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    )
  }

  return (
    <Tabs
      value={activeTab}
      onValueChange={(value) => onTabChange(value as PreviewTab)}
      className="flex h-full flex-col"
    >
      <TabsList className="flex w-full flex-wrap justify-start">
        {PREVIEW_TABS.map((tab) => (
          <TabsTrigger key={tab} value={tab}>
            {t(`workspace.tabs.${tab}`)}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="requirements" className="min-h-0 flex-1 overflow-y-auto pt-3">
        {renderRequirements()}
      </TabsContent>
      <TabsContent value="flow" className="min-h-0 flex-1 overflow-y-auto pt-3">
        {renderFlow()}
      </TabsContent>
      <TabsContent value="documents" className="min-h-0 flex-1 overflow-y-auto pt-3">
        {renderDocuments()}
      </TabsContent>
      <TabsContent value="style" className="min-h-0 flex-1 overflow-y-auto pt-3">
        {renderStyle()}
      </TabsContent>
      <TabsContent value="app" className="min-h-0 flex-1 pt-3">
        <Suspense
          fallback={
            <div className="flex h-full min-h-64 items-center justify-center">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          }
        >
          <CodeAppPreview />
        </Suspense>
      </TabsContent>
    </Tabs>
  )
}

export default CodePreviewPane
