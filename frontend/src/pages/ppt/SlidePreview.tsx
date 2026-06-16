/**
 * PPT Slide Preview Page
 * Preview generated slides and manage image generation
 */
import { useEffect, useState, useCallback } from "react"
import { useParams, useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { usePPTStore, type PPTPage } from "@/stores/pptStore"
import { useExportTasksStore } from "@/stores/exportTasksStore"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import { Checkbox } from "@/components/ui/checkbox"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  DropdownMenuSub,
  DropdownMenuSubTrigger,
  DropdownMenuSubContent,
} from "@/components/ui/dropdown-menu"
import { SlideCard, StatusBadge, ExportTasksPanel, ImageEditDialog } from "@/components/ppt"
import type { ExportFormat, ExportMode } from "@/api/ppt"
import {
  Loader2,
  ArrowLeft,
  Image as ImageIcon,
  FileDown,
  Sparkles,
  Check,
  RefreshCw,
  Grid,
  LayoutList,
  CheckSquare,
  Square,
  Wand2,
} from "lucide-react"

export default function SlidePreview() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  useTranslation("ppt")

  const {
    currentProject,
    fetchProject,
    isLoading,
    error,
    isGenerating,
    isExporting,
    isEditing,
    taskProgress,
    generationStep,
    selectedPageIds,
    generateImages,
    generateSelectedImages,
    regeneratePageImage,
    editPageImage,
    pollTaskStatus,
    exportProject,
    exportProjectAsync,
    togglePageSelection,
    selectAllPages,
    deselectAllPages,
    clearError,
    clearGenerationState,
  } = usePPTStore()

  const { addTask, pollTask } = useExportTasksStore()

  const [viewMode, setViewMode] = useState<"grid" | "list">("grid")
  const [selectedSlide, setSelectedSlide] = useState<PPTPage | null>(null)
  const [editingSlide, setEditingSlide] = useState<PPTPage | null>(null)
  const [regeneratingPageId, setRegeneratingPageId] = useState<string | null>(
    null
  )

  useEffect(() => {
    if (projectId) {
      fetchProject(projectId)
    }
    return () => clearGenerationState()
  }, [projectId, fetchProject, clearGenerationState])

  const pages = currentProject?.pages || []
  const selectedCount = selectedPageIds.size
  const hasImages = pages.some(
    (p) => p.generated_image_url || p.generated_image_path
  )

  const handleGenerateAllImages = useCallback(async () => {
    if (!projectId || isGenerating) return
    try {
      const taskId = await generateImages(projectId)
      await pollTaskStatus(projectId, taskId)
    } catch (error) {
      console.error("Generate images failed:", error)
    }
  }, [projectId, isGenerating, generateImages, pollTaskStatus])

  const handleGenerateSelectedImages = useCallback(async () => {
    if (!projectId || isGenerating || selectedCount === 0) return
    try {
      const taskId = await generateSelectedImages(projectId)
      await pollTaskStatus(projectId, taskId)
    } catch (error) {
      console.error("Generate selected images failed:", error)
    }
  }, [
    projectId,
    isGenerating,
    selectedCount,
    generateSelectedImages,
    pollTaskStatus,
  ])

  const handleRegenerateImage = useCallback(
    async (pageId: string) => {
      if (!projectId || regeneratingPageId) return
      setRegeneratingPageId(pageId)
      try {
        const taskId = await regeneratePageImage(projectId, pageId)
        await pollTaskStatus(projectId, taskId)
        await fetchProject(projectId)
      } catch (error) {
        console.error("Regenerate image failed:", error)
      } finally {
        setRegeneratingPageId(null)
      }
    },
    [projectId, regeneratingPageId, regeneratePageImage, pollTaskStatus, fetchProject]
  )

  const handleExport = useCallback(
    async (format: ExportFormat = "pptx", mode: ExportMode = "simple") => {
      if (!projectId || isExporting) return
      try {
        const result = await exportProject(projectId, { format, mode })
        window.open(result.download_url, "_blank")
      } catch (error) {
        console.error("Export failed:", error)
      }
    },
    [projectId, isExporting, exportProject]
  )

  const handleExportAsync = useCallback(
    async (format: ExportFormat = "pptx") => {
      if (!projectId || isExporting) return
      try {
        const result = await exportProjectAsync(projectId, format)
        // Add task to export store
        addTask({
          taskId: result.task_id,
          projectId,
          projectName:
            currentProject?.idea_prompt?.slice(0, 30) || "Untitled Project",
          format,
          status: "pending",
        })
        // Start polling
        pollTask(projectId, result.task_id)
      } catch (error) {
        console.error("Export failed:", error)
      }
    },
    [projectId, isExporting, exportProjectAsync, addTask, pollTask, currentProject]
  )

  const handleEditImage = useCallback(
    async (
      instruction: string,
      options?: { selectionMask?: string; materialImages?: string[] }
    ) => {
      if (!projectId || !editingSlide) {
        throw new Error("No project or slide selected")
      }
      const result = await editPageImage(projectId, editingSlide.id, instruction, options)
      // Update the editing slide with the new image
      setEditingSlide((prev) =>
        prev ? { ...prev, generated_image_url: result.page.generated_image_url } : null
      )
      // Also update selected slide if it's the same
      if (selectedSlide?.id === editingSlide.id) {
        setSelectedSlide((prev) =>
          prev ? { ...prev, generated_image_url: result.page.generated_image_url } : null
        )
      }
      return result
    },
    [projectId, editingSlide, selectedSlide, editPageImage]
  )

  const handleSlideClick = useCallback((page: PPTPage) => {
    setSelectedSlide(page)
  }, [])

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
              <span className="text-xl">🖼️</span>
              <span className="font-semibold">Preview Slides</span>
            </div>
            <Badge variant="outline">{pages.length} slides</Badge>
          </div>

          <div className="flex items-center gap-2">
            {/* View mode toggle */}
            <div className="flex border rounded-lg p-0.5">
              <Button
                variant={viewMode === "grid" ? "secondary" : "ghost"}
                size="sm"
                className="h-7 px-2"
                onClick={() => setViewMode("grid")}
              >
                <Grid className="h-4 w-4" />
              </Button>
              <Button
                variant={viewMode === "list" ? "secondary" : "ghost"}
                size="sm"
                className="h-7 px-2"
                onClick={() => setViewMode("list")}
              >
                <LayoutList className="h-4 w-4" />
              </Button>
            </div>

            {/* Selection controls */}
            {pages.length > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() =>
                  selectedCount === pages.length
                    ? deselectAllPages()
                    : selectAllPages()
                }
              >
                {selectedCount === pages.length ? (
                  <>
                    <CheckSquare className="mr-2 h-4 w-4" />
                    Deselect All
                  </>
                ) : (
                  <>
                    <Square className="mr-2 h-4 w-4" />
                    Select All
                  </>
                )}
              </Button>
            )}

            {/* Generate buttons */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={isGenerating || pages.length === 0}
                >
                  {isGenerating && generationStep === "images" ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Generating...
                    </>
                  ) : (
                    <>
                      <Sparkles className="mr-2 h-4 w-4" />
                      Generate
                    </>
                  )}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={handleGenerateAllImages}>
                  <ImageIcon className="mr-2 h-4 w-4" />
                  Generate All Images
                </DropdownMenuItem>
                {selectedCount > 0 && (
                  <DropdownMenuItem onClick={handleGenerateSelectedImages}>
                    <Check className="mr-2 h-4 w-4" />
                    Generate Selected ({selectedCount})
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>

            {/* Export button */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="sm" disabled={!hasImages || isExporting}>
                  {isExporting ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Exporting...
                    </>
                  ) : (
                    <>
                      <FileDown className="mr-2 h-4 w-4" />
                      Export
                    </>
                  )}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {/* Simple exports (images as background) */}
                <DropdownMenuSub>
                  <DropdownMenuSubTrigger>
                    <ImageIcon className="mr-2 h-4 w-4" />
                    Simple (Images)
                  </DropdownMenuSubTrigger>
                  <DropdownMenuSubContent>
                    <DropdownMenuItem onClick={() => handleExport("pptx", "simple")}>
                      Export as PPTX
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => handleExport("pdf", "simple")}>
                      Export as PDF
                    </DropdownMenuItem>
                  </DropdownMenuSubContent>
                </DropdownMenuSub>

                {/* Editable exports (with text extraction) */}
                <DropdownMenuSub>
                  <DropdownMenuSubTrigger>
                    <Wand2 className="mr-2 h-4 w-4" />
                    Editable (With Text)
                  </DropdownMenuSubTrigger>
                  <DropdownMenuSubContent>
                    <DropdownMenuItem onClick={() => handleExport("pptx", "editable")}>
                      Export as PPTX
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => handleExport("pdf", "editable")}>
                      Export as PDF
                    </DropdownMenuItem>
                  </DropdownMenuSubContent>
                </DropdownMenuSub>

                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => handleExportAsync("pptx")}>
                  Export PPTX (Background)
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>

            {/* Export tasks panel */}
            <ExportTasksPanel />
          </div>
        </header>

        {/* Progress bar */}
        {isGenerating && taskProgress && (
          <div className="px-6 py-2 bg-muted/50 border-b">
            <div className="flex items-center gap-4">
              <span className="text-sm text-muted-foreground">
                Generating images...
              </span>
              <Progress value={progressPercent} className="flex-1 h-2" />
              <span className="text-sm text-muted-foreground">
                {taskProgress.completed}/{taskProgress.total}
              </span>
            </div>
          </div>
        )}

        {/* Main content */}
        <ScrollArea className="flex-1">
          <div className="p-6">
            {pages.length === 0 ? (
              <div className="text-center py-20">
                <ImageIcon className="w-16 h-16 mx-auto mb-4 text-muted-foreground/50" />
                <h3 className="text-lg font-semibold mb-2">No slides yet</h3>
                <p className="text-muted-foreground mb-4">
                  Go back to add pages and generate content first
                </p>
                <Button
                  variant="outline"
                  onClick={() => navigate(`/ppt/project/${projectId}/outline`)}
                >
                  <ArrowLeft className="mr-2 h-4 w-4" />
                  Edit Outline
                </Button>
              </div>
            ) : viewMode === "grid" ? (
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
                {pages.map((page, index) => (
                  <SlideCard
                    key={page.id}
                    page={page}
                    index={index}
                    isSelected={selectedPageIds.has(page.id)}
                    isGenerating={regeneratingPageId === page.id}
                    onSelect={() => togglePageSelection(page.id)}
                    onRegenerateImage={() => handleRegenerateImage(page.id)}
                    onEdit={() => handleSlideClick(page)}
                  />
                ))}
              </div>
            ) : (
              <div className="space-y-3 max-w-4xl mx-auto">
                {pages.map((page, index) => {
                  const imageUrl =
                    page.generated_image_url ||
                    (page.generated_image_path
                      ? `/api/ppt/files${page.generated_image_path}`
                      : null)
                  return (
                    <Card
                      key={page.id}
                      className={`cursor-pointer transition-all ${
                        selectedPageIds.has(page.id)
                          ? "ring-2 ring-primary"
                          : "hover:shadow-md"
                      }`}
                      onClick={() => handleSlideClick(page)}
                    >
                      <CardContent className="p-4 flex gap-4">
                        {/* Checkbox */}
                        <div
                          className="flex-shrink-0 pt-2"
                          onClick={(e) => {
                            e.stopPropagation()
                            togglePageSelection(page.id)
                          }}
                        >
                          <Checkbox
                            checked={selectedPageIds.has(page.id)}
                          />
                        </div>

                        {/* Thumbnail */}
                        <div className="w-40 h-24 bg-muted rounded flex-shrink-0 overflow-hidden">
                          {imageUrl ? (
                            <img
                              src={imageUrl}
                              alt={page.outline_content?.title || "Slide"}
                              className="w-full h-full object-cover"
                            />
                          ) : (
                            <div className="w-full h-full flex items-center justify-center text-muted-foreground">
                              <ImageIcon className="h-8 w-8" />
                            </div>
                          )}
                        </div>

                        {/* Content */}
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 mb-1">
                            <span className="font-medium">Slide {index + 1}</span>
                            {page.part && (
                              <Badge variant="outline" className="text-xs">
                                {page.part}
                              </Badge>
                            )}
                            <StatusBadge
                              status={page.status}
                              size="sm"
                              showLabel={false}
                            />
                          </div>
                          <h3 className="font-medium truncate">
                            {page.outline_content?.title || "Untitled"}
                          </h3>
                          {page.description_content?.text && (
                            <p className="text-sm text-muted-foreground line-clamp-2 mt-1">
                              {page.description_content.text}
                            </p>
                          )}
                        </div>

                        {/* Actions */}
                        <div className="flex-shrink-0">
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={(e) => {
                              e.stopPropagation()
                              handleRegenerateImage(page.id)
                            }}
                            disabled={regeneratingPageId === page.id}
                          >
                            {regeneratingPageId === page.id ? (
                              <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                              <RefreshCw className="h-4 w-4" />
                            )}
                          </Button>
                        </div>
                      </CardContent>
                    </Card>
                  )
                })}
              </div>
            )}
          </div>
        </ScrollArea>
      </div>

      {/* Slide detail dialog */}
      <Dialog
        open={!!selectedSlide}
        onOpenChange={(open) => !open && setSelectedSlide(null)}
      >
        <DialogContent className="max-w-4xl max-h-[90vh] overflow-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {selectedSlide?.outline_content?.title || "Slide Details"}
              {selectedSlide?.part && (
                <Badge variant="outline" className="text-xs">
                  {selectedSlide.part}
                </Badge>
              )}
            </DialogTitle>
          </DialogHeader>

          {selectedSlide && (
            <div className="space-y-6">
              {/* Image preview */}
              <div className="aspect-video bg-muted rounded-lg overflow-hidden relative">
                {selectedSlide.generated_image_url ||
                selectedSlide.generated_image_path ? (
                  <img
                    src={
                      selectedSlide.generated_image_url ||
                      `/api/ppt/files${selectedSlide.generated_image_path}`
                    }
                    alt={selectedSlide.outline_content?.title || "Slide"}
                    className="w-full h-full object-contain"
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-muted-foreground">
                    <ImageIcon className="h-16 w-16" />
                  </div>
                )}
              </div>

              {/* Outline */}
              <div>
                <h3 className="font-semibold mb-2">Outline</h3>
                <div className="bg-muted/50 rounded-lg p-4">
                  <h4 className="font-medium mb-2">
                    {selectedSlide.outline_content?.title}
                  </h4>
                  {selectedSlide.outline_content?.points && (
                    <ul className="list-disc list-inside space-y-1 text-sm text-muted-foreground">
                      {selectedSlide.outline_content.points.map((point, i) => (
                        <li key={i}>{point}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>

              {/* Description */}
              {selectedSlide.description_content?.text && (
                <div>
                  <h3 className="font-semibold mb-2">Description</h3>
                  <div className="bg-muted/50 rounded-lg p-4">
                    <p className="text-sm whitespace-pre-wrap">
                      {selectedSlide.description_content.text}
                    </p>
                  </div>
                </div>
              )}

              {/* Actions */}
              <div className="flex justify-end gap-2">
                <Button
                  variant="outline"
                  onClick={() => {
                    setEditingSlide(selectedSlide)
                  }}
                  disabled={!selectedSlide.generated_image_url}
                >
                  <Wand2 className="mr-2 h-4 w-4" />
                  Edit Image
                </Button>
                <Button
                  variant="outline"
                  onClick={() => handleRegenerateImage(selectedSlide.id)}
                  disabled={regeneratingPageId === selectedSlide.id}
                >
                  {regeneratingPageId === selectedSlide.id ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Regenerating...
                    </>
                  ) : (
                    <>
                      <RefreshCw className="mr-2 h-4 w-4" />
                      Regenerate Image
                    </>
                  )}
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* Image edit dialog */}
      <ImageEditDialog
        open={!!editingSlide}
        onOpenChange={(open) => !open && setEditingSlide(null)}
        page={editingSlide}
        projectId={projectId || ""}
        isEditing={isEditing}
        onEdit={handleEditImage}
      />
    </AppLayout>
  )
}
