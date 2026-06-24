"""
UI style catalog for software creation.
"""
from dataclasses import asdict, dataclass

STYLE_SOURCE_URL = "https://www.uiprompt.site/zh/styles"


@dataclass(frozen=True)
class UIStyle:
    """Reusable UI style prompt entry."""

    id: str
    name: str
    description: str
    prompt: str

    def to_dict(self) -> dict:
        """Convert style to a serializable dictionary."""
        data = asdict(self)
        data["source_url"] = STYLE_SOURCE_URL
        return data


UI_STYLES = [
    UIStyle(
        id="minimal-saas",
        name="Minimal SaaS",
        description="克制、清晰、适合高频操作的 SaaS 工作台。",
        prompt=(
            "Use a minimal SaaS product style: clean hierarchy, quiet surfaces, "
            "dense but readable panels, restrained accent color, precise spacing, "
            "accessible contrast, and production-ready dashboard ergonomics."
        ),
    ),
    UIStyle(
        id="bento-grid",
        name="Bento Grid",
        description="模块化信息卡片，适合把复杂产品能力拆成可扫读区域。",
        prompt=(
            "Use a bento grid interface style: modular sections, strong information "
            "grouping, varied panel scale, subtle borders, crisp cards, and a balanced "
            "mix of metrics, workflow states, and preview surfaces."
        ),
    ),
    UIStyle(
        id="glassmorphism",
        name="Glassmorphism",
        description="半透明玻璃质感，适合轻量、未来感或创作型应用。",
        prompt=(
            "Use a refined glassmorphism style: translucent surfaces, soft blur, "
            "layered depth, luminous highlights, restrained gradients, and readable "
            "foreground text with strong contrast safeguards."
        ),
    ),
    UIStyle(
        id="neo-brutalism",
        name="Neo Brutalism",
        description="高对比、粗边框、强视觉个性，适合年轻化工具和创意产品。",
        prompt=(
            "Use a neo-brutalist style: bold typography, high-contrast blocks, thick "
            "borders, direct visual rhythm, confident color accents, and intentionally "
            "simple interaction surfaces."
        ),
    ),
    UIStyle(
        id="editor-pro",
        name="Professional Editor",
        description="类似专业创作软件的布局，强调工作区、检查器和状态反馈。",
        prompt=(
            "Use a professional editor style: persistent workspace, left navigation, "
            "central canvas or document area, right inspector controls, compact toolbars, "
            "clear save states, and efficient expert workflows."
        ),
    ),
    UIStyle(
        id="mobile-native",
        name="Mobile-First Web",
        description="移动优先的响应式 Web 视感:拇指友好、底部操作与原生式列表,以响应式 Web 实现。",
        prompt=(
            "Use a mobile-first responsive web style: thumb-friendly controls, bottom "
            "action bars, native-like list patterns, clear empty states, high legibility, "
            "and compact screen-by-screen progression — delivered as a responsive web app "
            "(not a native binary)."
        ),
    ),
    UIStyle(
        id="ai-console",
        name="AI Console",
        description="适合 AI 工作流、提示词调试和生成任务监控。",
        prompt=(
            "Use an AI console style: prompt-first layout, generated artifact panels, "
            "run history, model status indicators, token or credit awareness, and "
            "transparent step-by-step workflow feedback."
        ),
    ),
    UIStyle(
        id="startup-landing-app",
        name="Startup Product",
        description="产品展示感更强，适合需要快速验证商业想法的应用。",
        prompt=(
            "Use a startup product app style: polished first-screen product signal, "
            "confident visual hierarchy, conversion-aware actions, clean feature areas, "
            "and app-like functionality visible immediately."
        ),
    ),
    UIStyle(
        id="brutalism",
        name="Brutalism",
        description="原始粗野的网页美学:裸露结构、强硬无修饰,适合个性化作品集与文化站点。",
        prompt=(
            "Use a raw web brutalism style: bare unstyled structure, system fonts, "
            "harsh borders, exposed grids, monospace accents, and stark "
            "black-on-white surfaces — deliberately undecorated yet with a clear "
            "navigation and reading order."
        ),
    ),
    UIStyle(
        id="flat-design",
        name="Flat Design",
        description="去拟物的扁平体系:纯色块与清晰图标,通用且高效,适合 Web 与工具类产品。",
        prompt=(
            "Use a flat design system: no skeuomorphic shadows or gradients, solid "
            "color blocks, simple geometric icons, clear typographic hierarchy, "
            "generous whitespace, and crisp two-dimensional surfaces."
        ),
    ),
    UIStyle(
        id="fluent-2",
        name="Fluent 2",
        description="微软 Fluent 2 体系:亚克力材质、圆角与层次光影,适合跨端生产力应用。",
        prompt=(
            "Use Microsoft's Fluent 2 design system: acrylic translucency, soft "
            "depth and layering, rounded geometry, reveal-style highlights, "
            "coherent motion, and accessible system typography for cross-platform "
            "productivity apps."
        ),
    ),
    UIStyle(
        id="material-design",
        name="Material Design",
        description="Google Material 体系:纸张隐喻、立面阴影与鲜明强调色,适合 Android 与 Web 应用。",
        prompt=(
            "Use Google's Material Design system: a paper-and-ink metaphor, "
            "elevation shadows, bold primary and secondary color roles, responsive "
            "grids, FAB and component patterns, and meaningful motion with strong "
            "contrast."
        ),
    ),
    UIStyle(
        id="memphis",
        name="Memphis",
        description="80 年代孟菲斯风:撞色几何、波点与锯齿线条,活泼大胆,适合创意与年轻品牌。",
        prompt=(
            "Use an 80s Memphis design style: clashing bright colors, playful "
            "geometric shapes, squiggles, polka dots and zigzags, asymmetric "
            "compositions, and bold confident energy kept legible for content."
        ),
    ),
    UIStyle(
        id="minimalism",
        name="Minimalism",
        description="极致留白与克制用色:少即是多,适合精品、阅读与展示型站点。",
        prompt=(
            "Use a minimalism style: maximal whitespace, a very restrained palette, "
            "refined typography, only essential elements, strong alignment, and "
            "quiet hierarchy where negative space carries the composition."
        ),
    ),
    UIStyle(
        id="scroll-narrative",
        name="Scroll Narrative",
        description="滚动叙事:视差与渐进揭示驱动的沉浸式长页面,适合故事化品牌与产品发布页。",
        prompt=(
            "Use a scroll-narrative style: scroll-driven storytelling, parallax "
            "layers, progressive reveals, pinned sections, synchronized animations, "
            "and a clear chapter-like reading flow that stays performant and "
            "accessible."
        ),
    ),
    UIStyle(
        id="skeuomorphism",
        name="Skeuomorphism",
        description="拟物质感:仿真材质、高光与投影还原现实物件,适合怀旧或拟真工具。",
        prompt=(
            "Use a skeuomorphic style: realistic material textures, tactile "
            "highlights and inner shadows, beveled edges, depth that imitates "
            "physical objects, and convincing real-world metaphors while preserving "
            "usability."
        ),
    ),
    UIStyle(
        id="typography",
        name="Typography",
        description="以排版为主角:超大字号、强字重对比与精密栅格,适合编辑型与宣言式页面。",
        prompt=(
            "Use a typography-led style: oversized expressive headlines, strong "
            "weight and scale contrast, precise baseline grids, intentional kerning "
            "and leading, a limited palette so type carries the design, and "
            "impeccable readability."
        ),
    ),
    UIStyle(
        id="mouse-tracking",
        name="Mouse Tracking",
        description="光标追踪交互:元素随鼠标响应、跟随发光,适合互动展示与创意落地页。",
        prompt=(
            "Use a mouse-tracking interaction style: cursor-following elements, "
            "dynamic glow and spotlight that respond to pointer movement, parallax "
            "hover, and real-time visual feedback, with graceful fallbacks for "
            "touch and reduced-motion."
        ),
    ),
    UIStyle(
        id="magazine",
        name="Magazine Layout",
        description="杂志编排:多栏栅格、主图与引文混排,强编辑节奏,适合内容与媒体站。",
        prompt=(
            "Use a magazine layout style: multi-column editorial grids, dramatic "
            "hero imagery, pull quotes, drop caps, varied article rhythm, and a "
            "confident print-inspired hierarchy that stays responsive."
        ),
    ),
    UIStyle(
        id="arcade-crt",
        name="Arcade CRT",
        description="街机 CRT 扫描线:像素发光、屏幕弯曲与噪点,适合游戏与娱乐产品。",
        prompt=(
            "Use an arcade CRT style: scanline overlays, phosphor glow, slight "
            "screen curvature and vignette, pixel fonts, neon highlights, and retro "
            "game-cabinet energy while keeping core text crisp."
        ),
    ),
    UIStyle(
        id="art-deco",
        name="Art Deco",
        description="装饰艺术:对称几何、金色线条与奢华质感,适合高端品牌与活动页。",
        prompt=(
            "Use an Art Deco style: symmetrical geometric ornament, sunburst and "
            "chevron motifs, a gold-on-dark luxury palette, elegant high-contrast "
            "serif type, and refined linework with opulent detailing."
        ),
    ),
    UIStyle(
        id="bauhaus",
        name="Bauhaus",
        description="包豪斯:基础几何形与三原色、功能至上的构成主义,适合理性现代品牌。",
        prompt=(
            "Use a Bauhaus style: primary red, blue and yellow with black, pure "
            "geometric forms (circle, square, triangle), strict grid composition, "
            "functional clarity, and bold constructivist balance."
        ),
    ),
    UIStyle(
        id="dark-academia",
        name="Dark Academia",
        description="暗黑学院风:墨棕与典籍质感、古典衬线,适合阅读、教育与文化产品。",
        prompt=(
            "Use a dark academia style: a moody brown and ink palette, classical "
            "serif typography, parchment and library textures, vintage engravings, "
            "and a scholarly contemplative atmosphere with readable body text."
        ),
    ),
    UIStyle(
        id="digital-retro",
        name="Digital Retro",
        description="数位复古:80–90 年代电脑图形混搭现代极简,适合游戏与怀旧科技产品。",
        prompt=(
            "Use a digital retro style: 80s-90s computer graphics, pixel-art "
            "accents, CRT scan effects and early-internet visual language fused "
            "with contemporary minimalism, for a vintage-modern feel that stays "
            "usable."
        ),
    ),
    UIStyle(
        id="film-noir",
        name="Film Noir",
        description="黑色电影:高反差黑白、硬阴影与戏剧光影,适合叙事与影视类站点。",
        prompt=(
            "Use a film noir style: high-contrast black-and-white, dramatic "
            "chiaroscuro shadows, venetian-blind light, a grainy cinematic mood, "
            "and bold condensed type, with one restrained accent for emphasis."
        ),
    ),
    UIStyle(
        id="frutiger-aero",
        name="Frutiger Aero",
        description="Frutiger Aero:千禧中期玻璃质感、天蓝渐变与自然意象,适合清新科技产品。",
        prompt=(
            "Use a Frutiger Aero style: mid-2000s glossy glass, sky-blue gradients, "
            "water droplets and bubbles, lush nature imagery, clean humanist sans "
            "type, and optimistic translucent surfaces with strong legibility."
        ),
    ),
    UIStyle(
        id="mid-century-modern",
        name="Mid-Century Modern",
        description="中世纪现代:暖色调、有机几何与复古家居感,适合生活方式与品牌站。",
        prompt=(
            "Use a mid-century modern style: a warm muted retro palette (mustard, "
            "teal, burnt orange), organic geometric shapes, clean functional "
            "layouts, atomic-era motifs, and balanced craftsmanship."
        ),
    ),
    UIStyle(
        id="newspaper",
        name="Newspaper",
        description="报纸排版:密集多栏、黑白印刷质感与刊头,适合资讯、博客与档案站。",
        prompt=(
            "Use a newspaper typography style: dense multi-column text, a bold "
            "masthead and headlines, serif body copy, hairline rules, "
            "black-and-white print texture, and a structured editorial hierarchy "
            "that stays readable on screen."
        ),
    ),
    UIStyle(
        id="retro-futurism",
        name="Retro Futurism",
        description="复古未来主义:80 年代对未来的想象、终端绿与霓虹网格,适合科幻与游戏。",
        prompt=(
            "Use a retro-futurism style: a 1980s vision of the future, chrome and "
            "neon, wireframe grids, terminal glow, sci-fi cinema atmosphere, and "
            "bold futuristic type kept legible against dark backdrops."
        ),
    ),
    UIStyle(
        id="retro-os",
        name="Retro OS",
        description="复古操作系统:90 年代窗口、像素图标与立体按钮,适合怀旧工具与作品集。",
        prompt=(
            "Use a retro OS style: 90s desktop chrome, beveled window frames and "
            "title bars, pixel icons, chunky 3D buttons, a system-gray palette, and "
            "playful nostalgia while keeping interactions clear."
        ),
    ),
    UIStyle(
        id="steampunk",
        name="Steampunk",
        description="蒸汽朋克:黄铜齿轮、皮革与维多利亚机械感,适合主题游戏与叙事产品。",
        prompt=(
            "Use a steampunk style: brass and copper tones, gears and rivets, "
            "leather and aged-paper textures, Victorian ornament, mechanical "
            "detailing, and ornate serif type over a usable modern layout."
        ),
    ),
    UIStyle(
        id="swiss-design",
        name="Swiss Design",
        description="瑞士国际主义:网格系统、Helvetica 与极致秩序,适合理性、专业的内容站。",
        prompt=(
            "Use a Swiss / International Typographic style: strict grid systems, a "
            "Helvetica-like neutral sans, flush-left ragged-right text, asymmetric "
            "balance, abundant whitespace, and objective ornament-free clarity."
        ),
    ),
    UIStyle(
        id="synthwave",
        name="Synthwave",
        description="合成波:紫粉霓虹、网格地平线与落日,适合音乐、游戏与夜店风产品。",
        prompt=(
            "Use a synthwave style: magenta-and-cyan neon, sunset gradients, "
            "perspective grid horizons, chrome 80s type, glow and scanlines, and a "
            "nocturnal retro-futuristic mood with readable foreground content."
        ),
    ),
    UIStyle(
        id="vhs-aesthetic",
        name="VHS Aesthetic",
        description="VHS 录像带美学:色散、扫描噪点与时间码,适合复古媒体与潮流内容。",
        prompt=(
            "Use a VHS aesthetic: chromatic aberration, tracking noise and "
            "scanlines, timestamp overlays, washed analog colors, and slight warp "
            "applied as an overlay while keeping the primary UI sharp."
        ),
    ),
    UIStyle(
        id="3d-elements",
        name="3D Elements",
        description="3D 元素:立体造型、真实光照与景深,适合产品展示与沉浸式落地页。",
        prompt=(
            "Use a 3D elements style: realistic depth and perspective, soft studio "
            "lighting and shadows, floating volumetric objects, tactile materials, "
            "and subtle parallax, balanced so content stays readable and fast."
        ),
    ),
    UIStyle(
        id="accessibility",
        name="Accessibility First",
        description="无障碍优先:高对比、大触控目标与清晰焦点态,适合政务、医疗与通用产品。",
        prompt=(
            "Use an accessibility-first style: WCAG AAA contrast, large legible "
            "type, generous touch targets, visible focus rings, clear labels and "
            "error states, no color-only signaling, and motion-safe interactions."
        ),
    ),
    UIStyle(
        id="ambient",
        name="Ambient Light",
        description="环境光:弥散柔光、径向模糊与梦幻氛围,适合冥想、健康与音频类应用。",
        prompt=(
            "Use an ambient light style: diffuse radial glows, soft blurred halos, "
            "gentle gradients, rounded calm surfaces, and a serene dreamy "
            "atmosphere, keeping text on solid-enough surfaces to stay readable."
        ),
    ),
    UIStyle(
        id="anti-design",
        name="Anti-Design",
        description="反设计:刻意打破规则、错位排版与混乱构图,适合艺术、实验与先锋项目。",
        prompt=(
            "Use an anti-design style: deliberate rule-breaking, clashing fonts, "
            "off-grid chaotic layouts, raw unrefined elements, and provocative "
            "artistic expression that is intentional yet still navigable."
        ),
    ),
    UIStyle(
        id="aurora-glass",
        name="Aurora Glass",
        description="极光玻璃:极光渐变叠加毛玻璃,绚丽通透,适合高端科技与创意产品。",
        prompt=(
            "Use an aurora glass style: vivid aurora gradient backgrounds behind "
            "frosted-glass panels, luminous color bleeds, soft blur and depth, and "
            "bright accents, with strong text-contrast safeguards over the glass."
        ),
    ),
    UIStyle(
        id="biophilic",
        name="Biophilic",
        description="亲生物设计:自然光、植物意象与有机材质,适合健康、环保与生活方式产品。",
        prompt=(
            "Use a biophilic design style: natural light, botanical imagery, "
            "organic materials and textures, earthy greens and wood tones, flowing "
            "layouts, and a calm nature-connected feel that supports wellbeing."
        ),
    ),
    UIStyle(
        id="blueprint",
        name="Blueprint",
        description="蓝图风格:深蓝底白色线稿、网格与标注,适合工程、建筑与技术类产品。",
        prompt=(
            "Use a blueprint style: a deep blueprint-blue background, white "
            "technical linework, measurement grids and annotations, monospace "
            "labels, and precise schematic diagrams with engineering clarity."
        ),
    ),
    UIStyle(
        id="claymorphism",
        name="Claymorphism",
        description="黏土质感:圆润膨胀体块、柔和双阴影与糖果色,适合趣味与年轻化产品。",
        prompt=(
            "Use a claymorphism style: puffy rounded 3D shapes, soft "
            "inner-and-outer shadows, pastel candy colors, thick friendly geometry, "
            "and a playful tactile feel with clear readable foreground content."
        ),
    ),
    UIStyle(
        id="comic-book",
        name="Comic Book",
        description="漫画书风:粗描边、网点底纹与对话气泡,适合娱乐、活动与儿童向产品。",
        prompt=(
            "Use a comic book style: bold ink outlines, halftone dot shading, "
            "dynamic panels, speech bubbles and onomatopoeia, saturated primary "
            "colors, and energetic action-packed layouts kept legible."
        ),
    ),
    UIStyle(
        id="cube-3d",
        name="3D Cube",
        description="3D 立方体:等距盒体、堆叠几何与立体网格,适合数据可视化与科技展示。",
        prompt=(
            "Use a 3D cube style: isometric boxes, stacked cubic geometry, extruded "
            "surfaces, dimensional grids, and crisp axonometric perspective, with "
            "clear labeling so the structure reads as information."
        ),
    ),
    UIStyle(
        id="dark-mode",
        name="Dark Mode",
        description="深色模式:低光背景、克制发光与高可读对比,适合长时间使用的工具与仪表盘。",
        prompt=(
            "Use a dark mode style: near-black elevated surfaces, desaturated "
            "accents with controlled glow, careful contrast for legibility, dimmed "
            "but readable text, and reduced eye strain for prolonged use."
        ),
    ),
    UIStyle(
        id="duotone",
        name="Duotone",
        description="双色调:两种对比色映射全图,强烈统一,适合海报式品牌与活动页。",
        prompt=(
            "Use a duotone style: a two-color gradient mapped across imagery and "
            "UI, strong unified contrast, bold poster-like compositions, and a "
            "single consistent palette with enough tonal range to keep text "
            "readable."
        ),
    ),
    UIStyle(
        id="fabric",
        name="Fabric Texture",
        description="织物纹理:布料编织、缝线与柔软触感,温暖拟物,适合时尚、家居与手作品牌。",
        prompt=(
            "Use a fabric-texture style: woven cloth and knit patterns, stitched "
            "seams, soft folds and shadows, warm tactile materials, and cozy "
            "textile craftsmanship layered without harming text legibility."
        ),
    ),
    UIStyle(
        id="generative-art",
        name="Generative Art",
        description="生成艺术:算法图形、粒子与动态纹样,适合艺术、音乐与创意科技产品。",
        prompt=(
            "Use a generative art style: algorithmic patterns, flow fields and "
            "particles, parametric color and motion, organic complexity, and "
            "ever-evolving backdrops layered behind clear legible content."
        ),
    ),
    UIStyle(
        id="glow",
        name="Glow Effect",
        description="发光效果:投影辉光与呼吸脉动,点亮关键元素,适合夜间与娱乐界面。",
        prompt=(
            "Use a glow-effect style: luminous box-shadow halos, breathing pulse "
            "animations, neon edges on key elements, dark backdrops for contrast, "
            "and tasteful restraint so glow guides attention rather than "
            "overwhelming."
        ),
    ),
    UIStyle(
        id="gradients",
        name="Gradients",
        description="渐变美学:多彩柔和过渡与网格渐变,现代鲜活,适合品牌与营销页。",
        prompt=(
            "Use a gradient-rich style: smooth multi-stop and mesh gradients, "
            "vibrant yet harmonious color transitions, soft glows, and modern "
            "energy, ensuring text sits on areas with sufficient contrast."
        ),
    ),
    UIStyle(
        id="grain",
        name="Grain Noise",
        description="颗粒噪点:胶片质感叠加,柔化数字感,适合品牌、音乐与编辑型页面。",
        prompt=(
            "Use a grain-noise style: subtle film grain and noise texture over "
            "surfaces and gradients, an analog tactile finish that reduces digital "
            "flatness, applied lightly so it never harms legibility."
        ),
    ),
    UIStyle(
        id="hand-drawn-sketch",
        name="Hand-Drawn Sketch",
        description="手绘涂鸦:草图线条、不规则边与手写体,亲和有温度,适合教育与创意产品。",
        prompt=(
            "Use a hand-drawn sketch style: rough pencil and ink strokes, wobbly "
            "imperfect borders, handwritten-style fonts, doodle accents and arrows, "
            "paper texture, and a warm approachable personality with clear "
            "structure."
        ),
    ),
    UIStyle(
        id="holographic",
        name="Holographic Gradient",
        description="全息渐变:彩虹光谱随视角流转,未来感强,适合科技、潮流与音乐产品。",
        prompt=(
            "Use a holographic gradient style: an iridescent rainbow spectrum, "
            "dynamic hue shifts, prismatic sheen and chrome accents, futuristic "
            "surfaces, and high-energy color, kept legible with solid text "
            "containers."
        ),
    ),
    UIStyle(
        id="holographic-foil",
        name="Holographic Foil",
        description="全息箔:虹彩金属箔反光质感,奢华吸睛,适合潮牌、收藏与活动页。",
        prompt=(
            "Use a holographic foil style: iridescent metallic foil texture, "
            "rainbow light reflections, premium shimmer on cards and type, and "
            "collectible packaging vibes, balanced with a clean readable layout."
        ),
    ),
    UIStyle(
        id="industrial",
        name="Industrial",
        description="工业设计:深色硬朗、粗体大写与功能优先,适合后台、工具与数据平台。",
        prompt=(
            "Use an industrial design style: dark utilitarian surfaces, bold "
            "uppercase type, strong functional grids, metal and warning-tape "
            "accents, dense data ergonomics, and a rugged tool-like aesthetic for "
            "heavy-use apps."
        ),
    ),
    UIStyle(
        id="ink-wash",
        name="Ink Wash",
        description="水墨风:东方笔触、浓淡晕染与留白,雅致写意,适合文化、艺术与茶饮品牌。",
        prompt=(
            "Use an ink-wash (shuimo) style: East-Asian brush strokes, gradient ink "
            "bleeds, generous negative space, a restrained black-and-gray palette "
            "with one seal-red accent, and an elegant calligraphic mood with "
            "readable text."
        ),
    ),
    UIStyle(
        id="kawaii-minimal",
        name="Kawaii Minimal",
        description="可爱极简:柔和粉彩、圆角与萌系点缀,干净又治愈,适合生活方式与社交产品。",
        prompt=(
            "Use a kawaii minimal style: a soft pastel palette, rounded friendly "
            "shapes, cute minimal mascots and icons, plenty of whitespace, gentle "
            "typography, and a clean wholesome feel that stays uncluttered."
        ),
    ),
    UIStyle(
        id="leather",
        name="Leather Texture",
        description="皮革质感:缝线、压纹与暖棕高光,精致复古,适合奢品与拟物界面。",
        prompt=(
            "Use a leather-texture style: stitched seams, embossed grain, warm "
            "brown tones, subtle highlights and shadows, premium tactile "
            "craftsmanship, and luxury skeuomorphic detailing without sacrificing "
            "clarity."
        ),
    ),
    UIStyle(
        id="light",
        name="Light Effects",
        description="光效设计:多层光晕、散景与戏剧照明,营造氛围,适合展示与发布页。",
        prompt=(
            "Use a dramatic light-effects style: layered glows, bokeh, halos and "
            "lens flares, theatrical illumination against dark scenes, and luminous "
            "focal points, with content placed where contrast stays high."
        ),
    ),
    UIStyle(
        id="liminal-space",
        name="Liminal Space",
        description="边界空间:空旷过渡场景、诡谧静谧氛围,适合艺术、叙事与实验性项目。",
        prompt=(
            "Use a liminal space style: empty transitional environments, an "
            "eerie-yet-beautiful emptiness, muted nostalgic lighting, vast negative "
            "space, and a quiet uncanny mood, with subtle cues guiding navigation."
        ),
    ),
    UIStyle(
        id="liquid",
        name="Liquid Motion",
        description="液态流动:形变动画、径向渐变与流体融合,灵动有机,适合创意与品牌页。",
        prompt=(
            "Use a liquid-motion style: morphing blob shapes, fluid metaball "
            "merges, radial gradients, smooth flowing transitions, and organic "
            "biomorphic movement, kept performant and non-distracting around text."
        ),
    ),
    UIStyle(
        id="monochrome",
        name="Monochrome",
        description="单色调:单一色相深浅构成全局,克制统一,适合极简、摄影与作品集。",
        prompt=(
            "Use a monochrome style: a single hue across tints and shades (or pure "
            "grayscale), tonal hierarchy instead of color, strong typographic "
            "structure, and one optional accent reserved for critical actions."
        ),
    ),
    UIStyle(
        id="natural",
        name="Natural Earthy",
        description="自然质朴:大地色、植物元素与天然纹理,温润和谐,适合环保与手作品牌。",
        prompt=(
            "Use a natural earthy style: an earth-tone palette, botanical motifs, "
            "organic textures such as wood, stone and linen, soft daylight, and "
            "harmonious sustainable aesthetics that feel grounded and warm."
        ),
    ),
    UIStyle(
        id="nature",
        name="Nature Elements",
        description="自然元素特效:极光、波浪、粒子与烟雾交织,沉浸灵动,适合氛围型落地页。",
        prompt=(
            "Use a nature-elements style: animated aurora, flowing waves, drifting "
            "particles, and liquid and smoke effects layered into immersive scenes, "
            "with content kept readable on calmer foreground surfaces."
        ),
    ),
    UIStyle(
        id="neon",
        name="Neon Glow",
        description="霓虹发光:暗底亮管发光描边,夜感强烈,适合娱乐、夜生活与游戏产品。",
        prompt=(
            "Use a neon-glow style: dark backdrops, glowing neon-tube outlines and "
            "text, saturated electric colors, soft bloom, and nightlife energy, "
            "with key content kept high-contrast against the dark."
        ),
    ),
    UIStyle(
        id="neon-cyberpunk",
        name="Neon Cyberpunk",
        description="霓虹赛博朋克:雨夜都市、青粉霓虹与故障感,适合游戏、科技与潮流产品。",
        prompt=(
            "Use a neon cyberpunk style: a rain-slicked dark city mood, "
            "cyan-and-magenta neon, glitch and HUD overlays, dense techno "
            "detailing, and dystopian futurism, with legible panels over the chaos."
        ),
    ),
    UIStyle(
        id="neon-noir",
        name="Neon Noir",
        description="霓虹黑色电影:暗影都市配高饱和霓虹,冷峻戏剧,适合叙事与影视类站点。",
        prompt=(
            "Use a neon noir style: noir shadows and moody darkness pierced by "
            "saturated neon accents, cinematic rim lighting, rain and reflections, "
            "and dramatic atmosphere with one or two readable accent colors."
        ),
    ),
    UIStyle(
        id="organic",
        name="Modern Organic",
        description="现代有机:生物形态曲线、流动动画与暖大地色,亲和自然,适合健康与生活品牌。",
        prompt=(
            "Use a modern organic style: biomorphic curved shapes, flowing soft "
            "animations, a warm earthy palette, natural asymmetry, and approachable "
            "harmony, with comfortable spacing and a clear reading flow."
        ),
    ),
    UIStyle(
        id="outline-style",
        name="Outline Style",
        description="线条风格:以描边定义形体、线性图标与极简,适合现代极简与技术产品。",
        prompt=(
            "Use an outline style: shapes defined purely by strokes, consistent "
            "line weights, linear icons, minimal fills, airy whitespace, and "
            "refined geometric clarity for an elegant modern look."
        ),
    ),
    UIStyle(
        id="paper-cutout",
        name="Paper Cutout",
        description="剪纸风:分层纸片、投影与拼贴质感,手作温度,适合儿童、节庆与故事类产品。",
        prompt=(
            "Use a paper-cutout style: layered paper shapes with soft drop shadows, "
            "collage compositions, torn and folded edges, tactile craft texture, "
            "and bright friendly colors with clear depth ordering."
        ),
    ),
    UIStyle(
        id="particle",
        name="Particle System",
        description="粒子系统:动态粒子、连线与星云背景,科技灵动,适合数据、AI 与科技产品。",
        prompt=(
            "Use a particle-system style: animated particle fields, connecting "
            "constellations, subtle physics and drift, glowing nebula backdrops, "
            "and a high-tech atmosphere layered behind crisp readable UI."
        ),
    ),
    UIStyle(
        id="pop-art",
        name="Pop Art",
        description="波普艺术:撞色平涂、网点与漫画感,张扬复古,适合营销、活动与潮流品牌。",
        prompt=(
            "Use a pop art style: bold flat saturated colors, Ben-Day halftone "
            "dots, comic outlines, repetition and high contrast, and playful 60s "
            "pop energy kept structured and legible."
        ),
    ),
    UIStyle(
        id="scandi",
        name="Scandinavian",
        description="斯堪的纳维亚:浅木色、柔和中性与功能极简,温暖明亮,适合家居与生活产品。",
        prompt=(
            "Use a Scandinavian style: a light airy palette, soft neutrals with "
            "pale wood tones, functional minimalism, cozy hygge warmth, clean sans "
            "typography, and uncluttered comfortable layouts."
        ),
    ),
    UIStyle(
        id="sci-fi-hud",
        name="Sci-Fi HUD",
        description="科幻 HUD:全息界面、数据环与扫描动效,未来科技感,适合仪表盘与游戏。",
        prompt=(
            "Use a sci-fi HUD style: holographic interface panels, glowing data "
            "rings and reticles, scan-line animations, monospace telemetry, cyan "
            "and amber on dark, and futuristic command-center clarity for "
            "dashboards."
        ),
    ),
    UIStyle(
        id="smoke",
        name="Smoke Effect",
        description="烟雾效果:流动烟雾与雾气氛围,神秘柔和,适合氛围型展示与品牌页。",
        prompt=(
            "Use a smoke-effect style: drifting volumetric smoke and fog, soft "
            "swirling gradients, atmospheric depth, and a mysterious moody "
            "backdrop, with content surfaced on clearer panels for readability."
        ),
    ),
    UIStyle(
        id="soft-ui",
        name="Soft UI",
        description="Soft UI(新拟态):同色凹凸柔影,细腻低对比,适合精致仪表盘与控件密集界面。",
        prompt=(
            "Use a soft UI / neumorphism style: monochromatic surfaces with dual "
            "inner-and-outer soft shadows, subtle extruded and inset elements, and "
            "low-contrast tactile minimalism — with extra care to keep text and "
            "states accessible."
        ),
    ),
    UIStyle(
        id="solarpunk",
        name="Solarpunk",
        description="太阳庞克:生态未来、绿能与社区共生,明亮乐观,适合环保与公益类产品。",
        prompt=(
            "Use a solarpunk style: eco-futuristic optimism, lush greenery fused "
            "with clean technology, solar and golden light, natural materials, a "
            "hopeful bright palette, and community-minded warmth with clear "
            "usability."
        ),
    ),
    UIStyle(
        id="spotlight",
        name="Spotlight",
        description="聚光灯:移动径向光聚焦关键内容,戏剧舞台感,适合展示与发布页。",
        prompt=(
            "Use a spotlight style: a moving radial light that focuses attention, "
            "dramatic surrounding shadow, stage-like reveals, and interactive "
            "illumination following key elements, keeping focused content "
            "high-contrast."
        ),
    ),
    UIStyle(
        id="utility-first",
        name="Design Tokens",
        description="系统化设计令牌:一致的间距/字号/圆角刻度与克制定制,高可维护,适合快速迭代的产品(以纯 CSS 变量实现,不依赖 Tailwind)。",
        prompt=(
            "Use a systematic design-token style: consistent spacing, type and radius "
            "scales, a small set of reusable design tokens, restrained custom styling, "
            "and pragmatic, highly maintainable component patterns — expressed in plain "
            "CSS custom properties, without any utility-class framework."
        ),
    ),
    UIStyle(
        id="vaporwave",
        name="Vaporwave",
        description="蒸汽波:粉紫渐变、罗马雕像与故障网格,复古超现实,适合潮流与音乐产品。",
        prompt=(
            "Use a vaporwave style: pink-and-cyan gradients, 80s and 90s nostalgia, "
            "classical statues, glitch and grid motifs, Japanese-text accents, and "
            "a surreal retro mood with foreground content kept readable."
        ),
    ),
    UIStyle(
        id="wabi-sabi",
        name="Wabi-Sabi",
        description="侘寂:接纳残缺与不对称、天然质朴与留白,沉静雅致,适合茶饮、文化与精品。",
        prompt=(
            "Use a wabi-sabi style: beauty in imperfection, asymmetry and natural "
            "irregularity, weathered organic textures, muted earthy neutrals, "
            "generous emptiness, and quiet understated elegance."
        ),
    ),
    UIStyle(
        id="y2k",
        name="Y2K",
        description="Y2K 千禧风:金属质感、气泡感与亮丽撞色,未来复古,适合潮流与娱乐产品。",
        prompt=(
            "Use a Y2K style: early-2000s metallic chrome, glossy bubbly shapes, "
            "frosted translucency, bright saturated clashing colors, star and "
            "sparkle motifs, and techno-optimist nostalgia kept legible."
        ),
    ),
]


def list_styles() -> list[dict]:
    """Return all available UI styles."""
    return [style.to_dict() for style in UI_STYLES]


def get_styles(style_ids: list[str]) -> list[UIStyle]:
    """Return matching styles while preserving the requested order."""
    styles_by_id = {style.id: style for style in UI_STYLES}
    return [styles_by_id[style_id] for style_id in style_ids if style_id in styles_by_id]
