"""
RedBook Outline Service

大纲生成服务
"""
import re
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

from backend.services.ai import get_text_provider

logger = logging.getLogger(__name__)


class OutlineService:
    """大纲生成服务"""

    def __init__(self):
        logger.debug("初始化 OutlineService...")
        self.prompt_template = self._load_prompt_template()
        logger.info("OutlineService 初始化完成")

    def _load_prompt_template(self) -> str:
        """加载提示词模板"""
        prompt_path = Path(__file__).parent.parent.parent / "prompts" / "redbook" / "outline_prompt.txt"
        if prompt_path.exists():
            with open(prompt_path, "r", encoding="utf-8") as f:
                return f.read()
        logger.warning(f"提示词模板不存在: {prompt_path}")
        return ""

    def _parse_outline(self, outline_text: str) -> List[Dict[str, Any]]:
        """
        解析大纲文本为页面列表

        Args:
            outline_text: 原始大纲文本

        Returns:
            解析后的页面列表
        """
        # 按 <page> 分割页面
        if "<page>" in outline_text:
            pages_raw = re.split(r"<page>", outline_text, flags=re.IGNORECASE)
        else:
            # 向后兼容：如果没有 <page> 则使用 ---
            pages_raw = outline_text.split("---")

        pages = []

        for index, page_text in enumerate(pages_raw):
            page_text = page_text.strip()
            if not page_text:
                continue

            # 解析页面类型
            page_type = "内容"
            type_match = re.match(r"\[(\S+)\]", page_text)
            if type_match:
                type_cn = type_match.group(1)
                if type_cn in ("封面", "内容", "总结"):
                    page_type = type_cn

            pages.append({
                "index": len(pages),  # 使用实际索引
                "type": page_type,
                "content": page_text
            })

        return pages

    def generate_outline(
        self,
        topic: str,
        images: Optional[List[bytes]] = None
    ) -> Dict[str, Any]:
        """
        生成大纲

        Args:
            topic: 主题文本
            images: 参考图片列表（可选）

        Returns:
            {
                "success": bool,
                "outline": str,  # 原始大纲文本
                "pages": list,   # 解析后的页面列表
                "error": str     # 错误信息（失败时）
            }
        """
        try:
            logger.info(f"开始生成大纲: topic={topic[:50]}...")

            # 构建提示词
            prompt = self.prompt_template.format(topic=topic)

            if images and len(images) > 0:
                prompt += f"\n\n注意：用户提供了 {len(images)} 张参考图片，请在生成大纲时考虑这些图片的内容和风格。"

            # 使用统一的 AI 服务
            provider = get_text_provider()
            if not provider or not provider.is_configured():
                return {
                    "success": False,
                    "error": "AI 服务未配置。请在系统设置中配置 AI 服务商。"
                }

            # 调用 AI 生成
            result = provider.generate_text(prompt, images=images)

            if not result.success:
                return {
                    "success": False,
                    "error": result.error or "大纲生成失败"
                }

            outline_text = result.text

            # 解析大纲
            pages = self._parse_outline(outline_text)
            logger.info(f"大纲生成成功，共 {len(pages)} 页")

            return {
                "success": True,
                "outline": outline_text,
                "pages": pages,
                "has_images": images is not None and len(images) > 0
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"大纲生成失败: {error_msg}")

            # 详细错误信息
            if "api_key" in error_msg.lower() or "unauthorized" in error_msg.lower():
                detailed_error = f"API 认证失败。请检查 API Key 配置。\n错误: {error_msg}"
            elif "model" in error_msg.lower() or "404" in error_msg:
                detailed_error = f"模型访问失败。请检查模型配置。\n错误: {error_msg}"
            else:
                detailed_error = f"大纲生成失败。\n错误: {error_msg}"

            return {
                "success": False,
                "error": detailed_error
            }


# 单例实例
_service_instance = None


def get_outline_service() -> OutlineService:
    """获取大纲服务实例"""
    global _service_instance
    if _service_instance is None:
        _service_instance = OutlineService()
    return _service_instance
