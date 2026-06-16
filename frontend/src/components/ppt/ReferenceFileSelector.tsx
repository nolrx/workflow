/**
 * Reference File Selector Component
 * Browse and select reference files for PPT generation
 */
import { useState, useEffect, useRef, useCallback } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogFooter,
} from "@/components/ui/dialog";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import {
    FileText,
    Upload,
    X,
    Loader2,
    CheckCircle2,
    XCircle,
    RefreshCw,
    Clock,
} from "lucide-react";
import { pptApi, type ReferenceFile } from "@/api";

interface ReferenceFileSelectorProps {
    projectId?: string | null;
    isOpen: boolean;
    onClose: () => void;
    onSelect: (files: ReferenceFile[]) => void;
    multiple?: boolean;
    maxSelection?: number;
    initialSelectedIds?: string[];
}

export function ReferenceFileSelector({
    projectId,
    isOpen,
    onClose,
    onSelect,
    multiple = true,
    maxSelection,
    initialSelectedIds = [],
}: ReferenceFileSelectorProps) {
    const { toast } = useToast();
    const [files, setFiles] = useState<ReferenceFile[]>([]);
    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
    const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());
    const [isLoading, setIsLoading] = useState(false);
    const [isUploading, setIsUploading] = useState(false);
    const [parsingIds, setParsingIds] = useState<Set<string>>(new Set());
    const [filterScope, setFilterScope] = useState<string>("all");
    const fileInputRef = useRef<HTMLInputElement>(null);

    const loadFiles = useCallback(async () => {
        setIsLoading(true);
        try {
            const targetProjectId =
                filterScope === "all"
                    ? "all"
                    : filterScope === "none"
                      ? "none"
                      : filterScope;
            const response = await pptApi.listReferenceFiles(targetProjectId);
            if (response?.files) {
                setFiles((prev) => {
                    const fileMap = new Map<string, ReferenceFile>();
                    response.files.forEach((f: ReferenceFile) => {
                        fileMap.set(f.id, f);
                    });
                    // Keep parsing files
                    prev.forEach((f) => {
                        if (parsingIds.has(f.id) && !fileMap.has(f.id)) {
                            fileMap.set(f.id, f);
                        }
                    });
                    return Array.from(fileMap.values());
                });
            }
        } catch (error: any) {
            console.error("Failed to load files:", error);
            toast({
                title: "Error",
                description: error?.message || "Failed to load reference files",
                variant: "destructive",
            });
        } finally {
            setIsLoading(false);
        }
    }, [filterScope, parsingIds, toast]);

    useEffect(() => {
        if (isOpen) {
            loadFiles();
            setSelectedIds(new Set(initialSelectedIds));
        }
    }, [isOpen, filterScope, loadFiles, initialSelectedIds]);

    // Poll parsing status
    useEffect(() => {
        if (!isOpen || parsingIds.size === 0) return;

        const intervalId = setInterval(async () => {
            const idsToCheck = Array.from(parsingIds);
            const updatedFiles: ReferenceFile[] = [];
            const completedIds: string[] = [];

            for (const fileId of idsToCheck) {
                try {
                    const response = await pptApi.getReferenceFile(fileId);
                    if (response?.file) {
                        const updatedFile = response.file;
                        updatedFiles.push(updatedFile);
                        if (
                            updatedFile.parse_status === "completed" ||
                            updatedFile.parse_status === "failed"
                        ) {
                            completedIds.push(fileId);
                        }
                    }
                } catch (error) {
                    console.error(`Failed to poll file ${fileId}:`, error);
                }
            }

            if (updatedFiles.length > 0) {
                setFiles((prev) => {
                    const fileMap = new Map(prev.map((f) => [f.id, f]));
                    updatedFiles.forEach((uf) => fileMap.set(uf.id, uf));
                    return Array.from(fileMap.values());
                });
            }

            if (completedIds.length > 0) {
                setParsingIds((prev) => {
                    const newSet = new Set(prev);
                    completedIds.forEach((id) => newSet.delete(id));
                    return newSet;
                });
            }
        }, 2000);

        return () => clearInterval(intervalId);
    }, [isOpen, parsingIds]);

    const handleSelectFile = (file: ReferenceFile) => {
        if (multiple) {
            const newSelected = new Set(selectedIds);
            if (newSelected.has(file.id)) {
                newSelected.delete(file.id);
            } else {
                if (maxSelection && newSelected.size >= maxSelection) {
                    toast({
                        title: "Selection limit",
                        description: `Maximum ${maxSelection} files can be selected`,
                    });
                    return;
                }
                newSelected.add(file.id);
            }
            setSelectedIds(newSelected);
        } else {
            setSelectedIds(new Set([file.id]));
        }
    };

    const handleConfirm = async () => {
        const selected = files.filter((f) => selectedIds.has(f.id));
        if (selected.length === 0) {
            toast({
                title: "No selection",
                description: "Please select at least one file",
            });
            return;
        }

        // Trigger parsing for pending files
        const unparsedFiles = selected.filter(
            (f) => f.parse_status === "pending",
        );
        if (unparsedFiles.length > 0) {
            toast({
                title: "Parsing started",
                description: `${unparsedFiles.length} file(s) will be parsed in background`,
            });
            unparsedFiles.forEach((file) => {
                pptApi.parseReferenceFile(file.id).catch((error) => {
                    console.error(
                        `Failed to parse file ${file.filename}:`,
                        error,
                    );
                });
            });
        }

        onSelect(selected);
        onClose();
    };

    const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const inputFiles = e.target.files;
        if (!inputFiles || inputFiles.length === 0) return;

        setIsUploading(true);
        try {
            const targetProjectId =
                filterScope === "all" || filterScope === "none"
                    ? undefined
                    : filterScope;

            const uploadPromises = Array.from(inputFiles).map((file) =>
                pptApi.uploadReferenceFile(file, targetProjectId),
            );

            const results = await Promise.all(uploadPromises);
            const uploadedFiles = results
                .map((r) => r?.file)
                .filter((f): f is ReferenceFile => f !== undefined);

            if (uploadedFiles.length > 0) {
                toast({
                    title: "Upload successful",
                    description: `${uploadedFiles.length} file(s) uploaded`,
                });

                const needsParsing = uploadedFiles.filter(
                    (f) => f.parse_status === "parsing",
                );
                if (needsParsing.length > 0) {
                    setParsingIds((prev) => {
                        const newSet = new Set(prev);
                        needsParsing.forEach((f) => newSet.add(f.id));
                        return newSet;
                    });
                }

                setFiles((prev) => {
                    const fileMap = new Map(prev.map((f) => [f.id, f]));
                    uploadedFiles.forEach((uf) => fileMap.set(uf.id, uf));
                    return Array.from(fileMap.values());
                });

                setTimeout(() => loadFiles(), 500);
            }
        } catch (error: any) {
            console.error("Upload failed:", error);
            toast({
                title: "Upload failed",
                description: error?.message || "Failed to upload files",
                variant: "destructive",
            });
        } finally {
            setIsUploading(false);
            if (fileInputRef.current) {
                fileInputRef.current.value = "";
            }
        }
    };

    const handleDelete = async (e: React.MouseEvent, file: ReferenceFile) => {
        e.stopPropagation();
        if (!file.id) return;

        setDeletingIds((prev) => new Set(prev).add(file.id));
        try {
            await pptApi.deleteReferenceFile(file.id);
            toast({
                title: "Deleted",
                description: "File deleted successfully",
            });
            setSelectedIds((prev) => {
                const next = new Set(prev);
                next.delete(file.id);
                return next;
            });
            setParsingIds((prev) => {
                const next = new Set(prev);
                next.delete(file.id);
                return next;
            });
            loadFiles();
        } catch (error: any) {
            console.error("Delete failed:", error);
            toast({
                title: "Delete failed",
                description: error?.message || "Failed to delete file",
                variant: "destructive",
            });
        } finally {
            setDeletingIds((prev) => {
                const next = new Set(prev);
                next.delete(file.id);
                return next;
            });
        }
    };

    const formatFileSize = (bytes: number): string => {
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    };

    const getStatusIcon = (file: ReferenceFile) => {
        if (parsingIds.has(file.id) || file.parse_status === "parsing") {
            return <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />;
        }
        switch (file.parse_status) {
            case "completed":
                return <CheckCircle2 className="w-4 h-4 text-green-500" />;
            case "failed":
                return <XCircle className="w-4 h-4 text-red-500" />;
            case "pending":
                return <Clock className="w-4 h-4 text-muted-foreground" />;
            default:
                return null;
        }
    };

    const getStatusText = (file: ReferenceFile) => {
        if (parsingIds.has(file.id) || file.parse_status === "parsing") {
            return "Parsing...";
        }
        switch (file.parse_status) {
            case "pending":
                return "Pending";
            case "completed":
                return "Completed";
            case "failed":
                return "Failed";
            default:
                return "";
        }
    };

    return (
        <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
            <DialogContent className="max-w-2xl max-h-[90vh] flex flex-col">
                <DialogHeader>
                    <DialogTitle>Select Reference Files</DialogTitle>
                </DialogHeader>

                {/* Toolbar */}
                <div className="flex items-center justify-between gap-4 flex-wrap">
                    <div className="flex items-center gap-2 text-sm text-muted-foreground">
                        <span>
                            {files.length > 0
                                ? `${files.length} files`
                                : "No files"}
                        </span>
                        {selectedIds.size > 0 && (
                            <span className="text-primary">
                                {selectedIds.size} selected
                            </span>
                        )}
                    </div>
                    <div className="flex items-center gap-2">
                        <Select
                            value={filterScope}
                            onValueChange={setFilterScope}
                        >
                            <SelectTrigger className="w-[160px]">
                                <SelectValue placeholder="Filter" />
                            </SelectTrigger>
                            <SelectContent>
                                <SelectItem value="all">All Files</SelectItem>
                                <SelectItem value="none">No Project</SelectItem>
                                {projectId && (
                                    <SelectItem value={projectId}>
                                        Current Project
                                    </SelectItem>
                                )}
                            </SelectContent>
                        </Select>

                        <Button
                            variant="outline"
                            size="sm"
                            onClick={loadFiles}
                            disabled={isLoading}
                        >
                            <RefreshCw
                                className={cn(
                                    "w-4 h-4 mr-1",
                                    isLoading && "animate-spin",
                                )}
                            />
                            Refresh
                        </Button>

                        <Button
                            variant="outline"
                            size="sm"
                            onClick={() => fileInputRef.current?.click()}
                            disabled={isUploading}
                        >
                            <Upload className="w-4 h-4 mr-1" />
                            {isUploading ? "Uploading..." : "Upload"}
                        </Button>

                        {selectedIds.size > 0 && (
                            <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => setSelectedIds(new Set())}
                            >
                                Clear
                            </Button>
                        )}
                    </div>
                </div>

                {/* Hidden file input */}
                <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    accept=".pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.csv,.txt,.md"
                    onChange={handleUpload}
                    className="hidden"
                />

                {/* File list */}
                <div className="flex-1 border rounded-lg overflow-y-auto min-h-0 max-h-96">
                    {isLoading ? (
                        <div className="flex items-center justify-center py-12">
                            <Loader2 className="w-6 h-6 text-muted-foreground animate-spin" />
                            <span className="ml-2 text-muted-foreground">
                                Loading...
                            </span>
                        </div>
                    ) : files.length === 0 ? (
                        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
                            <FileText className="w-12 h-12 mb-2" />
                            <p>No reference files</p>
                            <p className="text-sm mt-1">
                                Click "Upload" to add files
                            </p>
                        </div>
                    ) : (
                        <div className="divide-y">
                            {files.map((file) => {
                                const isSelected = selectedIds.has(file.id);
                                const isDeleting = deletingIds.has(file.id);

                                return (
                                    <div
                                        key={file.id}
                                        onClick={() => handleSelectFile(file)}
                                        className={cn(
                                            "p-4 cursor-pointer transition-colors",
                                            isSelected &&
                                                "bg-primary/5 border-l-4 border-l-primary",
                                            file.parse_status === "failed" &&
                                                "opacity-60",
                                            !isSelected && "hover:bg-muted/50",
                                        )}
                                    >
                                        <div className="flex items-start gap-3">
                                            {/* Checkbox */}
                                            <div className="flex-shrink-0 mt-1">
                                                <div
                                                    className={cn(
                                                        "w-5 h-5 rounded border-2 flex items-center justify-center",
                                                        isSelected
                                                            ? "bg-primary border-primary"
                                                            : "border-muted-foreground/30",
                                                    )}
                                                >
                                                    {isSelected && (
                                                        <CheckCircle2 className="w-4 h-4 text-primary-foreground" />
                                                    )}
                                                </div>
                                            </div>

                                            {/* File icon */}
                                            <div className="flex-shrink-0">
                                                <div className="w-10 h-10 bg-blue-50 dark:bg-blue-900/20 rounded-lg flex items-center justify-center">
                                                    <FileText className="w-5 h-5 text-blue-600" />
                                                </div>
                                            </div>

                                            {/* File info */}
                                            <div className="flex-1 min-w-0">
                                                <div className="flex items-center gap-2">
                                                    <p className="text-sm font-medium truncate">
                                                        {file.filename}
                                                    </p>
                                                    <span className="text-xs text-muted-foreground flex-shrink-0">
                                                        {formatFileSize(
                                                            file.file_size,
                                                        )}
                                                    </span>
                                                </div>

                                                {/* Status */}
                                                <div className="flex items-center gap-1.5 mt-1">
                                                    {getStatusIcon(file)}
                                                    <p className="text-xs text-muted-foreground">
                                                        {getStatusText(file)}
                                                        {file.parse_status ===
                                                            "pending" && (
                                                            <span className="ml-1 text-orange-500">
                                                                (will parse on
                                                                confirm)
                                                            </span>
                                                        )}
                                                    </p>
                                                </div>

                                                {/* Error message */}
                                                {file.parse_status ===
                                                    "failed" &&
                                                    file.error_message && (
                                                        <p className="text-xs text-destructive mt-1 line-clamp-1">
                                                            {file.error_message}
                                                        </p>
                                                    )}
                                            </div>

                                            {/* Delete button */}
                                            <button
                                                onClick={(e) =>
                                                    handleDelete(e, file)
                                                }
                                                disabled={isDeleting}
                                                className="flex-shrink-0 p-1 text-muted-foreground hover:text-destructive hover:bg-destructive/10 rounded transition-colors disabled:opacity-50"
                                                title="Delete file"
                                            >
                                                {isDeleting ? (
                                                    <Loader2 className="w-4 h-4 animate-spin" />
                                                ) : (
                                                    <X className="w-4 h-4" />
                                                )}
                                            </button>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </div>

                <DialogFooter className="flex items-center justify-between">
                    <p className="text-xs text-muted-foreground">
                        Tip: Selecting unprocessed files will start parsing
                        automatically
                    </p>
                    <div className="flex items-center gap-2">
                        <Button variant="outline" onClick={onClose}>
                            Cancel
                        </Button>
                        <Button
                            onClick={handleConfirm}
                            disabled={selectedIds.size === 0}
                        >
                            Confirm ({selectedIds.size})
                        </Button>
                    </div>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}

export default ReferenceFileSelector;
