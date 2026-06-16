/**
 * Export Tasks Panel Component
 * Displays ongoing and completed export tasks
 */
import { useEffect } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import {
    Sheet,
    SheetContent,
    SheetHeader,
    SheetTitle,
    SheetTrigger,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import {
    Download,
    Loader2,
    CheckCircle2,
    XCircle,
    Trash2,
    FileDown,
    Clock,
} from "lucide-react";
import {
    useExportTasksStore,
    type ExportTask,
} from "@/stores/exportTasksStore";

interface ExportTasksPanelProps {
    className?: string;
}

export function ExportTasksPanel({ className }: ExportTasksPanelProps) {
    const {
        tasks,
        removeTask,
        clearCompletedTasks,
        pollAllPendingTasks,
        getPendingTasks,
    } = useExportTasksStore();

    const pendingTasks = getPendingTasks();
    const hasPendingTasks = pendingTasks.length > 0;

    // Poll pending tasks on mount
    useEffect(() => {
        if (hasPendingTasks) {
            pollAllPendingTasks();
        }
    }, [hasPendingTasks, pollAllPendingTasks]);

    const getStatusIcon = (status: ExportTask["status"]) => {
        switch (status) {
            case "pending":
                return <Clock className="w-4 h-4 text-muted-foreground" />;
            case "processing":
                return (
                    <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />
                );
            case "completed":
                return <CheckCircle2 className="w-4 h-4 text-green-500" />;
            case "failed":
                return <XCircle className="w-4 h-4 text-destructive" />;
        }
    };

    const getFormatLabel = (format: ExportTask["format"]) => {
        switch (format) {
            case "pptx":
                return "PPTX";
            case "pdf":
                return "PDF";
            case "editable-pptx":
                return "Editable PPTX";
        }
    };

    const handleDownload = (task: ExportTask) => {
        if (task.downloadUrl) {
            window.open(task.downloadUrl, "_blank");
        }
    };

    const formatDate = (dateString: string) => {
        const date = new Date(dateString);
        return date.toLocaleString();
    };

    return (
        <Sheet>
            <SheetTrigger asChild>
                <Button
                    variant="outline"
                    size="sm"
                    className={cn("relative", className)}
                >
                    <FileDown className="w-4 h-4 mr-2" />
                    Exports
                    {hasPendingTasks && (
                        <Badge
                            variant="default"
                            className="absolute -top-2 -right-2 h-5 w-5 p-0 flex items-center justify-center text-xs"
                        >
                            {pendingTasks.length}
                        </Badge>
                    )}
                </Button>
            </SheetTrigger>
            <SheetContent className="w-[400px] sm:w-[540px]">
                <SheetHeader>
                    <div className="flex items-center justify-between">
                        <SheetTitle>Export Tasks</SheetTitle>
                        {tasks.length > 0 && (
                            <Button
                                variant="ghost"
                                size="sm"
                                onClick={clearCompletedTasks}
                            >
                                <Trash2 className="w-4 h-4 mr-1" />
                                Clear Completed
                            </Button>
                        )}
                    </div>
                </SheetHeader>

                <div className="mt-6 space-y-4">
                    {tasks.length === 0 ? (
                        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
                            <FileDown className="w-12 h-12 mb-4 opacity-50" />
                            <p className="text-sm">No export tasks</p>
                            <p className="text-xs mt-1">
                                Export tasks will appear here when you export a
                                presentation
                            </p>
                        </div>
                    ) : (
                        tasks.map((task) => (
                            <div
                                key={task.id}
                                className={cn(
                                    "p-4 rounded-lg border",
                                    task.status === "failed" &&
                                        "border-destructive/50 bg-destructive/5",
                                )}
                            >
                                <div className="flex items-start justify-between gap-3">
                                    <div className="flex-1 min-w-0">
                                        {/* Header */}
                                        <div className="flex items-center gap-2 mb-2">
                                            {getStatusIcon(task.status)}
                                            <span className="font-medium text-sm truncate">
                                                {task.projectName ||
                                                    `Project ${task.projectId.slice(0, 8)}`}
                                            </span>
                                            <Badge
                                                variant="outline"
                                                className="text-xs"
                                            >
                                                {getFormatLabel(task.format)}
                                            </Badge>
                                        </div>

                                        {/* Progress */}
                                        {task.status === "processing" &&
                                            task.progress && (
                                                <div className="space-y-1 mb-2">
                                                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                                                        <span>
                                                            {task.progress
                                                                .current_step ||
                                                                "Processing..."}
                                                        </span>
                                                        <span>
                                                            {
                                                                task.progress
                                                                    .completed
                                                            }
                                                            /
                                                            {
                                                                task.progress
                                                                    .total
                                                            }
                                                        </span>
                                                    </div>
                                                    <Progress
                                                        value={
                                                            (task.progress
                                                                .completed /
                                                                task.progress
                                                                    .total) *
                                                            100
                                                        }
                                                        className="h-2"
                                                    />
                                                </div>
                                            )}

                                        {/* Status text */}
                                        <div className="text-xs text-muted-foreground">
                                            {task.status === "pending" &&
                                                "Waiting to start..."}
                                            {task.status === "processing" &&
                                                "Export in progress..."}
                                            {task.status === "completed" && (
                                                <span className="text-green-600">
                                                    Export completed -{" "}
                                                    {task.filename ||
                                                        "Download ready"}
                                                </span>
                                            )}
                                            {task.status === "failed" && (
                                                <span className="text-destructive">
                                                    {task.errorMessage ||
                                                        "Export failed"}
                                                </span>
                                            )}
                                        </div>

                                        {/* Warnings */}
                                        {task.warnings &&
                                            task.warnings.length > 0 && (
                                                <div className="mt-2 text-xs text-orange-600">
                                                    {task.warnings.map(
                                                        (warning, i) => (
                                                            <p key={i}>
                                                                ⚠️ {warning}
                                                            </p>
                                                        ),
                                                    )}
                                                </div>
                                            )}

                                        {/* Timestamp */}
                                        <div className="mt-2 text-xs text-muted-foreground">
                                            Created:{" "}
                                            {formatDate(task.createdAt)}
                                            {task.completedAt && (
                                                <span className="ml-2">
                                                    | Completed:{" "}
                                                    {formatDate(
                                                        task.completedAt,
                                                    )}
                                                </span>
                                            )}
                                        </div>
                                    </div>

                                    {/* Actions */}
                                    <div className="flex items-center gap-1">
                                        {task.status === "completed" &&
                                            task.downloadUrl && (
                                                <Button
                                                    variant="default"
                                                    size="sm"
                                                    onClick={() =>
                                                        handleDownload(task)
                                                    }
                                                >
                                                    <Download className="w-4 h-4" />
                                                </Button>
                                            )}
                                        <Button
                                            variant="ghost"
                                            size="sm"
                                            onClick={() => removeTask(task.id)}
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </Button>
                                    </div>
                                </div>
                            </div>
                        ))
                    )}
                </div>
            </SheetContent>
        </Sheet>
    );
}

export default ExportTasksPanel;
