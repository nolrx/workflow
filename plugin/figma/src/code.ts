/**
 * Plugin main thread: rebuild a Design IR payload as native Figma layers.
 *
 * The UI iframe does the network fetch + image decode (it has DOM/atob), then
 * forwards the payload (with images already decoded to bytes) here, where we have
 * access to `figma.*` to construct the document.
 */
import { fontStyleFromWeight, paintsFrom, textAlign } from "./build"
import type { BuildMessage, PluginNode } from "./ir"

figma.showUI(__html__, { width: 380, height: 460, themeColors: true })

figma.ui.onmessage = async (msg: BuildMessage | { type: string }) => {
  if (msg.type === "build") {
    const build = msg as BuildMessage
    try {
      const root = await buildNode(build.payload.root, build.imageBytes || {})
      figma.currentPage.appendChild(root)
      root.x = Math.round(figma.viewport.center.x - root.width / 2)
      root.y = Math.round(figma.viewport.center.y - root.height / 2)
      figma.currentPage.selection = [root]
      figma.viewport.scrollAndZoomIntoView([root])
      figma.notify(`已导入「${build.payload.name}」`)
      figma.ui.postMessage({ type: "done" })
    } catch (err) {
      const message = (err as Error).message || String(err)
      figma.notify("导入失败：" + message, { error: true })
      figma.ui.postMessage({ type: "error", message })
    }
  } else if (msg.type === "cancel") {
    figma.closePlugin()
  }
}

async function buildNode(
  node: PluginNode,
  images: Record<string, Uint8Array>
): Promise<SceneNode> {
  if (node.type === "TEXT") return buildText(node)
  if (node.type === "RECTANGLE" || node.type === "IMAGE") return buildRect(node, images)
  return buildFrame(node, images)
}

async function buildFrame(
  node: PluginNode,
  images: Record<string, Uint8Array>
): Promise<FrameNode> {
  const frame = figma.createFrame()
  frame.name = node.name || "Frame"
  frame.resizeWithoutConstraints(Math.max(1, node.width), Math.max(1, node.height))
  applyCommon(frame, node)
  // Image-filled container (preview-image export) -> set the image as the fill.
  applyImageFill(frame, node, images)

  for (const child of node.children || []) {
    const childNode = await buildNode(child, images)
    frame.appendChild(childNode)
    // Payload coordinates are already parent-relative.
    childNode.x = Math.round(child.x)
    childNode.y = Math.round(child.y)
  }
  return frame
}

function buildRect(node: PluginNode, images: Record<string, Uint8Array>): RectangleNode {
  const rect = figma.createRectangle()
  rect.name = node.name || "Rectangle"
  rect.resizeWithoutConstraints(Math.max(1, node.width), Math.max(1, node.height))
  applyCommon(rect, node)
  applyImageFill(rect, node, images)
  return rect
}

async function buildText(node: PluginNode): Promise<TextNode> {
  const text = figma.createText()
  text.name = node.name || "Text"
  const font = await loadFontSafe(node.fontFamily, fontStyleFromWeight(node.fontWeight))
  text.fontName = font
  text.characters = node.characters || ""
  if (node.fontSize) text.fontSize = Math.max(1, node.fontSize)
  text.textAlignHorizontal = textAlign(node.textAlignHorizontal)
  if (node.lineHeight != null && node.lineHeight > 0) {
    text.lineHeight = { value: node.lineHeight, unit: "PIXELS" }
  }
  if (node.letterSpacing != null) {
    text.letterSpacing = { value: node.letterSpacing, unit: "PIXELS" }
  }
  const fills = paintsFrom(node.fills)
  if (fills.length) text.fills = fills
  text.textAutoResize = "NONE"
  text.resizeWithoutConstraints(Math.max(1, node.width), Math.max(1, node.height))
  if (node.opacity != null) text.opacity = node.opacity
  return text
}

function applyCommon(node: FrameNode | RectangleNode, ir: PluginNode): void {
  const fills = paintsFrom(ir.fills)
  // Image fills are applied separately; only set solid/gradient fills here.
  if (fills.length) node.fills = fills
  const strokes = paintsFrom(ir.strokes)
  if (strokes.length) {
    node.strokes = strokes
    if (ir.strokeWeight != null) node.strokeWeight = Math.max(0, ir.strokeWeight)
  }
  // Per-corner radii (if provided) win; else fall back to the single scalar.
  // Both props exist on FrameNode and RectangleNode.
  if (ir.cornerRadii && ir.cornerRadii.length === 4) {
    const rect = node as RectangleNode
    rect.topLeftRadius = Math.max(0, ir.cornerRadii[0])
    rect.topRightRadius = Math.max(0, ir.cornerRadii[1])
    rect.bottomRightRadius = Math.max(0, ir.cornerRadii[2])
    rect.bottomLeftRadius = Math.max(0, ir.cornerRadii[3])
  } else if (ir.cornerRadius != null) {
    ;(node as RectangleNode).cornerRadius = Math.max(0, ir.cornerRadius)
  }
  if (ir.opacity != null) node.opacity = ir.opacity
}

function applyImageFill(
  node: FrameNode | RectangleNode,
  ir: PluginNode,
  images: Record<string, Uint8Array>
): void {
  const imagePaint = (ir.fills || []).find((paint) => paint.type === "IMAGE" && paint.imageRef)
  if (!imagePaint || !imagePaint.imageRef) return
  const bytes = images[imagePaint.imageRef]
  if (!bytes) return
  const image = figma.createImage(bytes)
  node.fills = [{ type: "IMAGE", scaleMode: "FILL", imageHash: image.hash }]
}

// Cache to avoid reloading the same font repeatedly.
const _loaded = new Set<string>()

async function loadFontSafe(family: string | undefined, style: string): Promise<FontName> {
  const target: FontName = { family: family || "Inter", style }
  const key = `${target.family}//${target.style}`
  if (_loaded.has(key)) return target
  try {
    await figma.loadFontAsync(target)
    _loaded.add(key)
    return target
  } catch {
    const fallback: FontName = { family: "Inter", style: "Regular" }
    await figma.loadFontAsync(fallback)
    _loaded.add("Inter//Regular")
    return fallback
  }
}
