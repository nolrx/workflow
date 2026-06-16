/**
 * RedBook History Page
 * Lists all RedBook tasks with search and filter
 */
import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { useRedBookStore } from "@/stores/redbookStore"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
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
import { Loading } from "@/components/common/Loading"
import { EmptyState } from "@/components/common/EmptyState"
import {
  Plus,
  Search,
  Trash2,
  MoreVertical,
  Image as ImageIcon,
  FileText,
} from "lucide-react"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { toast } from "sonner"

export default function RedBookHistory() {
  const { t } = useTranslation("redbook")
  const navigate = useNavigate()
  const {
    tasks,
    fetchTasks,
    deleteTask,
    fetchStats,
    stats,
    isLoading,
    hasMore,
    getImageUrl,
  } = useRedBookStore()

  const [searchQuery, setSearchQuery] = useState("")
  const [deleteTaskId, setDeleteTaskId] = useState<string | null>(null)

  useEffect(() => {
    fetchTasks()
    fetchStats()
  }, [fetchTasks, fetchStats])

  const handleLoadMore = () => {
    fetchTasks(20, tasks.length)
  }

  const handleDelete = async () => {
    if (!deleteTaskId) return

    try {
      await deleteTask(deleteTaskId)
      toast.success(t("history.toast.deleteSuccess"))
      fetchStats()
    } catch (err) {
      toast.error(t("history.toast.deleteFailed"))
      console.error(err)
    } finally {
      setDeleteTaskId(null)
    }
  }

  const filteredTasks = tasks.filter(
    (task) =>
      task.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
      (task.topic && task.topic.toLowerCase().includes(searchQuery.toLowerCase()))
  )

  const statusConfig = {
    draft: { label: t("history.status.draft"), variant: "secondary" as const },
    generating: { label: t("history.status.generating"), variant: "default" as const },
    partial: { label: t("history.status.partial"), variant: "outline" as const },
    completed: { label: t("history.status.completed"), variant: "default" as const },
    error: { label: t("history.status.error"), variant: "destructive" as const },
  }

  return (
    <AppLayout>
      <div className="container mx-auto max-w-6xl py-6 px-4">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold">{t("history.title")}</h1>
            {stats && (
              <p className="text-sm text-muted-foreground mt-1">
                {t("history.stats.summary", { total: stats.total, completed: stats.completed })}
              </p>
            )}
          </div>
          <Button onClick={() => navigate("/redbook")}>
            <Plus className="h-4 w-4 mr-2" />
            {t("history.newContent")}
          </Button>
        </div>

        {/* Stats Cards */}
        {stats && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <Card>
              <CardContent className="pt-4">
                <div className="text-2xl font-bold">{stats.total}</div>
                <p className="text-sm text-muted-foreground">{t("history.stats.total")}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <div className="text-2xl font-bold text-green-600">
                  {stats.completed}
                </div>
                <p className="text-sm text-muted-foreground">{t("history.stats.completed")}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <div className="text-2xl font-bold text-blue-600">
                  {stats.generating}
                </div>
                <p className="text-sm text-muted-foreground">{t("history.stats.generating")}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <div className="text-2xl font-bold text-gray-600">
                  {stats.draft}
                </div>
                <p className="text-sm text-muted-foreground">{t("history.stats.draft")}</p>
              </CardContent>
            </Card>
          </div>
        )}

        {/* Search */}
        <div className="relative mb-6">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder={t("history.searchPlaceholder")}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-10"
          />
        </div>

        {/* Task List */}
        {isLoading && tasks.length === 0 ? (
          <Loading />
        ) : filteredTasks.length === 0 ? (
          <EmptyState
            icon={<FileText className="h-12 w-12" />}
            title={searchQuery ? t("history.empty.noMatch.title") : t("history.empty.noTasks.title")}
            description={
              searchQuery ? t("history.empty.noMatch.description") : t("history.empty.noTasks.description")
            }
            action={
              !searchQuery ? (
                <Button onClick={() => navigate("/redbook")}>
                  <Plus className="h-4 w-4 mr-2" />
                  {t("history.newContent")}
                </Button>
              ) : undefined
            }
          />
        ) : (
          <div className="space-y-3">
            {filteredTasks.map((task) => {
              const config = statusConfig[task.status]
              const thumbnailUrl = task.image_task_id && task.thumbnail_path
                ? getImageUrl(task.image_task_id, task.thumbnail_path)
                : null

              return (
                <Card
                  key={task.id}
                  className="hover:shadow-md transition-shadow cursor-pointer"
                  onClick={() => navigate(`/redbook/task/${task.id}`)}
                >
                  <CardContent className="p-4">
                    <div className="flex items-start gap-4">
                      {/* Thumbnail */}
                      <div className="w-16 h-16 rounded-lg bg-muted flex-shrink-0 overflow-hidden">
                        {thumbnailUrl ? (
                          <img
                            src={thumbnailUrl}
                            alt={task.title}
                            className="w-full h-full object-cover"
                            onError={(e) => {
                              e.currentTarget.style.display = "none"
                            }}
                          />
                        ) : (
                          <div className="w-full h-full flex items-center justify-center text-muted-foreground">
                            <ImageIcon className="h-6 w-6" />
                          </div>
                        )}
                      </div>

                      {/* Content */}
                      <div className="flex-1 min-w-0">
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <h3 className="font-medium truncate">{task.title}</h3>
                            {task.topic && (
                              <p className="text-sm text-muted-foreground line-clamp-1 mt-0.5">
                                {task.topic}
                              </p>
                            )}
                          </div>
                          <div
                            className="flex items-center gap-2"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <Badge variant={config.variant}>{config.label}</Badge>
                            <DropdownMenu>
                              <DropdownMenuTrigger asChild>
                                <Button variant="ghost" size="sm">
                                  <MoreVertical className="h-4 w-4" />
                                </Button>
                              </DropdownMenuTrigger>
                              <DropdownMenuContent align="end">
                                <DropdownMenuItem
                                  className="text-destructive"
                                  onClick={() => setDeleteTaskId(task.id)}
                                >
                                  <Trash2 className="h-4 w-4 mr-2" />
                                  {t("history.menu.delete")}
                                </DropdownMenuItem>
                              </DropdownMenuContent>
                            </DropdownMenu>
                          </div>
                        </div>
                        <div className="flex items-center gap-3 mt-2 text-xs text-muted-foreground">
                          <span>
                            {new Date(task.created_at).toLocaleDateString()}
                          </span>
                          {task.outline_parsed && (
                            <span>{t("history.pagesCount", { count: task.outline_parsed.length })}</span>
                          )}
                        </div>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              )
            })}

            {/* Load More */}
            {hasMore && (
              <div className="text-center pt-4">
                <Button
                  variant="outline"
                  onClick={handleLoadMore}
                  disabled={isLoading}
                >
                  {isLoading ? "Loading..." : "Load More"}
                </Button>
              </div>
            )}
          </div>
        )}

        {/* Delete Confirmation Dialog */}
        <AlertDialog
          open={!!deleteTaskId}
          onOpenChange={() => setDeleteTaskId(null)}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{t("history.deleteDialog.title")}</AlertDialogTitle>
              <AlertDialogDescription>
                {t("history.deleteDialog.description")}
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={handleDelete}
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              >
                {t("history.menu.delete")}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </AppLayout>
  )
}
