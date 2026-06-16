/**
 * Slide Card Component
 * Displays a single slide with image preview and outline/description
 */
import { useState } from "react";
import { cn } from "@/lib/utils";
import { StatusBadge } from "./StatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuItem,
    DropdownMenuSeparator,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
    MoreVertical,
    RefreshCw,
    Trash2,
    Image as ImageIcon,
    FileText,
    Check,
} from "lucide-react";
import type { PPTPage } from "@/stores/pptStore";

interface SlideCardProps {
    page: PPTPage;
    index: number;
    isSelected?: boolean;
    isGenerating?: boolean;
    onSelect?: () => void;
    onRegenerateImage?: () => void;
    onRegenerateDescription?: () => void;
    onDelete?: () => void;
    onEdit?: () => void;
    className?: string;
}

export function SlideCard({
    page,
    index,
    isSelected = false,
    isGenerating = false,
    onSelect,
    onRegenerateImage,
    onRegenerateDescription,
    onDelete,
    onEdit,
    className,
}: SlideCardProps) {
    const [imageError, setImageError] = useState(false);

    const hasImage = page.generated_image_url && !imageError;
    const title = page.outline_content?.title || `Slide ${index + 1}`;
    const points = page.outline_content?.points || [];

    return (
        <Card
            className={cn(
                "relative overflow-hidden transition-all cursor-pointer hover:shadow-md",
                isSelected && "ring-2 ring-primary",
                isGenerating && "opacity-70",
                className,
            )}
            onClick={onSelect}
        >
            {/* Selection indicator */}
            {onSelect && (
                <div
                    className={cn(
                        "absolute top-2 left-2 z-10 w-5 h-5 rounded border-2 flex items-center justify-center transition-colors",
                        isSelected
                            ? "bg-primary border-primary text-primary-foreground"
                            : "bg-white/80 border-gray-300 hover:border-primary",
                    )}
                    onClick={(e) => {
                        e.stopPropagation();
                        onSelect();
                    }}
                >
                    {isSelected && <Check className="w-3 h-3" />}
                </div>
            )}

            {/* Page number badge */}
            <div className="absolute top-2 right-2 z-10 px-2 py-0.5 bg-black/50 text-white text-xs rounded">
                {index + 1}
            </div>

            {/* Image area */}
            <div className="relative aspect-video bg-gray-100 dark:bg-gray-800">
                {hasImage ? (
                    <img
                        src={page.generated_image_url!}
                        alt={title}
                        className="w-full h-full object-cover"
                        onError={() => setImageError(true)}
                    />
                ) : (
                    <div className="w-full h-full flex items-center justify-center text-gray-400">
                        <ImageIcon className="w-12 h-12" />
                    </div>
                )}

                {/* Generating overlay */}
                {isGenerating && (
                    <div className="absolute inset-0 bg-black/50 flex items-center justify-center">
                        <RefreshCw className="w-8 h-8 text-white animate-spin" />
                    </div>
                )}
            </div>

            <CardContent className="p-3">
                {/* Title and status */}
                <div className="flex items-start justify-between gap-2 mb-2">
                    <h3 className="font-medium text-sm line-clamp-1 flex-1">
                        {title}
                    </h3>
                    <StatusBadge
                        status={page.status}
                        size="sm"
                        showLabel={false}
                    />
                </div>

                {/* Part label if exists */}
                {page.part && (
                    <div className="text-xs text-muted-foreground mb-2">
                        Part: {page.part}
                    </div>
                )}

                {/* Points preview */}
                {points.length > 0 && (
                    <ul className="text-xs text-muted-foreground space-y-0.5">
                        {points.slice(0, 3).map((point, i) => (
                            <li key={i} className="line-clamp-1">
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

                {/* Actions dropdown */}
                {(onRegenerateImage ||
                    onRegenerateDescription ||
                    onDelete ||
                    onEdit) && (
                    <div className="mt-2 pt-2 border-t flex justify-end">
                        <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    className="h-7 w-7 p-0"
                                    onClick={(e) => e.stopPropagation()}
                                >
                                    <MoreVertical className="w-4 h-4" />
                                </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                                {onEdit && (
                                    <DropdownMenuItem onClick={onEdit}>
                                        <FileText className="w-4 h-4 mr-2" />
                                        Edit
                                    </DropdownMenuItem>
                                )}
                                {onRegenerateDescription && (
                                    <DropdownMenuItem
                                        onClick={onRegenerateDescription}
                                    >
                                        <FileText className="w-4 h-4 mr-2" />
                                        Regenerate Description
                                    </DropdownMenuItem>
                                )}
                                {onRegenerateImage && (
                                    <DropdownMenuItem
                                        onClick={onRegenerateImage}
                                    >
                                        <ImageIcon className="w-4 h-4 mr-2" />
                                        Regenerate Image
                                    </DropdownMenuItem>
                                )}
                                {onDelete && (
                                    <>
                                        <DropdownMenuSeparator />
                                        <DropdownMenuItem
                                            onClick={onDelete}
                                            className="text-destructive focus:text-destructive"
                                        >
                                            <Trash2 className="w-4 h-4 mr-2" />
                                            Delete
                                        </DropdownMenuItem>
                                    </>
                                )}
                            </DropdownMenuContent>
                        </DropdownMenu>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}

export default SlideCard;
