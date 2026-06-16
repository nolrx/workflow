/**
 * Toast hook using sonner
 */
import { toast as sonnerToast } from "sonner"

interface ToastOptions {
  title?: string
  description?: string
  variant?: "default" | "destructive"
}

export function useToast() {
  const toast = ({ title, description, variant }: ToastOptions) => {
    if (variant === "destructive") {
      sonnerToast.error(title, { description })
    } else {
      sonnerToast(title, { description })
    }
  }

  return { toast }
}

export { sonnerToast as toast }
