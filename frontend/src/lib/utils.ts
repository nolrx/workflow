import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

/**
 * Merge class names with Tailwind-aware conflict resolution.
 * Standard shadcn/ui helper used across the component library.
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
