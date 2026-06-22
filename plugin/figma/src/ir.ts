/**
 * Plugin-side mirror of the platform's Design IR (consumption only).
 *
 * Matches the output of the backend `ir_to_plugin_payload`: parent-relative
 * coordinates, 0..1 RGBA colors, and image fills referencing the `images` map.
 */
export interface PluginColor {
  r: number
  g: number
  b: number
  a?: number
}

export interface PluginGradientStop {
  position: number
  color: PluginColor
}

export interface PluginPaint {
  type: "SOLID" | "IMAGE" | "GRADIENT_LINEAR"
  color?: PluginColor
  opacity?: number
  imageRef?: string
  // GRADIENT_LINEAR only: ordered stops + angle in degrees (0 = left->right,
  // 90 = top->bottom). Turned into a Figma gradientTransform on the plugin side.
  gradientStops?: PluginGradientStop[]
  angle?: number
}

export interface PluginNode {
  type: "DOCUMENT" | "FRAME" | "GROUP" | "RECTANGLE" | "TEXT" | "IMAGE"
  name: string
  x: number
  y: number
  width: number
  height: number
  opacity?: number
  fills?: PluginPaint[]
  strokes?: PluginPaint[]
  strokeWeight?: number
  cornerRadius?: number
  cornerRadii?: number[] // [topLeft, topRight, bottomRight, bottomLeft]
  characters?: string
  fontSize?: number
  fontFamily?: string
  fontWeight?: number | null
  textAlignHorizontal?: string
  lineHeight?: number // pixels
  letterSpacing?: number // pixels
  // Auto-layout hints (FRAME only). Applied by the plugin only when visually
  // safe (single-axis, non-overlapping, ~uniform gaps); otherwise children keep
  // their absolute x/y. See maybeApplyAutoLayout in code.ts.
  layoutMode?: "HORIZONTAL" | "VERTICAL"
  itemSpacing?: number
  padding?: { top: number; right: number; bottom: number; left: number }
  children?: PluginNode[]
}

export interface PluginPayload {
  ir_version: string
  source: string
  name: string
  images: Record<string, string> // imageRef -> data URL
  root: PluginNode
}

/** What the UI iframe forwards to the plugin main thread. */
export interface BuildMessage {
  type: "build"
  payload: PluginPayload
  /** image ref -> raw bytes (decoded from data URLs in the iframe). */
  imageBytes: Record<string, Uint8Array>
}
