/**
 * Status badges for the App Space — deployment status, health, run status,
 * iteration status and risk level. Each maps a backend value to a shadcn Badge
 * variant + an i18n label (apps namespace), so colors stay consistent everywhere.
 */
import { useTranslation } from "react-i18next"
import { Badge } from "@/components/ui/badge"

type Variant = "default" | "secondary" | "destructive" | "outline"

const DEPLOY_VARIANT: Record<string, Variant> = {
  running: "default",
  starting: "secondary",
  building: "secondary",
  provisioning: "secondary",
  pending: "outline",
  failed: "destructive",
  stopped: "outline",
  rolled_back: "destructive",
}

const HEALTH_VARIANT: Record<string, Variant> = {
  healthy: "default",
  unhealthy: "destructive",
  unknown: "outline",
}

const RUN_VARIANT: Record<string, Variant> = {
  completed: "default",
  running: "secondary",
  queued: "secondary",
  paused: "secondary",
  partial: "secondary",
  failed: "destructive",
  cancelled: "outline",
}

const ITERATION_VARIANT: Record<string, Variant> = {
  released: "default",
  staging_ready: "default",
  generating: "secondary",
  analyzing: "secondary",
  staging_deploying: "secondary",
  release_pending: "secondary",
  awaiting_plan_approval: "outline",
  draft: "outline",
  failed: "destructive",
  cancelled: "outline",
}

const RISK_VARIANT: Record<string, Variant> = {
  low: "outline",
  medium: "secondary",
  high: "destructive",
}

export function DeployStatusBadge({ status }: { status: string | null }) {
  const { t } = useTranslation("apps")
  const value = status || "pending"
  return <Badge variant={DEPLOY_VARIANT[value] ?? "outline"}>{t(`deployStatus.${value}`)}</Badge>
}

export function HealthBadge({ health }: { health: string | null }) {
  const { t } = useTranslation("apps")
  const value = health || "unknown"
  return <Badge variant={HEALTH_VARIANT[value] ?? "outline"}>{t(`health.${value}`)}</Badge>
}

export function RunStatusBadge({ status }: { status: string | null }) {
  const { t } = useTranslation("apps")
  if (!status) return null
  return <Badge variant={RUN_VARIANT[status] ?? "outline"}>{t(`runStatus.${status}`)}</Badge>
}

export function IterationStatusBadge({ status }: { status: string }) {
  const { t } = useTranslation("apps")
  return (
    <Badge variant={ITERATION_VARIANT[status] ?? "outline"}>{t(`iterationStatus.${status}`)}</Badge>
  )
}

export function RiskBadge({ risk }: { risk: string | undefined }) {
  const { t } = useTranslation("apps")
  if (!risk) return null
  return <Badge variant={RISK_VARIANT[risk] ?? "outline"}>{t(`iterate.riskLevels.${risk}`)}</Badge>
}
