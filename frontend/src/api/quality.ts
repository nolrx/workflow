import { api } from "@/api/client"

interface Envelope<T> {
  success: boolean
  data: T
  message?: string
}

/** Aggregated metrics over a set of CodeQualitySample rows (one bucket). */
export interface QualityBucket {
  count: number
  pass_rate: number | null
  blocked?: number
  mean_weighted_score: number | null
  mean_verify_rounds: number | null
  degraded_rate: number | null
  feature_pass_rate: number | null
  verdicts: Record<string, number>
  eval_accuracy: number | null
}

export interface QualityDay extends QualityBucket {
  day: string
}

/** GET /api/code/quality/trends payload. */
export interface QualityTrends {
  kind: string
  lane: string | null
  window_days: number
  scope: string
  overall: QualityBucket
  by_lane: Record<string, QualityBucket>
  by_prompt_version: Record<string, QualityBucket>
  by_model: Record<string, QualityBucket>
  by_day: QualityDay[]
}

export interface QualityTrendParams {
  lane?: string
  windowDays?: number
  scopeAll?: boolean
  kind?: string
}

/** Generation-quality trends (eval framework, P0-B). Owner-scoped by default;
 *  admins pass scopeAll for a platform-wide view. */
export async function getQualityTrends(
  params: QualityTrendParams = {}
): Promise<QualityTrends> {
  const q = new URLSearchParams()
  if (params.lane) q.set("lane", params.lane)
  if (params.windowDays != null) q.set("window_days", String(params.windowDays))
  if (params.scopeAll) q.set("scope", "all")
  if (params.kind) q.set("kind", params.kind)
  const qs = q.toString()
  const env = await api.get<Envelope<QualityTrends>>(
    `/code/quality/trends${qs ? `?${qs}` : ""}`
  )
  return env.data
}
