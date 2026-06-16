/**
 * PPT Studio History Page
 * List of all PPT projects with search and filters
 */
import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { usePPTStore, type PPTProject } from "@/stores/pptStore"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
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
  Loader2,
  Plus,
  Search,
  MoreVertical,
  Trash2,
  Edit,
  Image as ImageIcon,
  Clock,
} from "lucide-react"
import { formatDistanceToNow } from "date-fns"

function getStatusBadge(status: string) {
  switch (status) {
    case "COMPLETED":
      return <Badge className="bg-green-500">Completed</Badge>
    case "PROCESSING":
      return <Badge className="bg-blue-500">Processing</Badge>
    case "FAILED":
      return <Badge variant="destructive">Failed</Badge>
    default:
      return <Badge variant="secondary">{status}</Badge>
  }
}

function ProjectCard({
  project,
  onEdit,
  onDelete,
  t,
}: {
  project: PPTProject
  onEdit: () => void
  onDelete: () => void
  t: any
}) {
  const pageCount = project.pages?.length || 0
  const firstPage = project.pages?.[0]

  return (
    <Card className="group hover:shadow-md transition-shadow cursor-pointer">
      <CardContent className="p-0">
        <div className="flex">
          {/* Thumbnail */}
          <div className="w-40 h-28 bg-muted flex-shrink-0 overflow-hidden rounded-l-lg">
            {firstPage?.generated_image_path ? (
              <img
                src={`/api/ppt/files${firstPage.generated_image_path}`}
                alt="Preview"
                className="w-full h-full object-cover"
              />
            ) : (
              <div className="w-full h-full flex items-center justify-center text-muted-foreground">
                <ImageIcon className="h-10 w-10" />
              </div>
            )}
          </div>

          {/* Content */}
          <div className="flex-1 p-4 min-w-0">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <h3 className="font-medium truncate mb-1" onClick={onEdit}>
                  {project.idea_prompt?.slice(0, 60) || "Untitled Project"}
                  {(project.idea_prompt?.length || 0) > 60 ? "..." : ""}
                </h3>
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  {getStatusBadge(project.status)}
                  <span>{t('editor.pagesCount', { count: pageCount })}</span>
                </div>
              </div>

              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="opacity-0 group-hover:opacity-100 transition-opacity"
                  >
                    <MoreVertical className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem onClick={onEdit}>
                    <Edit className="mr-2 h-4 w-4" />
                    {t('history.menu.edit')}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onClick={onDelete}
                    className="text-destructive"
                  >
                    <Trash2 className="mr-2 h-4 w-4" />
                    {t('history.menu.delete')}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>

            <div className="flex items-center gap-1 text-xs text-muted-foreground mt-3">
              <Clock className="h-3 w-3" />
              {formatDistanceToNow(new Date(project.updated_at), {
                addSuffix: true,
              })}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

export default function PPTHistory() {
  const navigate = useNavigate()
  const { t } = useTranslation('ppt')
  const {
    projects,
    fetchProjects,
    deleteProject,
    isLoading,
    error,
    hasMore,
  } = usePPTStore()
  const [searchQuery, setSearchQuery] = useState("")
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [projectToDelete, setProjectToDelete] = useState<PPTProject | null>(
    null
  )

  useEffect(() => {
    fetchProjects()
  }, [fetchProjects])

  const filteredProjects = projects.filter((p) =>
    p.idea_prompt?.toLowerCase().includes(searchQuery.toLowerCase())
  )

  const handleDelete = async () => {
    if (projectToDelete) {
      try {
        await deleteProject(projectToDelete.id)
        setDeleteDialogOpen(false)
        setProjectToDelete(null)
      } catch (err) {
        console.error("Failed to delete project:", err)
      }
    }
  }

  const handleLoadMore = () => {
    fetchProjects(50, projects.length)
  }

  return (
    <AppLayout>
      <div className="container mx-auto max-w-4xl py-8">
        {/* Header */}
        <div className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">{t('history.title')}</h1>
          <p className="text-muted-foreground">
            {t('history.subtitle')}
          </p>
        </div>
        <Button onClick={() => navigate("/ppt")}>
          <Plus className="mr-2 h-4 w-4" />
          {t('history.newProject')}
        </Button>
      </div>

      {/* Search */}
      <div className="mb-6 relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          placeholder={t('history.searchPlaceholder')}
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="pl-10"
        />
      </div>

      {/* Error */}
      {error && (
        <div className="mb-6 p-4 bg-destructive/10 text-destructive rounded-lg">
          {error}
        </div>
      )}

      {/* Projects List */}
      {isLoading && projects.length === 0 ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-8 w-8 animate-spin text-primary" />
        </div>
      ) : filteredProjects.length === 0 ? (
        <Card className="p-12">
          <div className="text-center text-muted-foreground">
            <ImageIcon className="h-12 w-12 mx-auto mb-4 opacity-50" />
            <h3 className="font-medium mb-2">
              {searchQuery ? t('history.empty.noMatch.title') : t('history.empty.noProjects.title')}
            </h3>
            <p className="text-sm mb-4">
              {searchQuery
                ? t('history.empty.noMatch.description')
                : t('history.empty.noProjects.description')}
            </p>
            {!searchQuery && (
              <Button onClick={() => navigate("/ppt")}>
                <Plus className="mr-2 h-4 w-4" />
                {t('history.createProject')}
              </Button>
            )}
          </div>
        </Card>
      ) : (
        <div className="space-y-4">
          {filteredProjects.map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              onEdit={() => navigate(`/ppt/project/${project.id}`)}
              onDelete={() => {
                setProjectToDelete(project)
                setDeleteDialogOpen(true)
              }}
              t={t}
            />
          ))}

          {/* Load More */}
          {hasMore && (
            <div className="pt-4 text-center">
              <Button
                variant="outline"
                onClick={handleLoadMore}
                disabled={isLoading}
              >
                {isLoading ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Loading...
                  </>
                ) : (
                  "Load More"
                )}
              </Button>
            </div>
          )}
        </div>
      )}

      {/* Delete Confirmation Dialog */}
      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('history.deleteDialog.title')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('history.deleteDialog.description')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDelete}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {t('history.menu.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      </div>
    </AppLayout>
  )
}
