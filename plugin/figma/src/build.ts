/**
 * Pure mapping helpers: Design IR values -> Figma primitives.
 *
 * Kept side-effect free (no `figma.*`) so the node-creation code in code.ts stays
 * thin and these can be reasoned about / tested independently.
 */
import type { PluginColor, PluginPaint } from "./ir"

/**
 * Map a 0..1 paint list to Figma paints, preserving order. Handles SOLID and
 * GRADIENT_LINEAR; IMAGE paints are applied separately in code.ts (they need the
 * decoded bytes). Replaces the old solid-only helper so vector blocks restored
 * from a sliced thumbnail can carry gradients.
 */
export function paintsFrom(paints: PluginPaint[] | undefined): Paint[] {
  if (!paints) return []
  const out: Paint[] = []
  for (const paint of paints) {
    if (paint.type === "SOLID" && paint.color) {
      out.push({
        type: "SOLID",
        color: clampColor(paint.color),
        opacity: clamp01(paint.opacity == null ? 1 : paint.opacity),
      })
    } else if (paint.type === "GRADIENT_LINEAR" && paint.gradientStops?.length) {
      out.push(gradientPaint(paint))
    }
  }
  return out
}

function gradientPaint(paint: PluginPaint): GradientPaint {
  const stops: ColorStop[] = (paint.gradientStops || []).map((stop) => ({
    position: clamp01(stop.position),
    color: {
      r: clamp01(stop.color.r),
      g: clamp01(stop.color.g),
      b: clamp01(stop.color.b),
      a: clamp01(stop.color.a == null ? 1 : stop.color.a),
    },
  }))
  return {
    type: "GRADIENT_LINEAR",
    gradientTransform: gradientTransformFromAngle(paint.angle == null ? 90 : paint.angle),
    gradientStops: stops,
    opacity: clamp01(paint.opacity == null ? 1 : paint.opacity),
  }
}

/**
 * Affine transform mapping object space (0..1) to gradient space for a linear
 * gradient at `angleDeg` (0 = left->right, 90 = top->bottom). Centered in the
 * unit square so the gradient spans the node. Verified at the cardinal angles.
 */
export function gradientTransformFromAngle(angleDeg: number): Transform {
  const angle = ((angleDeg % 360) * Math.PI) / 180
  const cos = Math.cos(angle)
  const sin = Math.sin(angle)
  return [
    [cos, sin, (1 - cos - sin) / 2],
    [-sin, cos, (sin + 1 - cos) / 2],
  ]
}

export function clampColor(color: PluginColor): RGB {
  return { r: clamp01(color.r), g: clamp01(color.g), b: clamp01(color.b) }
}

export function clamp01(value: number): number {
  if (Number.isNaN(value)) return 0
  return Math.max(0, Math.min(1, value))
}

/** Translate a numeric font weight to a Figma font style name. */
export function fontStyleFromWeight(weight: number | null | undefined): string {
  if (!weight) return "Regular"
  if (weight >= 800) return "Bold"
  if (weight >= 700) return "Bold"
  if (weight >= 600) return "Semi Bold"
  if (weight >= 500) return "Medium"
  if (weight <= 300) return "Light"
  return "Regular"
}

const VALID_ALIGN = new Set(["LEFT", "CENTER", "RIGHT", "JUSTIFIED"])

export function textAlign(value: string | undefined): "LEFT" | "CENTER" | "RIGHT" | "JUSTIFIED" {
  const upper = (value || "LEFT").toUpperCase()
  return (VALID_ALIGN.has(upper) ? upper : "LEFT") as
    | "LEFT"
    | "CENTER"
    | "RIGHT"
    | "JUSTIFIED"
}
