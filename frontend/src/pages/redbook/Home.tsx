/**
 * RedBook Studio Home Page
 * Entry point for creating new RedBook content
 */
import { useState, useRef } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { useRedBookStore, type RedBookPage } from "@/stores/redbookStore"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import {
  Loader2,
  ImagePlus,
  X,
  Sparkles,
  ArrowRight,
  History,
} from "lucide-react"
import { toast } from "sonner"

export default function RedBookHome() {
  const { t } = useTranslation("redbook")
  const navigate = useNavigate()
  const {
    createTask,
    generateOutline,
    isLoading,
    error,
    clearError,
  } = useRedBookStore()

  const [topic, setTopic] = useState("")
  const [images, setImages] = useState<File[]>([])
  const [imagePreviews, setImagePreviews] = useState<string[]>([])
  const [outline, setOutline] = useState("")
  const [pages, setPages] = useState<RedBookPage[]>([])
  const [step, setStep] = useState<"input" | "outline">("input")

  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleImageSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || [])
    if (files.length + images.length > 4) {
      toast.error(t("home.toast.maxImages"))
      return
    }

    setImages((prev) => [...prev, ...files])

    // Generate previews
    files.forEach((file) => {
      const reader = new FileReader()
      reader.onloadend = () => {
        setImagePreviews((prev) => [...prev, reader.result as string])
      }
      reader.readAsDataURL(file)
    })
  }

  const removeImage = (index: number) => {
    setImages((prev) => prev.filter((_, i) => i !== index))
    setImagePreviews((prev) => prev.filter((_, i) => i !== index))
  }

  const handleGenerateOutline = async () => {
    if (!topic.trim()) {
      toast.error(t("home.toast.topicRequired"))
      return
    }

    clearError()

    try {
      const result = await generateOutline(topic, images.length > 0 ? images : undefined)
      setOutline(result.outline)
      setPages(result.pages)
      setStep("outline")
      toast.success(t("home.toast.outlineSuccess", { count: result.pages.length }))
    } catch (err) {
      toast.error((err as Error).message || t("home.toast.outlineFailed"))
    }
  }

  const handleCreateTask = async () => {
    if (pages.length === 0) {
      toast.error(t("home.toast.outlineRequired"))
      return
    }

    try {
      // Create task with outline
      const task = await createTask(topic.slice(0, 100) || "新任务", topic)

      // Update task with outline data
      await useRedBookStore.getState().updateTask(task.id, {
        outline_raw: outline,
        outline_parsed: pages,
      })

      toast.success(t("home.toast.taskSuccess"))
      navigate(`/redbook/task/${task.id}`)
    } catch (err) {
      toast.error(t("home.toast.taskFailed"))
      console.error(err)
    }
  }

  const handleBack = () => {
    setStep("input")
    clearError()
  }

  return (
    <AppLayout>
      <div className="container mx-auto max-w-4xl py-8 px-4">
        <div className="mb-8 text-center">
          <h1 className="text-3xl font-bold mb-2">{t("home.title")}</h1>
          <p className="text-muted-foreground">
            {t("home.subtitle")}
          </p>
        </div>

        {step === "input" ? (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Sparkles className="h-5 w-5" />
                {t("home.createNew")}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              {/* Topic Input */}
              <div className="space-y-2">
                <Label htmlFor="topic">{t("home.form.topicLabel")}</Label>
                <Textarea
                  id="topic"
                  placeholder={t("home.form.topicPlaceholder")}
                  value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  rows={4}
                  className="resize-none"
                />
                <p className="text-xs text-muted-foreground">
                  {t("home.form.topicHelper")}
                </p>
              </div>

              {/* Image Upload */}
              <div className="space-y-2">
                <Label>{t("home.form.imagesLabel")}</Label>
                <div className="flex flex-wrap gap-3">
                  {imagePreviews.map((preview, index) => (
                    <div
                      key={index}
                      className="relative w-24 h-24 rounded-lg overflow-hidden border"
                    >
                      <img
                        src={preview}
                        alt={t("home.form.imageAlt", { index: index + 1 })}
                        className="w-full h-full object-cover"
                      />
                      <button
                        onClick={() => removeImage(index)}
                        className="absolute top-1 right-1 p-1 bg-black/50 rounded-full hover:bg-black/70"
                      >
                        <X className="h-3 w-3 text-white" />
                      </button>
                    </div>
                  ))}
                  {images.length < 4 && (
                    <button
                      onClick={() => fileInputRef.current?.click()}
                      className="w-24 h-24 rounded-lg border-2 border-dashed border-muted-foreground/25 hover:border-muted-foreground/50 flex flex-col items-center justify-center gap-1 text-muted-foreground"
                    >
                      <ImagePlus className="h-6 w-6" />
                      <span className="text-xs">{t("home.form.addImage")}</span>
                    </button>
                  )}
                </div>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  multiple
                  onChange={handleImageSelect}
                  className="hidden"
                />
                <p className="text-xs text-muted-foreground">
                  {t("home.form.imagesHelper")}
                </p>
              </div>

              {error && (
                <div className="p-3 bg-destructive/10 text-destructive rounded-md text-sm">
                  {error}
                </div>
              )}

              <div className="flex justify-between pt-4">
                <Button variant="outline" onClick={() => navigate("/redbook/history")}>
                  <History className="mr-2 h-4 w-4" />
                  {t("home.buttons.history")}
                </Button>
                <Button
                  onClick={handleGenerateOutline}
                  disabled={!topic.trim() || isLoading}
                >
                  {isLoading ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      {t("home.buttons.generating")}
                    </>
                  ) : (
                    <>
                      {t("home.buttons.generateOutline")}
                      <ArrowRight className="ml-2 h-4 w-4" />
                    </>
                  )}
                </Button>
              </div>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Sparkles className="h-5 w-5" />
                {t("home.preview.title")}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              {/* Outline Preview */}
              <div className="space-y-4">
                {pages.map((page, index) => (
                  <div
                    key={index}
                    className="p-4 rounded-lg border bg-muted/30"
                  >
                    <div className="flex items-center gap-2 mb-2">
                      <span className="px-2 py-0.5 rounded text-xs font-medium bg-primary/10 text-primary">
                        {page.type}
                      </span>
                      <span className="text-sm text-muted-foreground">
                        {t("home.preview.pageLabel", { index: index + 1 })}
                      </span>
                    </div>
                    <p className="text-sm whitespace-pre-wrap">{page.content}</p>
                  </div>
                ))}
              </div>

              {/* Raw Outline (collapsible) */}
              <details className="group">
                <summary className="cursor-pointer text-sm text-muted-foreground hover:text-foreground">
                  {t("home.preview.rawOutline")}
                </summary>
                <pre className="mt-2 p-4 rounded-lg bg-muted text-xs overflow-auto max-h-60">
                  {outline}
                </pre>
              </details>

              {error && (
                <div className="p-3 bg-destructive/10 text-destructive rounded-md text-sm">
                  {error}
                </div>
              )}

              <div className="flex justify-between pt-4">
                <Button variant="outline" onClick={handleBack}>
                  {t("home.preview.backToEdit")}
                </Button>
                <Button onClick={handleCreateTask} disabled={isLoading}>
                  {isLoading ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      {t("home.preview.creating")}
                    </>
                  ) : (
                    <>
                      {t("home.preview.startGenerate")}
                      <ArrowRight className="ml-2 h-4 w-4" />
                    </>
                  )}
                </Button>
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </AppLayout>
  )
}
