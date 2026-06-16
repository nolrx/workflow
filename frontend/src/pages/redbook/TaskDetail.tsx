/**
 * RedBook Task Detail Page
 * Displays task details, generated images, and content
 */
import { useEffect, useState } from "react"
import { useParams, useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { useRedBookStore, type RedBookPage } from "@/stores/redbookStore"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Loading } from "@/components/common/Loading"
import {
  Loader2,
  RefreshCw,
  Download,
  ArrowLeft,
  Image as ImageIcon,
  FileText,
  Copy,
  Check,
  AlertCircle,
} from "lucide-react"
import { toast } from "sonner"

export default function TaskDetail() {
  const { t } = useTranslation("redbook")
  const { id: taskId } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const {
    currentTask,
    fetchTask,
    generateImages,
    generateContent,
    retryImage,
    getImageUrl,
    isLoading,
    isGenerating,
    generateProgress,
    generatedImages,
    error,
    clearError,
    resetGeneration,
    updateTask,
  } = useRedBookStore()

  const [activeTab, setActiveTab] = useState("images")
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null)
  const [retryingIndex, setRetryingIndex] = useState<number | null>(null)

  useEffect(() => {
    if (taskId) {
      fetchTask(taskId)
      resetGeneration()
    }
  }, [taskId, fetchTask, resetGeneration])

  const handleGenerateImages = async () => {
    if (!currentTask?.outline_parsed || !taskId) return

    clearError()

    try {
      await generateImages(
        taskId,
        currentTask.outline_parsed,
        currentTask.outline_raw || "",
        undefined,
        currentTask.topic || ""
      )
      // Refresh task to get updated status
      await fetchTask(taskId)
      toast.success(t("taskDetail.toast.imagesComplete"))
    } catch (err) {
      toast.error((err as Error).message || t("taskDetail.toast.imagesFailed"))
    }
  }

  const handleGenerateContent = async () => {
    if (!currentTask?.topic || !currentTask?.outline_raw) {
      toast.error(t("taskDetail.toast.missingData"))
      return
    }

    clearError()

    try {
      const content = await generateContent(
        currentTask.topic,
        currentTask.outline_raw
      )

      // Save content to task
      await updateTask(taskId!, {
        content_titles: content.titles,
        content_copywriting: content.copywriting,
        content_tags: content.tags,
      })

      await fetchTask(taskId!)
      toast.success(t("taskDetail.toast.contentSuccess"))
    } catch (err) {
      toast.error((err as Error).message || t("taskDetail.toast.contentFailed"))
    }
  }

  const handleRetryImage = async (page: RedBookPage) => {
    if (!currentTask?.image_task_id || !taskId) return

    setRetryingIndex(page.index)

    try {
      const result = await retryImage(taskId, currentTask.image_task_id, page)
      if (result.success) {
        toast.success(t("taskDetail.toast.imageRetrySuccess", { index: page.index + 1 }))
      } else {
        toast.error(result.error || t("taskDetail.toast.imageRetryFailed"))
      }
    } finally {
      setRetryingIndex(null)
    }
  }

  const handleCopy = (text: string, index: number) => {
    navigator.clipboard.writeText(text)
    setCopiedIndex(index)
    toast.success(t("taskDetail.toast.copied"))
    setTimeout(() => setCopiedIndex(null), 2000)
  }

  const handleDownload = () => {
    if (!currentTask?.image_task_id) return
    window.open(`/api/redbook/tasks/${taskId}/download`, "_blank")
  }

  if (isLoading && !currentTask) {
    return (
      <AppLayout>
        <Loading />
      </AppLayout>
    )
  }

  if (!currentTask) {
    return (
      <AppLayout>
        <div className="container mx-auto max-w-4xl py-8 px-4">
          <div className="text-center py-12">
            <p className="text-muted-foreground">{t("taskDetail.notFound")}</p>
            <Button
              variant="outline"
              className="mt-4"
              onClick={() => navigate("/redbook")}
            >
              {t("taskDetail.backToHome")}
            </Button>
          </div>
        </div>
      </AppLayout>
    )
  }

  const pages = currentTask.outline_parsed || []
  const statusBadge = {
    draft: { label: t("taskDetail.status.draft"), variant: "secondary" as const },
    generating: { label: t("taskDetail.status.generating"), variant: "default" as const },
    partial: { label: t("taskDetail.status.partial"), variant: "outline" as const },
    completed: { label: t("taskDetail.status.completed"), variant: "default" as const },
    error: { label: t("taskDetail.status.error"), variant: "destructive" as const },
  }[currentTask.status]

  return (
    <AppLayout>
      <div className="container mx-auto max-w-6xl py-6 px-4">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-4">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => navigate("/redbook/history")}
            >
              <ArrowLeft className="h-4 w-4 mr-1" />
              {t("taskDetail.back")}
            </Button>
            <div>
              <h1 className="text-xl font-semibold truncate max-w-md">
                {currentTask.title}
              </h1>
              <div className="flex items-center gap-2 mt-1">
                <Badge variant={statusBadge.variant}>{statusBadge.label}</Badge>
                <span className="text-sm text-muted-foreground">
                  {new Date(currentTask.created_at).toLocaleDateString()}
                </span>
              </div>
            </div>
          </div>
          <div className="flex gap-2">
            {currentTask.image_task_id && (
              <Button variant="outline" size="sm" onClick={handleDownload}>
                <Download className="h-4 w-4 mr-1" />
                Download
              </Button>
            )}
          </div>
        </div>

        {/* Main Content */}
        <Tabs value={activeTab} onValueChange={setActiveTab}>
          <TabsList className="mb-4">
            <TabsTrigger value="images" className="flex items-center gap-2">
              <ImageIcon className="h-4 w-4" />
              {t("taskDetail.tabs.images", { count: pages.length })}
            </TabsTrigger>
            <TabsTrigger value="content" className="flex items-center gap-2">
              <FileText className="h-4 w-4" />
              {t("taskDetail.tabs.content")}
            </TabsTrigger>
          </TabsList>

          {/* Images Tab */}
          <TabsContent value="images">
            {error && (
              <div className="mb-4 p-3 bg-destructive/10 text-destructive rounded-md text-sm flex items-center gap-2">
                <AlertCircle className="h-4 w-4" />
                {error}
              </div>
            )}

            {/* Generation Progress */}
            {isGenerating && generateProgress && (
              <Card className="mb-4">
                <CardContent className="py-4">
                  <div className="flex items-center gap-4">
                    <Loader2 className="h-5 w-5 animate-spin text-primary" />
                    <div className="flex-1">
                      <div className="flex justify-between text-sm mb-1">
                        <span>{t("taskDetail.images.generating")}</span>
                        <span>
                          {t("taskDetail.images.progress", {
                            completed: generateProgress.completed,
                            total: generateProgress.total
                          })}
                        </span>
                      </div>
                      <div className="w-full bg-muted rounded-full h-2">
                        <div
                          className="bg-primary h-2 rounded-full transition-all"
                          style={{
                            width: `${
                              (generateProgress.completed / generateProgress.total) *
                              100
                            }%`,
                          }}
                        />
                      </div>
                    </div>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Generate Button */}
            {!currentTask.image_task_id && pages.length > 0 && (
              <div className="mb-6 text-center">
                <Button
                  size="lg"
                  onClick={handleGenerateImages}
                  disabled={isGenerating}
                >
                  {isGenerating ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      {t("taskDetail.images.generating_btn")}
                    </>
                  ) : (
                    t("taskDetail.images.startGenerate")
                  )}
                </Button>
              </div>
            )}

            {/* Image Grid */}
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
              {pages.map((page, index) => {
                const imageUrl = generatedImages.get(index) ||
                  (currentTask.image_task_id
                    ? getImageUrl(currentTask.image_task_id, `${index}.png`)
                    : null)

                return (
                  <Card key={index} className="overflow-hidden">
                    <div className="aspect-[3/4] bg-muted relative">
                      {imageUrl ? (
                        <img
                          src={imageUrl}
                          alt={t("taskDetail.images.pageAlt", { index: index + 1 })}
                          className="w-full h-full object-cover"
                          onError={(e) => {
                            e.currentTarget.style.display = "none"
                          }}
                        />
                      ) : (
                        <div className="w-full h-full flex items-center justify-center text-muted-foreground">
                          {isGenerating && generateProgress?.currentIndex === index ? (
                            <Loader2 className="h-8 w-8 animate-spin" />
                          ) : (
                            <ImageIcon className="h-8 w-8" />
                          )}
                        </div>
                      )}
                    </div>
                    <CardContent className="p-3">
                      <div className="flex items-center justify-between">
                        <div>
                          <Badge variant="outline" className="text-xs">
                            {page.type}
                          </Badge>
                          <span className="text-xs text-muted-foreground ml-2">
                            {t("taskDetail.images.pageAlt", { index: index + 1 })}
                          </span>
                        </div>
                        {currentTask.image_task_id && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => handleRetryImage(page)}
                            disabled={retryingIndex === index}
                          >
                            {retryingIndex === index ? (
                              <Loader2 className="h-3 w-3 animate-spin" />
                            ) : (
                              <RefreshCw className="h-3 w-3" />
                            )}
                          </Button>
                        )}
                      </div>
                    </CardContent>
                  </Card>
                )
              })}
            </div>
          </TabsContent>

          {/* Content Tab */}
          <TabsContent value="content">
            {!currentTask.content_titles && (
              <div className="text-center py-12">
                <p className="text-muted-foreground mb-4">
                  {t("taskDetail.content.empty")}
                </p>
                <Button onClick={handleGenerateContent} disabled={isLoading}>
                  {isLoading ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      {t("taskDetail.content.generating")}
                    </>
                  ) : (
                    t("taskDetail.content.generateContent")
                  )}
                </Button>
              </div>
            )}

            {currentTask.content_titles && (
              <div className="space-y-6">
                {/* Titles */}
                <Card>
                  <CardHeader>
                    <CardTitle className="text-base">{t("taskDetail.content.titles")}</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="space-y-2">
                      {currentTask.content_titles.map((title, index) => (
                        <div
                          key={index}
                          className="flex items-center justify-between p-3 rounded-lg bg-muted/50"
                        >
                          <span className="flex-1">{title}</span>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => handleCopy(title, index)}
                          >
                            {copiedIndex === index ? (
                              <Check className="h-4 w-4 text-green-500" />
                            ) : (
                              <Copy className="h-4 w-4" />
                            )}
                          </Button>
                        </div>
                      ))}
                    </div>
                  </CardContent>
                </Card>

                {/* Copywriting */}
                {currentTask.content_copywriting && (
                  <Card>
                    <CardHeader className="flex flex-row items-center justify-between">
                      <CardTitle className="text-base">{t("taskDetail.content.copywriting")}</CardTitle>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() =>
                          handleCopy(currentTask.content_copywriting!, 100)
                        }
                      >
                        {copiedIndex === 100 ? (
                          <Check className="h-4 w-4 text-green-500" />
                        ) : (
                          <Copy className="h-4 w-4" />
                        )}
                      </Button>
                    </CardHeader>
                    <CardContent>
                      <ScrollArea className="h-[300px]">
                        <p className="whitespace-pre-wrap text-sm">
                          {currentTask.content_copywriting}
                        </p>
                      </ScrollArea>
                    </CardContent>
                  </Card>
                )}

                {/* Tags */}
                {currentTask.content_tags && currentTask.content_tags.length > 0 && (
                  <Card>
                    <CardHeader className="flex flex-row items-center justify-between">
                      <CardTitle className="text-base">{t("taskDetail.content.tags")}</CardTitle>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() =>
                          handleCopy(
                            currentTask.content_tags!.map((t) => `#${t}`).join(" "),
                            101
                          )
                        }
                      >
                        {copiedIndex === 101 ? (
                          <Check className="h-4 w-4 text-green-500" />
                        ) : (
                          <Copy className="h-4 w-4" />
                        )}
                      </Button>
                    </CardHeader>
                    <CardContent>
                      <div className="flex flex-wrap gap-2">
                        {currentTask.content_tags.map((tag, index) => (
                          <Badge key={index} variant="secondary">
                            #{tag}
                          </Badge>
                        ))}
                      </div>
                    </CardContent>
                  </Card>
                )}

                {/* Regenerate Button */}
                <div className="text-center">
                  <Button
                    variant="outline"
                    onClick={handleGenerateContent}
                    disabled={isLoading}
                  >
                    {isLoading ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        {t("taskDetail.content.regenerating")}
                      </>
                    ) : (
                      <>
                        <RefreshCw className="mr-2 h-4 w-4" />
                        {t("taskDetail.content.regenerate")}
                      </>
                    )}
                  </Button>
                </div>
              </div>
            )}
          </TabsContent>
        </Tabs>
      </div>
    </AppLayout>
  )
}
