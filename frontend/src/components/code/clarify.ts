import type { AgentRun } from "@/api/agent"
import type { ClarificationQuestion } from "@/api/code"

/**
 * Extract the latest requirements clarification questionnaire from a run's
 * artifacts. Each requirements (re)generation attaches a fresh
 * `requirements_questions.json` artifact, so the newest by creation time is the
 * current questionnaire. Returns [] when none has been produced (or the AI
 * judged the requirement clear enough). No new schema — read straight from the
 * run snapshot. See docs/requirements-clarify-spec.md.
 */
export function selectRequirementsQuestions(run: AgentRun | null): ClarificationQuestion[] {
  let latest: { time: number; questions: ClarificationQuestion[] } | null = null
  for (const artifact of run?.artifacts ?? []) {
    if (artifact.filename !== "requirements_questions.json") continue
    const content = artifact.content_json as { questions?: ClarificationQuestion[] } | null
    if (!Array.isArray(content?.questions)) continue
    // Compare numerically: the backend serializes created_at as isoformat()+"Z",
    // which OMITS the fractional part when microsecond==0, so a lexicographic
    // string compare is non-monotonic ("…00Z" > "…00.5Z"). Unparseable timestamps
    // sort as -Infinity; ties keep the later element (artifacts arrive
    // created_at-ascending), so the newest questionnaire always wins.
    const parsed = Date.parse(artifact.created_at ?? "")
    const time = Number.isNaN(parsed) ? -Infinity : parsed
    if (!latest || time >= latest.time) latest = { time, questions: content.questions }
  }
  return latest?.questions ?? []
}
