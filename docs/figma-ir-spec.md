# Figma Design IR spec

The **Design IR** is the canonical intermediate representation that bridges the
platform and Figma in both directions. It is a small, bounded subset of Figma's
node schema. Authoritative definition: `backend/services/code/figma/ir.py`. The
Figma plugin consumes the *plugin payload* derived from it
(`plugin/figma/src/ir.ts`).

```
Figma REST node tree  --figma_node_to_ir-->  DesignIR  --ir_to_plugin_payload-->  plugin payload
                          (import)                            (export)
```

## Conventions

- **Colors**: RGBA in `0..1` (matches the Figma REST + Plugin APIs). Never 0–255.
- **Coordinates**: the IR stores absolute `absoluteBoundingBox`; the plugin
  payload converts to **parent-relative** `x/y` (what `node.x/y` means in Figma).
- **Compactness**: `to_dict()` drops `null` / empty fields. `to_prompt_text()`
  caps length for prompt injection.

## DesignIR

| field | type | notes |
|-------|------|-------|
| `ir_version` | string | currently `"1.0"` |
| `source` | string | `figma` \| `html` \| `preview_image` |
| `name` | string | design / file name |
| `root` | IRNode | usually the selected `FRAME` |
| `tokens` | DesignTokens | de-duplicated palette / fonts / spacings |
| `images` | map | `image_ref` → data URL (inline for export) |

## IRNode

`type ∈ { DOCUMENT, FRAME, GROUP, RECTANGLE, TEXT, IMAGE }`. Figma types are
mapped to the nearest of these (containers → `FRAME`, vectors/shapes →
`RECTANGLE`).

| field | type | applies to |
|-------|------|-----------|
| `id`, `name`, `type` | string | all |
| `box` | `{x,y,width,height}` | all (absolute) |
| `fills`, `strokes` | `Paint[]` | all |
| `stroke_weight` | number | all |
| `corner_radius` | number | rect/frame |
| `opacity` | number `0..1` | all |
| `characters` | string | `TEXT` |
| `text_style` | TextStyle | `TEXT` |
| `layout_mode`, `item_spacing`, `padding` | — | auto-layout frames |
| `children` | IRNode[] | containers |

`Paint`: `{ type: "SOLID"|"IMAGE"|…, color?: {r,g,b,a}, opacity, image_ref? }`.
`TextStyle`: `{ font_family, font_size, font_weight, line_height_px,
letter_spacing, text_align_horizontal, fills }`.

## Plugin payload (`ir_to_plugin_payload`)

Flattened for the plugin: parent-relative `x/y`, solid fills as
`{type:"SOLID", color:{r,g,b}, opacity}`, image fills as
`{type:"IMAGE", imageRef}` (bytes resolved from `images`), text carries
`fontSize/fontFamily/fontWeight/textAlignHorizontal`, and `children` recurse.

## Directions

- **Import / restore** (`code_figma_restore` workflow): Figma REST node tree →
  `figma_node_to_ir` → IR (text) + a rendered PNG → AI rebuilds HTML.
- **Export — preview image**: a rendered PNG → `image_design_ir` (a single
  image-filled frame), deterministic, 100% fidelity.
- **Export — HTML**: generated HTML → AI emits a plugin payload directly
  (no headless browser exists to measure the DOM).
