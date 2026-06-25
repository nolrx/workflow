import { useTranslation } from "react-i18next"
import { AlertTriangle, Loader2 } from "lucide-react"
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

interface DeleteSessionDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  isDeployed?: boolean
  isDeleting?: boolean
  onConfirm: () => void
}

export function DeleteSessionDialog({
  open,
  onOpenChange,
  title,
  isDeployed,
  isDeleting,
  onConfirm,
}: DeleteSessionDialogProps) {
  const { t } = useTranslation("common")

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t("sidebar.deleteConfirmTitle")}</AlertDialogTitle>
          <AlertDialogDescription>
            {isDeployed ? (
              <span className="flex items-center gap-2 text-amber-600">
                <AlertTriangle className="h-4 w-4" />
                {t("sidebar.cannotDeleteDeployed")}
              </span>
            ) : (
              t("sidebar.deleteConfirmDescription", { title })
            )}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={isDeleting}>
            {t("buttons.cancel")}
          </AlertDialogCancel>
          {!isDeployed && (
            <AlertDialogAction onClick={onConfirm} disabled={isDeleting}>
              {isDeleting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t("buttons.delete")}
            </AlertDialogAction>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
