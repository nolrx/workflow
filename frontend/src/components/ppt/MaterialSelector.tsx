/**
 * Material Selector Component
 * Browse and select materials from library
 */
import { useState, useEffect, useCallback } from "react";
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
import { Checkbox } from "@/components/ui/checkbox";
import { useToast } from "@/hooks/use-toast";
import {
    Image as ImageIcon,
    RefreshCw,
    Upload,
    X,
    Check,
    Loader2,
} from "lucide-react";
import { pptApi, type Material } from "@/api";

interface MaterialSelectorProps {
    projectId?: string;
    isOpen: boolean;
    onClose: () => void;
    onSelect: (materials: Material[], saveAsTemplate?: boolean) => void;
    multiple?: boolean;
    maxSelection?: number;
    showSaveAsTemplateOption?: boolean;
}

export function MaterialSelector({
    projectId,
    isOpen,
    onClose,
    onSelect,
    multiple = false,
    maxSelection,
    showSaveAsTemplateOption = false,
}: MaterialSelectorProps) {
    const { toast } = useToast();
    const [materials, setMaterials] = useState<Material[]>([]);
    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
    const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());
    const [isLoading, setIsLoading] = useState(false);
    const [isUploading, setIsUploading] = useState(false);
    const [filterScope, setFilterScope] = useState<string>("all");
    const [saveAsTemplate, setSaveAsTemplate] = useState(true);

    const loadMaterials = useCallback(async () => {
        setIsLoading(true);
        try {
            const targetProjectId =
                filterScope === "all"
                    ? "all"
                    : filterScope === "none"
                      ? "none"
                      : filterScope;
            const response = await pptApi.listMaterials(targetProjectId);
            if (response?.materials) {
                setMaterials(response.materials);
            }
        } catch (error: any) {
            console.error("Failed to load materials:", error);
            toast({
                title: "Error",
                description: error?.message || "Failed to load materials",
                variant: "destructive",
            });
        } finally {
            setIsLoading(false);
        }
    }, [filterScope, toast]);

    useEffect(() => {
        if (isOpen) {
            loadMaterials();
            setSelectedIds(new Set());
        }
    }, [isOpen, loadMaterials]);

    const handleSelectMaterial = (material: Material) => {
        const id = material.id;
        if (multiple) {
            const newSelected = new Set(selectedIds);
            if (newSelected.has(id)) {
                newSelected.delete(id);
            } else {
                if (maxSelection && newSelected.size >= maxSelection) {
                    toast({
                        title: "Selection limit",
                        description: `Maximum ${maxSelection} materials can be selected`,
                    });
                    return;
                }
                newSelected.add(id);
            }
            setSelectedIds(newSelected);
        } else {
            setSelectedIds(new Set([id]));
        }
    };

    const handleConfirm = () => {
        const selected = materials.filter((m) => selectedIds.has(m.id));
        if (selected.length === 0) {
            toast({
                title: "No selection",
                description: "Please select at least one material",
            });
            return;
        }
        onSelect(
            selected,
            showSaveAsTemplateOption ? saveAsTemplate : undefined,
        );
        onClose();
    };

    const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        const allowedTypes = [
            "image/png",
            "image/jpeg",
            "image/jpg",
            "image/gif",
            "image/webp",
        ];
        if (!allowedTypes.includes(file.type)) {
            toast({
                title: "Invalid format",
                description: "Unsupported image format",
                variant: "destructive",
            });
            return;
        }

        setIsUploading(true);
        try {
            const targetProjectId =
                filterScope === "all" || filterScope === "none"
                    ? undefined
                    : filterScope;
            await pptApi.uploadMaterial(file, targetProjectId);
            toast({
                title: "Success",
                description: "Material uploaded successfully",
            });
            loadMaterials();
        } catch (error: any) {
            console.error("Upload failed:", error);
            toast({
                title: "Upload failed",
                description: error?.message || "Failed to upload material",
                variant: "destructive",
            });
        } finally {
            setIsUploading(false);
            e.target.value = "";
        }
    };

    const handleDelete = async (e: React.MouseEvent, material: Material) => {
        e.stopPropagation();
        if (!material.id) return;

        setDeletingIds((prev) => new Set(prev).add(material.id));
        try {
            await pptApi.deleteMaterial(material.id);
            setMaterials((prev) => prev.filter((m) => m.id !== material.id));
            setSelectedIds((prev) => {
                const next = new Set(prev);
                next.delete(material.id);
                return next;
            });
            toast({
                title: "Deleted",
                description: "Material deleted successfully",
            });
        } catch (error: any) {
            console.error("Delete failed:", error);
            toast({
                title: "Delete failed",
                description: error?.message || "Failed to delete material",
                variant: "destructive",
            });
        } finally {
            setDeletingIds((prev) => {
                const next = new Set(prev);
                next.delete(material.id);
                return next;
            });
        }
    };

    const getMaterialDisplayName = (m: Material) =>
        m.prompt?.trim() ||
        m.name?.trim() ||
        m.original_filename?.trim() ||
        m.filename ||
        "Untitled";

    return (
        <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
            <DialogContent className="max-w-3xl max-h-[90vh] flex flex-col">
                <DialogHeader>
                    <DialogTitle>Select Materials</DialogTitle>
                </DialogHeader>

                {/* Toolbar */}
                <div className="flex items-center justify-between gap-4 flex-wrap">
                    <div className="flex items-center gap-2 text-sm text-muted-foreground">
                        <span>
                            {materials.length > 0
                                ? `${materials.length} materials`
                                : "No materials"}
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
                                <SelectItem value="all">
                                    All Materials
                                </SelectItem>
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
                            onClick={loadMaterials}
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

                        <label>
                            <Button
                                variant="outline"
                                size="sm"
                                disabled={isUploading}
                                asChild
                            >
                                <span>
                                    <Upload className="w-4 h-4 mr-1" />
                                    {isUploading ? "Uploading..." : "Upload"}
                                </span>
                            </Button>
                            <input
                                type="file"
                                accept="image/*"
                                onChange={handleUpload}
                                className="hidden"
                                disabled={isUploading}
                            />
                        </label>

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

                {/* Material Grid */}
                <div className="flex-1 overflow-y-auto min-h-0">
                    {isLoading && materials.length === 0 ? (
                        <div className="flex items-center justify-center py-12">
                            <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
                        </div>
                    ) : materials.length === 0 ? (
                        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
                            <ImageIcon className="w-12 h-12 mb-4 opacity-50" />
                            <div className="text-sm">No materials</div>
                            <div className="text-xs mt-1">
                                Upload images to get started
                            </div>
                        </div>
                    ) : (
                        <div className="grid grid-cols-4 gap-4 p-4">
                            {materials.map((material) => {
                                const isSelected = selectedIds.has(material.id);
                                const isDeleting = deletingIds.has(material.id);
                                return (
                                    <div
                                        key={material.id}
                                        onClick={() =>
                                            handleSelectMaterial(material)
                                        }
                                        className={cn(
                                            "aspect-video rounded-lg border-2 cursor-pointer transition-all relative group overflow-hidden",
                                            isSelected
                                                ? "border-primary ring-2 ring-primary/20"
                                                : "border-border hover:border-primary/50",
                                        )}
                                    >
                                        <img
                                            src={material.url}
                                            alt={getMaterialDisplayName(
                                                material,
                                            )}
                                            className="absolute inset-0 w-full h-full object-cover"
                                        />
                                        {/* Delete button */}
                                        <button
                                            type="button"
                                            onClick={(e) =>
                                                handleDelete(e, material)
                                            }
                                            disabled={isDeleting}
                                            className="absolute -top-2 -right-2 w-6 h-6 bg-destructive text-destructive-foreground rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity shadow z-10"
                                        >
                                            {isDeleting ? (
                                                <RefreshCw className="w-3 h-3 animate-spin" />
                                            ) : (
                                                <X className="w-3 h-3" />
                                            )}
                                        </button>
                                        {/* Selection indicator */}
                                        {isSelected && (
                                            <div className="absolute inset-0 bg-primary/20 flex items-center justify-center">
                                                <div className="bg-primary text-primary-foreground rounded-full w-6 h-6 flex items-center justify-center">
                                                    <Check className="w-4 h-4" />
                                                </div>
                                            </div>
                                        )}
                                        {/* Filename on hover */}
                                        <div className="absolute bottom-0 left-0 right-0 bg-black/60 text-white text-xs p-1 truncate opacity-0 group-hover:opacity-100 transition-opacity">
                                            {getMaterialDisplayName(material)}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </div>

                <DialogFooter className="flex-col gap-3 sm:flex-col">
                    {/* Save as template option */}
                    {showSaveAsTemplateOption && (
                        <div className="w-full p-3 bg-muted rounded-lg">
                            <label className="flex items-center gap-2 cursor-pointer">
                                <Checkbox
                                    checked={saveAsTemplate}
                                    onCheckedChange={(
                                        checked: boolean | "indeterminate",
                                    ) => setSaveAsTemplate(checked === true)}
                                />
                                <span className="text-sm">
                                    Save to my template library
                                </span>
                            </label>
                        </div>
                    )}

                    <div className="flex justify-end gap-2 w-full">
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

export default MaterialSelector;
