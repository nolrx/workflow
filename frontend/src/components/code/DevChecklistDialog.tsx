/**
 * Dev Mode functional-checklist dialog.
 *
 * The persistent功能清单 (task board) used to live as a fixed pane in the dev
 * page's right column; it now opens on demand from the task-planning area so the
 * live preview can use the full height. Self-contained: reads the board + task
 * mutations straight from devStore, so it can be dropped anywhere (here, the
 * planner header) without prop threading. The trigger button doubles as a live
 * progress readout (functional done/total).
 */
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ListChecks } from "lucide-react"

import { DevChecklistPanel } from "@/components/code/DevChecklistPanel"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { useDevStore } from "@/stores/devStore"

export function DevChecklistDialog() {
  const { t } = useTranslation("code")
  const board = useDevStore((s) => s.board)
  const setTaskStatus = useDevStore((s) => s.setTaskStatus)
  const addTask = useDevStore((s) => s.addTask)
  const [open, setOpen] = useState(false)

  const done = board?.functional_done ?? 0
  const total = board?.functional_total ?? 0

  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
        <ListChecks className="mr-1 h-4 w-4" />
        {t("dev.checklistTitle")}
        <span className="ml-1 text-muted-foreground">
          {done}/{total}
        </span>
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="flex max-h-[80vh] max-w-lg flex-col gap-0 overflow-hidden p-0">
          {/* Panel已自带可见标题/进度条,这里的 DialogTitle 仅供无障碍朗读,视觉隐藏。 */}
          <DialogHeader className="sr-only">
            <DialogTitle>{t("dev.checklistTitle")}</DialogTitle>
          </DialogHeader>
          <div className="h-[60vh] min-h-0">
            <DevChecklistPanel
              board={board}
              onToggle={(id, status) => void setTaskStatus(id, status)}
              onAdd={(title) => void addTask(title)}
            />
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
