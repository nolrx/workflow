/**
 * PPT Detail Editor Page
 * Edit descriptions and detailed content for PPT pages
 */
import { useEffect, useState, useCallback } from "react"
import { useParams, useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { usePPTStore, type PPTPage } from "@/stores/pptStore"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Textarea } from "@/components/ui/textarea"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import { ScrollArea } from "@/components/ui/scroll-area"
import { StatusBadge } from "@/components/ppt"
import {
  Loader2,
  ArrowLeft,
  ArrowRight,
  FileText,
  Sparkles,
  RefreshCw,
  Check,
} from "lucide-react"

interface DescriptionCardProps {
  page: PPTPage
  index: number
  isSelected: boolean
  isGenerating: boolean
  onSelect: () => void
  onRegenerate: () => void
  onDescriptionChange: (text: string) => void
}

function DescriptionCard({
  page,
  index,
  isSelected,
  isGenerating,
  onSelect,
  onRegenerate,
  onDescriptionChange,
}: DescriptionCardProps) {
  const title = page.outline_content?.title || `Page ${index + 1}`
  const points = page.outline_content?.points || []
  const description =
    page.description_content?.text ||
    page.description_content?.text_content?.join("\n\n") ||
    ""

  return (
    <Card
      className={`transition-all ${
        isSelected ? "ring-2 ring-primary" : "hover:shadow-md"
      }`}
    >
      <CardHeader className="pb-3 cursor-pointer" onClick={onSelect}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-full bg-primary/10 text-primary flex items-center justify-center text-sm font-medium">
              {index + 1}
            </div>
            <div>
              <CardTitle className="text-base">{title}</CardTitle>
              {page.part && (
                <Badge variant="outline" className="text-xs mt-1">
                  {page.part}
                </Badge>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <StatusBadge status={page.status} size="sm" />
            <Button
              variant="ghost"
              size="sm"
              onClick={(e) => {
                e.stopPropagation()
                onRegenerate()
              }}
              disabled={isGenerating}
            >
              {isGenerating ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <RefreshCw className="w-4 h-4" />
              )}
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-0">
        {/* Outline points */}
        {points.length > 0 && (
          <div className="mb-4 p-3 bg-muted/50 rounded-lg">
            <div className="text-xs font-medium text-muted-foreground mb-2">
              Outline Points
            </div>
            <ul className="text-sm space-y-1">
              {points.map((point, i) => (
                <li key={i} className="flex items-start">
                  <span className="mr-2 text-primary">•</span>
                  <span>{point}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Description textarea */}
        <Textarea
          value={description}
          onChange={(e) => onDescriptionChange(e.target.value)}
          placeholder="Enter detailed description for this slide..."
          rows={4}
          className="resize-none"
        />
      </CardContent>
    </Card>
  )
}

export default function DetailEditor() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  useTranslation("ppt")

  const {
    currentProject,
    fetchProject,
    isLoading,
    error,
    isGenerating,
    taskProgress,
    generationStep,
    generateDescriptions,
    regeneratePageDescription,
    updatePage,
    pollTaskStatus,
    clearError,
    clearGenerationState,
  } = usePPTStore()

  const [selectedPageId, setSelectedPageId] = useState<string | null>(null)
  const [regeneratingPageId, setRegeneratingPageId] = useState<string | null>(
    null
  )
  const [localDescriptions, setLocalDescriptions] = useState<
    Record<string, string>
  >({})

  useEffect(() => {
    if (projectId) {
      fetchProject(projectId)
    }
    return () => clearGenerationState()
  }, [projectId, fetchProject, clearGenerationState])

  // Initialize local descriptions from pages
  useEffect(() => {
    if (currentProject?.pages) {
      const descriptions: Record<string, string> = {}
      currentProject.pages.forEach((page) => {
        descriptions[page.id] =
          page.description_content?.text ||
          page.description_content?.text_content?.join("\n\n") ||
          ""
      })
      setLocalDescriptions(descriptions)
    }
  }, [currentProject?.pages])

  const pages = currentProject?.pages || []
  const selectedPage = pages.find((p) => p.id === selectedPageId)

  const handleGenerateDescriptions = useCallback(async () => {
    if (!projectId || isGenerating) return
    try {
      const taskId = await generateDescriptions(projectId)
      await pollTaskStatus(projectId, taskId)
    } catch (error) {
      console.error("Generate descriptions failed:", error)
    }
  }, [projectId, isGenerating, generateDescriptions, pollTaskStatus])

  const handleRegeneratePage = useCallback(
    async (pageId: string) => {
      if (!projectId || regeneratingPageId) return
      setRegeneratingPageId(pageId)
      try {
        await regeneratePageDescription(projectId, pageId)
        // Refresh project to get updated data
        await fetchProject(projectId)
      } catch (error) {
        console.error("Regenerate description failed:", error)
      } finally {
        setRegeneratingPageId(null)
      }
    },
    [projectId, regeneratingPageId, regeneratePageDescription, fetchProject]
  )

  const handleDescriptionChange = useCallback(
    (pageId: string, text: string) => {
      setLocalDescriptions((prev) => ({
        ...prev,
        [pageId]: text,
      }))
    },
    []
  )

  const handleSaveDescription = useCallback(
    async (pageId: string) => {
      if (!projectId) return
      const text = localDescriptions[pageId]
      try {
        await updatePage(projectId, pageId, {
          description_content: { text },
        })
      } catch (error) {
        console.error("Save description failed:", error)
      }
    },
    [projectId, localDescriptions, updatePage]
  )

  const progressPercent =
    taskProgress && taskProgress.total > 0
      ? (taskProgress.completed / taskProgress.total) * 100
      : 0

  if (isLoading && !currentProject) {
    return (
      <AppLayout>
        <div className="flex items-center justify-center h-[calc(100vh-200px)]">
          <Loader2 className="h-8 w-8 animate-spin text-primary" />
        </div>
      </AppLayout>
    )
  }

  if (error) {
    return (
      <AppLayout>
        <div className="container mx-auto max-w-4xl py-8">
          <div className="p-6 bg-destructive/10 text-destructive rounded-lg">
            <h2 className="font-semibold mb-2">Error</h2>
            <p>{error}</p>
            <Button variant="outline" className="mt-4" onClick={clearError}>
              Dismiss
            </Button>
          </div>
        </div>
      </AppLayout>
    )
  }

  if (!currentProject) {
    return (
      <AppLayout>
        <div className="container mx-auto max-w-4xl py-8 text-center">
          <p className="text-muted-foreground mb-4">Project not found</p>
          <Button variant="outline" onClick={() => navigate("/ppt")}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            Back to Home
          </Button>
        </div>
      </AppLayout>
    )
  }

  return (
    <AppLayout>
      <div className="-m-6 h-[calc(100vh-64px)] flex flex-col">
        {/* Header */}
        <header className="border-b px-6 py-3 flex items-center justify-between bg-background">
          <div className="flex items-center gap-4">
            <Button
              variant="ghost"
              size="icon"
              onClick={() => navigate(-1)}
            >
              <ArrowLeft className="h-5 w-5" />
            </Button>
            <div className="flex items-center gap-2">
              <span className="text-xl">📄</span>
              <span className="font-semibold">Edit Details</span>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={handleGenerateDescriptions}
              disabled={isGenerating || pages.length === 0}
            >
              {isGenerating && generationStep === "descriptions" ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Generating...
                </>
              ) : (
                <>
                  <Sparkles className="mr-2 h-4 w-4" />
                  Generate All
                </>
              )}
            </Button>
            <Button
              size="sm"
              onClick={() => navigate(`/ppt/project/${projectId}/preview`)}
            >
              Next
              <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
          </div>
        </header>

        {/* Progress bar */}
        {isGenerating && taskProgress && (
          <div className="px-6 py-2 bg-muted/50 border-b">
            <div className="flex items-center gap-4">
              <span className="text-sm text-muted-foreground">
                Generating descriptions...
              </span>
              <Progress value={progressPercent} className="flex-1 h-2" />
              <span className="text-sm text-muted-foreground">
                {taskProgress.completed}/{taskProgress.total}
              </span>
            </div>
          </div>
        )}

        {/* Main content */}
        <div className="flex-1 flex overflow-hidden">
          {/* Left - Description cards */}
          <ScrollArea className="flex-1">
            <div className="p-6 max-w-3xl mx-auto">
              {pages.length === 0 ? (
                <div className="text-center py-20">
                  <FileText className="w-16 h-16 mx-auto mb-4 text-muted-foreground/50" />
                  <h3 className="text-lg font-semibold mb-2">No pages yet</h3>
                  <p className="text-muted-foreground mb-4">
                    Go back to outline editor to add pages first
                  </p>
                  <Button
                    variant="outline"
                    onClick={() =>
                      navigate(`/ppt/project/${projectId}/outline`)
                    }
                  >
                    <ArrowLeft className="mr-2 h-4 w-4" />
                    Edit Outline
                  </Button>
                </div>
              ) : (
                <div className="space-y-4">
                  {pages.map((page, index) => (
                    <DescriptionCard
                      key={page.id}
                      page={page}
                      index={index}
                      isSelected={selectedPageId === page.id}
                      isGenerating={regeneratingPageId === page.id}
                      onSelect={() => setSelectedPageId(page.id)}
                      onRegenerate={() => handleRegeneratePage(page.id)}
                      onDescriptionChange={(text) =>
                        handleDescriptionChange(page.id, text)
                      }
                    />
                  ))}
                </div>
              )}
            </div>
          </ScrollArea>

          {/* Right - Preview */}
          <div className="hidden lg:block w-96 border-l bg-muted/30 overflow-auto">
            <div className="p-6">
              <h3 className="font-semibold mb-4">Preview</h3>
              {selectedPage ? (
                <div className="space-y-4">
                  <div>
                    <div className="text-sm text-muted-foreground mb-1">
                      Title
                    </div>
                    <div className="font-semibold">
                      {selectedPage.outline_content?.title || "Untitled"}
                    </div>
                  </div>

                  <div>
                    <div className="text-sm text-muted-foreground mb-1">
                      Description
                    </div>
                    <div className="text-sm whitespace-pre-wrap bg-background p-3 rounded-lg border">
                      {localDescriptions[selectedPage.id] ||
                        "No description yet..."}
                    </div>
                  </div>

                  <Button
                    size="sm"
                    variant="outline"
                    className="w-full"
                    onClick={() => handleSaveDescription(selectedPage.id)}
                  >
                    <Check className="mr-2 h-4 w-4" />
                    Save Changes
                  </Button>
                </div>
              ) : (
                <div className="text-center py-10 text-muted-foreground">
                  <div className="text-4xl mb-2">👆</div>
                  <p>Click a card to preview</p>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </AppLayout>
  )
}
