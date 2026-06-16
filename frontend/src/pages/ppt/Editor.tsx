/**
 * PPT Studio Editor Page
 * Main project editor with outline, pages, and preview
 */
import { useEffect, useCallback, useState } from "react"
import { useParams, useNavigate, useLocation } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { usePPTStore, type PPTPage } from "@/stores/pptStore"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Loader2,
  ArrowLeft,
  Image as ImageIcon,
  FileDown,
  Settings,
  ChevronRight,
  Sparkles,
  RefreshCw,
  ListOrdered,
  FileText,
  Eye,
} from "lucide-react"

function getStatusColor(status: string) {
  switch (status) {
    case "COMPLETED":
      return "bg-green-500"
    case "PROCESSING":
    case "GENERATING":
      return "bg-blue-500"
    case "FAILED":
      return "bg-red-500"
    default:
      return "bg-gray-400"
  }
}

interface PageCardProps {
  page: PPTPage
  index: number
  t: any
  onClick?: () => void
}

function PageCard({ page, index, t, onClick }: PageCardProps) {
  const imageUrl = page.generated_image_url || (page.generated_image_path ? `/api/ppt/files${page.generated_image_path}` : null)

  return (
    <Card className="group cursor-pointer hover:shadow-md transition-shadow" onClick={onClick}>
      <CardContent className="p-4">
        <div className="flex gap-4">
          {/* Thumbnail */}
          <div className="w-32 h-20 bg-muted rounded flex-shrink-0 overflow-hidden">
            {imageUrl ? (
              <img
                src={imageUrl}
                alt={t('editor.pages.label', { index: index + 1 })}
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
              <span className="font-medium">{t('editor.pages.label', { index: index + 1 })}</span>
              {page.part && (
                <Badge variant="outline" className="text-xs">
                  {page.part}
                </Badge>
              )}
              <div
                className={`w-2 h-2 rounded-full ${getStatusColor(page.status)}`}
              />
            </div>
            <h3 className="font-medium truncate">
              {page.outline_content?.title || "Untitled"}
            </h3>
            {page.outline_content?.points && (
              <p className="text-sm text-muted-foreground truncate">
                {page.outline_content.points.slice(0, 2).join(" | ")}
              </p>
            )}
          </div>

          {/* Arrow */}
          <div className="flex items-center opacity-0 group-hover:opacity-100 transition-opacity">
            <ChevronRight className="h-5 w-5 text-muted-foreground" />
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

export default function PPTEditor() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const { t } = useTranslation('ppt')

  // Determine current step from URL
  const currentStep = location.pathname.includes('/outline')
    ? 'outline'
    : location.pathname.includes('/detail')
    ? 'detail'
    : location.pathname.includes('/preview')
    ? 'preview'
    : 'editor'
  const {
    currentProject,
    fetchProject,
    isLoading,
    error,
    isGenerating,
    isExporting,
    generationStep,
    taskProgress,
    generateAll,
    generateImages,
    pollTaskStatus,
    exportProject,
    clearGenerationState,
  } = usePPTStore()

  const [selectedPage, setSelectedPage] = useState<PPTPage | null>(null)
  const [isRegenerating, setIsRegenerating] = useState(false)

  useEffect(() => {
    if (projectId) {
      fetchProject(projectId)
    }
    return () => {
      clearGenerationState()
    }
  }, [projectId, fetchProject, clearGenerationState])

  const handleGenerate = useCallback(async () => {
    if (!projectId || isGenerating) return
    try {
      await generateAll(projectId)
    } catch (error) {
      console.error("Generation failed:", error)
    }
  }, [projectId, isGenerating, generateAll])

  const handleRegeneratePage = useCallback(async (pageId: string) => {
    if (!projectId || isRegenerating) return
    setIsRegenerating(true)
    try {
      const taskId = await generateImages(projectId, "zh", [pageId])
      await pollTaskStatus(projectId, taskId)
      // Refresh the selected page data
      await fetchProject(projectId)
      // Update selected page from refreshed data
      const updatedPage = currentProject?.pages?.find(p => (p.page_id || p.id) === pageId)
      if (updatedPage) setSelectedPage(updatedPage)
    } catch (error) {
      console.error("Regenerate failed:", error)
    } finally {
      setIsRegenerating(false)
    }
  }, [projectId, isRegenerating, generateImages, pollTaskStatus, fetchProject, currentProject])

  const handlePageClick = useCallback((page: PPTPage) => {
    setSelectedPage(page)
  }, [])

  const handleExport = useCallback(async () => {
    if (!projectId || isExporting) return
    try {
      const result = await exportProject(projectId)
      // Trigger download by opening the URL
      window.open(result.download_url, '_blank')
    } catch (error) {
      console.error("Export failed:", error)
    }
  }, [projectId, isExporting, exportProject])

  const getGenerationStepLabel = () => {
    switch (generationStep) {
      case "outline":
        return t("editor.generating.outline")
      case "descriptions":
        return t("editor.generating.descriptions")
      case "images":
        return t("editor.generating.images")
      default:
        return t("editor.generating.default")
    }
  }

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
            <h2 className="font-semibold mb-2">{t('editor.error')}</h2>
            <p>{error}</p>
            <Button
              variant="outline"
              className="mt-4"
              onClick={() => navigate("/ppt")}
            >
              <ArrowLeft className="mr-2 h-4 w-4" />
              Back to Home
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
          <p className="text-muted-foreground mb-4">{t('editor.projectNotFound')}</p>
          <Button variant="outline" onClick={() => navigate("/ppt")}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            Back to Home
          </Button>
        </div>
      </AppLayout>
    )
  }

  const pages = currentProject.pages || []

  return (
    <AppLayout>
      <div className="-m-6 h-[calc(100vh-64px)] flex flex-col">
        {/* Header */}
        <div className="border-b px-6 py-4 flex items-center justify-between bg-background">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => navigate(-1)}
          >
            <ArrowLeft className="h-5 w-5" />
          </Button>
          <div>
            <h1 className="font-semibold">
              {currentProject.idea_prompt?.slice(0, 50) || "Untitled Project"}
              {(currentProject.idea_prompt?.length || 0) > 50 ? "..." : ""}
            </h1>
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Badge variant="outline">{currentProject.status}</Badge>
              <span>{t('editor.pagesCount', { count: pages.length })}</span>
            </div>
          </div>
        </div>

        {/* Step Navigation */}
        <div className="flex items-center gap-1 bg-muted rounded-lg p-1">
          <Button
            key="nav-outline"
            variant={currentStep === 'outline' ? 'secondary' : 'ghost'}
            size="sm"
            className="gap-2"
            onClick={() => navigate(`/ppt/project/${projectId}/outline`)}
          >
            <ListOrdered className="h-4 w-4" />
            大纲
          </Button>
          <Button
            key="nav-detail"
            variant={currentStep === 'detail' ? 'secondary' : 'ghost'}
            size="sm"
            className="gap-2"
            onClick={() => navigate(`/ppt/project/${projectId}/detail`)}
          >
            <FileText className="h-4 w-4" />
            描述
          </Button>
          <Button
            key="nav-preview"
            variant={currentStep === 'preview' ? 'secondary' : 'ghost'}
            size="sm"
            className="gap-2"
            onClick={() => navigate(`/ppt/project/${projectId}/preview`)}
          >
            <Eye className="h-4 w-4" />
            预览
          </Button>
        </div>

        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" disabled>
            <Settings className="mr-2 h-4 w-4" />
            {t('editor.settings')}
          </Button>
          <Button
            variant="default"
            size="sm"
            onClick={handleGenerate}
            disabled={isGenerating}
          >
            {isGenerating ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                {getGenerationStepLabel()}
                {taskProgress && (
                  <span className="ml-2">
                    ({taskProgress.completed}/{taskProgress.total})
                  </span>
                )}
              </>
            ) : (
              <>
                <Sparkles className="mr-2 h-4 w-4" />
                {t('editor.generate')}
              </>
            )}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleExport}
            disabled={isExporting || pages.length === 0 || !pages.some(p => p.generated_image_url || p.generated_image_path)}
          >
            {isExporting ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                {t('editor.exporting')}
              </>
            ) : (
              <>
                <FileDown className="mr-2 h-4 w-4" />
                {t('editor.export')}
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Generation Progress Bar */}
      {isGenerating && taskProgress && (
        <div className="px-6 py-2 bg-muted/50 border-b">
          <div className="flex items-center gap-4">
            <span className="text-sm text-muted-foreground">
              {getGenerationStepLabel()}
            </span>
            <div className="flex-1 h-2 bg-muted rounded-full overflow-hidden">
              <div
                className="h-full bg-primary transition-all duration-300"
                style={{
                  width: `${taskProgress.total > 0 ? (taskProgress.completed / taskProgress.total) * 100 : 0}%`,
                }}
              />
            </div>
            <span className="text-sm text-muted-foreground">
              {taskProgress.completed}/{taskProgress.total}
            </span>
          </div>
        </div>
      )}

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Sidebar - Outline */}
        <div className="w-80 border-r bg-muted/30 flex flex-col">
          <div className="p-4 border-b">
            <h2 className="font-semibold">{t('editor.sidebar.title')}</h2>
          </div>
          <ScrollArea className="flex-1">
            <div className="p-4 space-y-2">
              {pages.length === 0 ? (
                <div className="text-center py-8 text-muted-foreground">
                  <p className="text-sm">{t('editor.sidebar.emptyTitle')}</p>
                  <p className="text-xs mt-1">
                    {t('editor.sidebar.emptyDescription')}
                  </p>
                </div>
              ) : (
                pages.map((page, index) => (
                  <Card
                    key={page.page_id || page.id || index}
                    className="cursor-pointer hover:bg-accent transition-colors"
                    onClick={() => handlePageClick(page)}
                  >
                    <CardContent className="p-3">
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-muted-foreground w-6">
                          {index + 1}
                        </span>
                        <span className="text-sm font-medium truncate">
                          {page.outline_content?.title || "Untitled"}
                        </span>
                        <div
                          className={`w-2 h-2 rounded-full ml-auto ${getStatusColor(page.status)}`}
                        />
                      </div>
                    </CardContent>
                  </Card>
                ))
              )}
            </div>
          </ScrollArea>
        </div>

        {/* Main - Pages Grid */}
        <div className="flex-1 overflow-auto">
          <div className="p-6">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="font-semibold">{t('editor.pages.title')}</h2>
              <span className="text-sm text-muted-foreground">
                {t('editor.pagesCount', { count: pages.length })}
              </span>
            </div>

            {pages.length === 0 ? (
              <Card className="p-12">
                <div className="text-center text-muted-foreground">
                  <ImageIcon className="h-12 w-12 mx-auto mb-4 opacity-50" />
                  <h3 className="font-medium mb-2">{t('editor.pages.emptyTitle')}</h3>
                  <p className="text-sm">
                    {t('editor.pages.emptyDescription')}
                  </p>
                </div>
              </Card>
            ) : (
              <div className="space-y-3">
                {pages.map((page, index) => (
                  <PageCard
                    key={page.page_id || page.id || index}
                    page={page}
                    index={index}
                    t={t}
                    onClick={() => handlePageClick(page)}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Page Detail Dialog */}
      <Dialog open={!!selectedPage} onOpenChange={(open) => !open && setSelectedPage(null)}>
        <DialogContent className="max-w-4xl max-h-[90vh] overflow-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {selectedPage?.outline_content?.title || "Page Details"}
              {selectedPage?.part && (
                <Badge variant="outline" className="text-xs">
                  {selectedPage.part}
                </Badge>
              )}
            </DialogTitle>
          </DialogHeader>

          {selectedPage && (
            <div className="space-y-6">
              {/* Image Preview */}
              <div className="aspect-video bg-muted rounded-lg overflow-hidden relative">
                {(selectedPage.generated_image_url || selectedPage.generated_image_path) ? (
                  <img
                    src={selectedPage.generated_image_url || `/api/ppt/files${selectedPage.generated_image_path}`}
                    alt={selectedPage.outline_content?.title || "Page image"}
                    className="w-full h-full object-contain"
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-muted-foreground">
                    <ImageIcon className="h-16 w-16" />
                  </div>
                )}
              </div>

              {/* Outline Content */}
              <div>
                <h3 className="font-semibold mb-2">{t("editor.pageDetail.outline")}</h3>
                <div className="bg-muted/50 rounded-lg p-4">
                  <h4 className="font-medium mb-2">{selectedPage.outline_content?.title}</h4>
                  {selectedPage.outline_content?.points && (
                    <ul className="list-disc list-inside space-y-1 text-sm text-muted-foreground">
                      {selectedPage.outline_content.points.map((point, i) => (
                        <li key={i}>{point}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>

              {/* Description Content */}
              {selectedPage.description_content?.text && (
                <div>
                  <h3 className="font-semibold mb-2">{t("editor.pageDetail.description")}</h3>
                  <div className="bg-muted/50 rounded-lg p-4">
                    <p className="text-sm whitespace-pre-wrap">{selectedPage.description_content.text}</p>
                  </div>
                </div>
              )}

              {/* Actions */}
              <div className="flex justify-end gap-2">
                <Button
                  variant="outline"
                  onClick={() => handleRegeneratePage(selectedPage.page_id || selectedPage.id)}
                  disabled={isRegenerating}
                >
                  {isRegenerating ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      {t("editor.pageDetail.regenerating")}
                    </>
                  ) : (
                    <>
                      <RefreshCw className="mr-2 h-4 w-4" />
                      {t("editor.pageDetail.regenerate")}
                    </>
                  )}
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
      </div>
    </AppLayout>
  )
}
