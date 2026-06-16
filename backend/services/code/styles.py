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
        name="Mobile Native",
        description="移动端原生产品感，适合先移动后桌面的应用。",
        prompt=(
            "Use a mobile-native app style: thumb-friendly controls, bottom actions, "
            "native list patterns, clear empty states, high legibility, and compact "
            "screen-by-screen progression."
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
]


def list_styles() -> list[dict]:
    """Return all available UI styles."""
    return [style.to_dict() for style in UI_STYLES]


def get_styles(style_ids: list[str]) -> list[UIStyle]:
    """Return matching styles while preserving the requested order."""
    styles_by_id = {style.id: style for style in UI_STYLES}
    return [styles_by_id[style_id] for style_id in style_ids if style_id in styles_by_id]
