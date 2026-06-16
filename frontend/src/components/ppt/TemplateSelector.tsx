/**
 * Template Selector Component
 * Browse and select templates for PPT generation
 */
import { useState, useEffect } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { useToast } from "@/hooks/use-toast";
import { X, Upload, ImagePlus, Loader2 } from "lucide-react";
import { pptApi, type UserTemplate } from "@/api";
import { MaterialSelector } from "./MaterialSelector";
import type { Material } from "@/api";

// Preset templates
const presetTemplates = [
    {
        id: "preset-1",
        name: "Vintage Scroll",
        preview: "/templates/template_y.png",
    },
    {
        id: "preset-2",
        name: "Vector Illustration",
        preview: "/templates/template_vector_illustration.png",
    },
    {
        id: "preset-3",
        name: "Glass Effect",
        preview: "/templates/template_glass.png",
    },
    { id: "preset-4", name: "Tech Blue", preview: "/templates/template_b.png" },
    {
        id: "preset-5",
        name: "Simple Business",
        preview: "/templates/template_s.png",
    },
    {
        id: "preset-6",
        name: "Academic Report",
        preview: "/templates/template_academic.jpg",
    },
];

interface TemplateSelectorProps {
    onSelect: (templateFile: File | null, templateId?: string) => void;
    selectedTemplateId?: string | null;
    selectedPresetTemplateId?: string | null;
    showUpload?: boolean;
    projectId?: string | null;
}

export function TemplateSelector({
    onSelect,
    selectedTemplateId,
    selectedPresetTemplateId,
    showUpload = true,
    projectId,
}: TemplateSelectorProps) {
    const { toast } = useToast();
    const [userTemplates, setUserTemplates] = useState<UserTemplate[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [isMaterialSelectorOpen, setIsMaterialSelectorOpen] = useState(false);
    const [deletingId, setDeletingId] = useState<string | null>(null);
    const [saveToLibrary, setSaveToLibrary] = useState(true);

    useEffect(() => {
        loadUserTemplates();
    }, []);

    const loadUserTemplates = async () => {
        setIsLoading(true);
        try {
            const response = await pptApi.listUserTemplates();
            if (response?.templates) {
                setUserTemplates(response.templates);
            }
        } catch (error: any) {
            console.error("Failed to load templates:", error);
        } finally {
            setIsLoading(false);
        }
    };

    const handleTemplateUpload = async (
        e: React.ChangeEvent<HTMLInputElement>,
    ) => {
        const file = e.target.files?.[0];
        if (!file) return;

        try {
            if (showUpload) {
                // Main page: upload to user template library
                const response = await pptApi.uploadUserTemplate(file);
                if (response) {
                    setUserTemplates((prev) => [response, ...prev]);
                    onSelect(null, response.template_id);
                    toast({
                        title: "Success",
                        description: "Template uploaded successfully",
                    });
                }
            } else {
                // Preview page: optionally save to library
                if (saveToLibrary) {
                    const response = await pptApi.uploadUserTemplate(file);
                    if (response) {
                        setUserTemplates((prev) => [response, ...prev]);
                        onSelect(file, response.template_id);
                        toast({
                            title: "Success",
                            description: "Template saved to library",
                        });
                    }
                } else {
                    onSelect(file);
                }
            }
        } catch (error: any) {
            console.error("Upload failed:", error);
            toast({
                title: "Upload failed",
                description: error?.message || "Failed to upload template",
                variant: "destructive",
            });
        }
        e.target.value = "";
    };

    const handleSelectUserTemplate = (template: UserTemplate) => {
        onSelect(null, template.template_id);
    };

    const handleSelectPresetTemplate = (templateId: string) => {
        onSelect(null, templateId);
    };

    const handleSelectMaterials = async (
        materials: Material[],
        saveAsTemplate?: boolean,
    ) => {
        if (materials.length === 0) return;

        try {
            // Convert material URL to File
            const response = await fetch(materials[0].url);
            const blob = await response.blob();
            const file = new File([blob], "template.png", {
                type: blob.type || "image/png",
            });

            if (saveAsTemplate) {
                const uploadResponse = await pptApi.uploadUserTemplate(file);
                if (uploadResponse) {
                    setUserTemplates((prev) => [uploadResponse, ...prev]);
                    onSelect(file, uploadResponse.template_id);
                    toast({
                        title: "Success",
                        description: "Material saved as template",
                    });
                }
            } else {
                onSelect(file);
                toast({
                    title: "Selected",
                    description: "Material selected as template",
                });
            }
        } catch (error: any) {
            console.error("Failed to load material:", error);
            toast({
                title: "Error",
                description: error?.message || "Failed to load material",
                variant: "destructive",
            });
        }
    };

    const handleDeleteTemplate = async (
        template: UserTemplate,
        e: React.MouseEvent,
    ) => {
        e.stopPropagation();
        if (selectedTemplateId === template.template_id) {
            toast({
                title: "Cannot delete",
                description: "Please deselect the template first",
            });
            return;
        }

        setDeletingId(template.template_id);
        try {
            await pptApi.deleteUserTemplate(template.template_id);
            setUserTemplates((prev) =>
                prev.filter((t) => t.template_id !== template.template_id),
            );
            toast({
                title: "Deleted",
                description: "Template deleted successfully",
            });
        } catch (error: any) {
            console.error("Delete failed:", error);
            toast({
                title: "Delete failed",
                description: error?.message || "Failed to delete template",
                variant: "destructive",
            });
        } finally {
            setDeletingId(null);
        }
    };

    return (
        <>
            <div className="space-y-6">
                {/* User templates */}
                {userTemplates.length > 0 && (
                    <div>
                        <h4 className="text-sm font-medium mb-3">
                            My Templates
                        </h4>
                        <div className="grid grid-cols-4 gap-4">
                            {userTemplates.map((template) => (
                                <div
                                    key={template.template_id}
                                    onClick={() =>
                                        handleSelectUserTemplate(template)
                                    }
                                    className={cn(
                                        "aspect-[4/3] rounded-lg border-2 cursor-pointer transition-all relative group overflow-hidden",
                                        selectedTemplateId ===
                                            template.template_id
                                            ? "border-primary ring-2 ring-primary/20"
                                            : "border-border hover:border-primary/50",
                                    )}
                                >
                                    <img
                                        src={template.template_image_url}
                                        alt={template.name || "Template"}
                                        className="absolute inset-0 w-full h-full object-cover"
                                    />
                                    {/* Delete button */}
                                    {selectedTemplateId !==
                                        template.template_id && (
                                        <button
                                            type="button"
                                            onClick={(e) =>
                                                handleDeleteTemplate(
                                                    template,
                                                    e,
                                                )
                                            }
                                            disabled={
                                                deletingId ===
                                                template.template_id
                                            }
                                            className="absolute -top-2 -right-2 w-6 h-6 bg-destructive text-destructive-foreground rounded-full flex items-center justify-center shadow z-20 opacity-0 group-hover:opacity-100 transition-opacity"
                                        >
                                            {deletingId ===
                                            template.template_id ? (
                                                <Loader2 className="w-3 h-3 animate-spin" />
                                            ) : (
                                                <X className="w-3 h-3" />
                                            )}
                                        </button>
                                    )}
                                    {selectedTemplateId ===
                                        template.template_id && (
                                        <div className="absolute inset-0 bg-primary/20 flex items-center justify-center pointer-events-none">
                                            <span className="text-primary-foreground font-semibold text-sm bg-primary/80 px-2 py-1 rounded">
                                                Selected
                                            </span>
                                        </div>
                                    )}
                                </div>
                            ))}
                        </div>
                    </div>
                )}

                {/* Preset templates */}
                <div>
                    <h4 className="text-sm font-medium mb-3">
                        Preset Templates
                    </h4>
                    <div className="grid grid-cols-4 gap-4">
                        {presetTemplates.map((template) => (
                            <div
                                key={template.id}
                                onClick={() =>
                                    handleSelectPresetTemplate(template.id)
                                }
                                className={cn(
                                    "aspect-[4/3] rounded-lg border-2 cursor-pointer transition-all bg-muted flex items-center justify-center relative overflow-hidden",
                                    selectedPresetTemplateId === template.id
                                        ? "border-primary ring-2 ring-primary/20"
                                        : "border-border hover:border-primary/50",
                                )}
                            >
                                {template.preview ? (
                                    <>
                                        <img
                                            src={template.preview}
                                            alt={template.name}
                                            className="absolute inset-0 w-full h-full object-cover"
                                        />
                                        {selectedPresetTemplateId ===
                                            template.id && (
                                            <div className="absolute inset-0 bg-primary/20 flex items-center justify-center pointer-events-none">
                                                <span className="text-primary-foreground font-semibold text-sm bg-primary/80 px-2 py-1 rounded">
                                                    Selected
                                                </span>
                                            </div>
                                        )}
                                    </>
                                ) : (
                                    <span className="text-sm text-muted-foreground">
                                        {template.name}
                                    </span>
                                )}
                            </div>
                        ))}

                        {/* Upload template */}
                        <label className="aspect-[4/3] rounded-lg border-2 border-dashed border-border hover:border-primary/50 cursor-pointer transition-all flex flex-col items-center justify-center gap-2">
                            <Upload className="w-6 h-6 text-muted-foreground" />
                            <span className="text-sm text-muted-foreground">
                                Upload
                            </span>
                            <input
                                type="file"
                                accept="image/*"
                                onChange={handleTemplateUpload}
                                className="hidden"
                                disabled={isLoading}
                            />
                        </label>
                    </div>

                    {/* Save to library option */}
                    {!showUpload && (
                        <div className="mt-4 p-3 bg-muted rounded-lg">
                            <label className="flex items-center gap-2 cursor-pointer">
                                <Checkbox
                                    checked={saveToLibrary}
                                    onCheckedChange={(
                                        checked: boolean | "indeterminate",
                                    ) => setSaveToLibrary(checked === true)}
                                />
                                <span className="text-sm">
                                    Save uploaded templates to my library
                                </span>
                            </label>
                        </div>
                    )}
                </div>

                {/* Select from material library */}
                {projectId && (
                    <div>
                        <h4 className="text-sm font-medium mb-3">
                            From Material Library
                        </h4>
                        <Button
                            variant="outline"
                            onClick={() => setIsMaterialSelectorOpen(true)}
                            className="w-full"
                        >
                            <ImagePlus className="w-4 h-4 mr-2" />
                            Select from Materials
                        </Button>
                    </div>
                )}
            </div>

            {/* Material selector dialog */}
            {projectId && (
                <MaterialSelector
                    projectId={projectId}
                    isOpen={isMaterialSelectorOpen}
                    onClose={() => setIsMaterialSelectorOpen(false)}
                    onSelect={handleSelectMaterials}
                    multiple={false}
                    showSaveAsTemplateOption={true}
                />
            )}
        </>
    );
}

/**
 * Get template file by ID
 */
export const getTemplateFile = async (
    templateId: string,
    userTemplates: UserTemplate[],
): Promise<File | null> => {
    // Check preset templates
    const presetTemplate = presetTemplates.find((t) => t.id === templateId);
    if (presetTemplate && presetTemplate.preview) {
        try {
            const response = await fetch(presetTemplate.preview);
            const blob = await response.blob();
            return new File(
                [blob],
                presetTemplate.preview.split("/").pop() || "template.png",
                { type: blob.type },
            );
        } catch (error) {
            console.error("Failed to load preset template:", error);
            return null;
        }
    }

    // Check user templates
    const userTemplate = userTemplates.find(
        (t) => t.template_id === templateId,
    );
    if (userTemplate) {
        try {
            const response = await fetch(userTemplate.template_image_url);
            const blob = await response.blob();
            return new File([blob], "template.png", { type: blob.type });
        } catch (error) {
            console.error("Failed to load user template:", error);
            return null;
        }
    }

    return null;
};

export default TemplateSelector;
