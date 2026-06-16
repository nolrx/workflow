/**
 * PPT Outline Editor Page
 * Edit and reorder outline content for PPT pages
 */
import { useEffect, useState, useCallback } from "react"
import { useParams, useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { usePPTStore, type PPTPage } from "@/stores/pptStore"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Badge } from "@/components/ui/badge"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Loader2,
  ArrowLeft,
  ArrowRight,
  Plus,
  Save,
  FileText,
  Sparkles,
  MoreVertical,
  GripVertical,
  Trash2,
  Edit,
} from "lucide-react"

interface OutlineCardProps {
  page: PPTPage
  index: number
  isSelected: boolean
  onSelect: () => void
  onEdit: () => void
  onDelete: () => void
}

function OutlineCard({
  page,
  index,
  isSelected,
  onSelect,
  onEdit,
  onDelete,
}: OutlineCardProps) {
  const title = page.outline_content?.title || `Page ${index + 1}`
  const points = page.outline_content?.points || []

  return (
    <Card
      className={`cursor-pointer transition-all ${
        isSelected ? "ring-2 ring-primary" : "hover:shadow-md"
      }`}
      onClick={onSelect}
    >
      <CardContent className="p-4">
        <div className="flex items-start gap-3">
          {/* Drag handle */}
          <div className="flex-shrink-0 pt-1 cursor-grab text-muted-foreground hover:text-foreground">
            <GripVertical className="w-5 h-5" />
          </div>

          {/* Page number */}
          <div className="flex-shrink-0 w-8 h-8 rounded-full bg-primary/10 text-primary flex items-center justify-center text-sm font-medium">
            {index + 1}
          </div>

          {/* Content */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <h3 className="font-medium truncate">{title}</h3>
              {page.part && (
                <Badge variant="outline" className="text-xs">
                  {page.part}
                </Badge>
              )}
            </div>
            {points.length > 0 && (
              <ul className="text-sm text-muted-foreground space-y-0.5">
                {points.slice(0, 3).map((point, i) => (
                  <li key={i} className="truncate">
                    • {point}
                  </li>
                ))}
                {points.length > 3 && (
                  <li className="text-muted-foreground/60">
                    +{points.length - 3} more...
                  </li>
                )}
              </ul>
            )}
          </div>

          {/* Actions */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
              <Button variant="ghost" size="icon" className="h-8 w-8">
                <MoreVertical className="w-4 h-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onClick={onEdit}>
                <Edit className="w-4 h-4 mr-2" />
                Edit
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={onDelete} className="text-destructive">
                <Trash2 className="w-4 h-4 mr-2" />
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </CardContent>
    </Card>
  )
}

interface EditDialogProps {
  page: PPTPage | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onSave: (data: { title: string; points: string[] }) => void
}

function EditPageDialog({ page, open, onOpenChange, onSave }: EditDialogProps) {
  const [title, setTitle] = useState("")
  const [pointsText, setPointsText] = useState("")

  useEffect(() => {
    if (page && open) {
      setTitle(page.outline_content?.title || "")
      setPointsText(page.outline_content?.points?.join("\n") || "")
    }
  }, [page, open])

  const handleSave = () => {
    const points = pointsText
      .split("\n")
      .map((p) => p.trim())
      .filter((p) => p)
    onSave({ title, points })
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit Page Outline</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div>
            <label className="text-sm font-medium mb-1.5 block">Title</label>
            <Input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Enter page title"
            />
          </div>
          <div>
            <label className="text-sm font-medium mb-1.5 block">
              Points (one per line)
            </label>
            <Textarea
              value={pointsText}
              onChange={(e) => setPointsText(e.target.value)}
              placeholder="Enter bullet points, one per line"
              rows={6}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave}>Save Changes</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default function OutlineEditor() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  useTranslation("ppt")

  const {
    currentProject,
    fetchProject,
    isLoading,
    error,
    isGenerating,
    generateOutline,
    addPage,
    updatePage,
    deletePage,
    clearError,
  } = usePPTStore()

  const [selectedPageId, setSelectedPageId] = useState<string | null>(null)
  const [editingPage, setEditingPage] = useState<PPTPage | null>(null)
  const [pageToDelete, setPageToDelete] = useState<PPTPage | null>(null)
  const [confirmRegenerate, setConfirmRegenerate] = useState(false)

  useEffect(() => {
    if (projectId) {
      fetchProject(projectId)
    }
  }, [projectId, fetchProject])

  const pages = currentProject?.pages || []
  const selectedPage = pages.find((p) => p.id === selectedPageId)

  const handleGenerateOutline = useCallback(async () => {
    if (!projectId) return

    if (pages.length > 0) {
      setConfirmRegenerate(true)
      return
    }

    try {
      await generateOutline(projectId)
    } catch (error) {
      console.error("Generate outline failed:", error)
    }
  }, [projectId, pages.length, generateOutline])

  const handleConfirmRegenerate = useCallback(async () => {
    if (!projectId) return
    setConfirmRegenerate(false)
    try {
      await generateOutline(projectId)
    } catch (error) {
      console.error("Generate outline failed:", error)
    }
  }, [projectId, generateOutline])

  const handleAddPage = useCallback(async () => {
    if (!projectId) return
    try {
      await addPage(projectId)
    } catch (error) {
      console.error("Add page failed:", error)
    }
  }, [projectId, addPage])

  const handleEditPage = useCallback((page: PPTPage) => {
    setEditingPage(page)
  }, [])

  const handleSaveEdit = useCallback(
    async (data: { title: string; points: string[] }) => {
      if (!projectId || !editingPage) return
      try {
        await updatePage(projectId, editingPage.id, {
          outline_content: {
            title: data.title,
            points: data.points,
          },
        })
        setEditingPage(null)
      } catch (error) {
        console.error("Update page failed:", error)
      }
    },
    [projectId, editingPage, updatePage]
  )

  const handleDeletePage = useCallback(async () => {
    if (!projectId || !pageToDelete) return
    try {
      await deletePage(projectId, pageToDelete.id)
      setPageToDelete(null)
      if (selectedPageId === pageToDelete.id) {
        setSelectedPageId(null)
      }
    } catch (error) {
      console.error("Delete page failed:", error)
    }
  }, [projectId, pageToDelete, deletePage, selectedPageId])

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
              <span className="text-xl">📝</span>
              <span className="font-semibold">Edit Outline</span>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" disabled>
              <Save className="mr-2 h-4 w-4" />
              Save
            </Button>
            <Button
              size="sm"
              onClick={() => navigate(`/ppt/project/${projectId}/detail`)}
            >
              Next
              <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
          </div>
        </header>

        {/* Context bar */}
        {currentProject.idea_prompt && (
          <div className="bg-muted/50 border-b px-6 py-2">
            <div className="flex items-start gap-2 text-sm">
              <Sparkles className="w-4 h-4 mt-0.5 text-primary" />
              <span className="text-muted-foreground">PPT Idea:</span>
              <span className="line-clamp-2">{currentProject.idea_prompt}</span>
            </div>
          </div>
        )}

        {/* Main content */}
        <div className="flex-1 flex overflow-hidden">
          {/* Left - Outline list */}
          <div className="flex-1 overflow-auto p-6">
            <div className="max-w-3xl mx-auto">
              {/* Action buttons */}
              <div className="flex gap-3 mb-6">
                <Button onClick={handleAddPage}>
                  <Plus className="mr-2 h-4 w-4" />
                  Add Page
                </Button>
                <Button
                  variant="secondary"
                  onClick={handleGenerateOutline}
                  disabled={isGenerating}
                >
                  {isGenerating ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Generating...
                    </>
                  ) : pages.length === 0 ? (
                    "Generate Outline"
                  ) : (
                    "Regenerate Outline"
                  )}
                </Button>
              </div>

              {/* Page cards */}
              {pages.length === 0 ? (
                <div className="text-center py-20">
                  <FileText className="w-16 h-16 mx-auto mb-4 text-muted-foreground/50" />
                  <h3 className="text-lg font-semibold mb-2">No pages yet</h3>
                  <p className="text-muted-foreground mb-4">
                    Add pages manually or generate an outline automatically
                  </p>
                </div>
              ) : (
                <div className="space-y-3">
                  {pages.map((page, index) => (
                    <OutlineCard
                      key={page.id}
                      page={page}
                      index={index}
                      isSelected={selectedPageId === page.id}
                      onSelect={() => setSelectedPageId(page.id)}
                      onEdit={() => handleEditPage(page)}
                      onDelete={() => setPageToDelete(page)}
                    />
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Right - Preview */}
          <div className="hidden lg:block w-96 border-l bg-muted/30 p-6 overflow-auto">
            <h3 className="font-semibold mb-4">Preview</h3>
            {selectedPage ? (
              <div className="space-y-4">
                <div>
                  <div className="text-sm text-muted-foreground mb-1">Title</div>
                  <div className="font-semibold">
                    {selectedPage.outline_content?.title || "Untitled"}
                  </div>
                </div>
                <div>
                  <div className="text-sm text-muted-foreground mb-2">Points</div>
                  <ul className="space-y-2">
                    {selectedPage.outline_content?.points?.map((point, i) => (
                      <li key={i} className="flex items-start text-sm">
                        <span className="mr-2 text-primary">•</span>
                        <span>{point}</span>
                      </li>
                    ))}
                  </ul>
                </div>
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

      {/* Edit dialog */}
      <EditPageDialog
        page={editingPage}
        open={!!editingPage}
        onOpenChange={(open) => !open && setEditingPage(null)}
        onSave={handleSaveEdit}
      />

      {/* Delete confirmation */}
      <AlertDialog
        open={!!pageToDelete}
        onOpenChange={(open) => !open && setPageToDelete(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Page</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete this page? This action cannot be
              undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleDeletePage}>
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Regenerate confirmation */}
      <AlertDialog
        open={confirmRegenerate}
        onOpenChange={setConfirmRegenerate}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Regenerate Outline</AlertDialogTitle>
            <AlertDialogDescription>
              You already have outline content. Regenerating will replace all
              existing pages. Are you sure?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmRegenerate}>
              Regenerate
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </AppLayout>
  )
}
