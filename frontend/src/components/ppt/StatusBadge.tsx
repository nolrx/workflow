/**
 * Status Badge Component
 * Displays page/task status with appropriate styling
 */
import { cn } from "@/lib/utils";

export type PageStatus =
    | "DRAFT"
    | "OUTLINE_GENERATED"
    | "DESCRIPTION_GENERATED"
    | "IMAGE_GENERATED"
    | "GENERATING"
    | "COMPLETED"
    | "FAILED";

interface StatusBadgeProps {
    status: PageStatus | string;
    size?: "sm" | "md" | "lg";
    showLabel?: boolean;
    className?: string;
}

const statusConfig: Record<
    string,
    { label: string; bgColor: string; textColor: string; dotColor: string }
> = {
    DRAFT: {
        label: "Draft",
        bgColor: "bg-gray-100 dark:bg-gray-800",
        textColor: "text-gray-600 dark:text-gray-400",
        dotColor: "bg-gray-400",
    },
    OUTLINE_GENERATED: {
        label: "Outline",
        bgColor: "bg-blue-100 dark:bg-blue-900/30",
        textColor: "text-blue-600 dark:text-blue-400",
        dotColor: "bg-blue-500",
    },
    DESCRIPTION_GENERATED: {
        label: "Description",
        bgColor: "bg-purple-100 dark:bg-purple-900/30",
        textColor: "text-purple-600 dark:text-purple-400",
        dotColor: "bg-purple-500",
    },
    IMAGE_GENERATED: {
        label: "Image Ready",
        bgColor: "bg-green-100 dark:bg-green-100",
        textColor: "text-green-600 dark:text-green-400",
        dotColor: "bg-green-500",
    },
    GENERATING: {
        label: "Generating",
        bgColor: "bg-yellow-100 dark:bg-yellow-900/30",
        textColor: "text-yellow-600 dark:text-yellow-400",
        dotColor: "bg-yellow-500 animate-pulse",
    },
    COMPLETED: {
        label: "Completed",
        bgColor: "bg-green-100 dark:bg-green-100",
        textColor: "text-green-600 dark:text-green-400",
        dotColor: "bg-green-500",
    },
    FAILED: {
        label: "Failed",
        bgColor: "bg-red-100 dark:bg-red-900/30",
        textColor: "text-red-600 dark:text-red-400",
        dotColor: "bg-red-500",
    },
    PENDING: {
        label: "Pending",
        bgColor: "bg-gray-100 dark:bg-gray-800",
        textColor: "text-gray-600 dark:text-gray-400",
        dotColor: "bg-gray-400",
    },
    PROCESSING: {
        label: "Processing",
        bgColor: "bg-yellow-100 dark:bg-yellow-900/30",
        textColor: "text-yellow-600 dark:text-yellow-400",
        dotColor: "bg-yellow-500 animate-pulse",
    },
};

const sizeConfig = {
    sm: {
        badge: "px-1.5 py-0.5 text-xs",
        dot: "w-1.5 h-1.5",
    },
    md: {
        badge: "px-2 py-1 text-sm",
        dot: "w-2 h-2",
    },
    lg: {
        badge: "px-3 py-1.5 text-base",
        dot: "w-2.5 h-2.5",
    },
};

export function StatusBadge({
    status,
    size = "md",
    showLabel = true,
    className,
}: StatusBadgeProps) {
    const config = statusConfig[status] || statusConfig.DRAFT;
    const sizeStyles = sizeConfig[size];

    return (
        <span
            className={cn(
                "inline-flex items-center gap-1.5 rounded-full font-medium",
                config.bgColor,
                config.textColor,
                sizeStyles.badge,
                className,
            )}
        >
            <span
                className={cn("rounded-full", config.dotColor, sizeStyles.dot)}
            />
            {showLabel && <span>{config.label}</span>}
        </span>
    );
}

export default StatusBadge;
