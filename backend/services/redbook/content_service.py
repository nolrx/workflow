"""
RedBook Content Service

内容生成服务：生成小红书风格的标题、文案和标签
"""
import json
import re
import logging
from pathlib import Path
from typing import Dict, Any

from backend.services.ai import get_text_provider

logger = logging.getLogger(__name__)


class ContentService:
    """内容生成服务"""

    def __init__(self):
        logger.debug("初始化 ContentService...")
        self.prompt_template = self._load_prompt_template()
        logger.info("ContentService 初始化完成")

    def _load_prompt_template(self) -> str:
        """加载提示词模板"""
        prompt_path = Path(__file__).parent.parent.parent / "prompts" / "redbook" / "content_prompt.txt"
        if prompt_path.exists():
            with open(prompt_path, "r", encoding="utf-8") as f:
                return f.read()
        logger.warning(f"提示词模板不存在: {prompt_path}")
        return ""

    def _parse_json_response(self, response_text: str) -> Dict[str, Any]:
        """解析 AI 返回的 JSON 响应"""
        # 尝试直接解析
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            pass

        # 尝试从 markdown 代码块中提取
        json_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', response_text)
        if json_match:
            try:
                return json.loads(json_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 尝试找到 JSON 对象的开始和结束
        start_idx = response_text.find('{')
        end_idx = response_text.rfind('}')
        if start_idx != -1 and end_idx != -1:
            try:
                return json.loads(response_text[start_idx:end_idx + 1])
            except json.JSONDecodeError:
                pass

        logger.error(f"无法解析 JSON 响应: {response_text[:200]}...")
        raise ValueError("AI 返回的内容格式不正确，无法解析")

    def generate_content(
        self,
        topic: str,
        outline: str
    ) -> Dict[str, Any]:
        """
        生成标题、文案和标签

        Args:
            topic: 用户输入的主题
            outline: 大纲内容

        Returns:
            包含 titles, copywriting, tags 的字典
        """
        try:
            logger.info(f"开始生成内容: topic={topic[:50]}...")

            # 使用统一的 AI 服务
            provider = get_text_provider()
            if not provider or not provider.is_configured():
                return {
                    "success": False,
                    "error": "AI 服务未配置。请在系统设置中配置 AI 服务商。"
                }

            # 构建提示词
            prompt = self.prompt_template.format(
                topic=topic,
                outline=outline
            )

            # 调用 AI 生成
            result = provider.generate_text(prompt)

            if not result.success:
                return {
                    "success": False,
                    "error": result.error or "内容生成失败"
                }

            response_text = result.text

            logger.debug(f"API 返回文本长度: {len(response_text)} 字符")

            # 解析 JSON 响应
            content_data = self._parse_json_response(response_text)

            # 提取和验证字段
            titles = content_data.get('titles', [])
            copywriting = content_data.get('copywriting', '')
            tags = content_data.get('tags', [])

            # 确保 titles 是列表
            if isinstance(titles, str):
                titles = [titles]

            # 确保 tags 是列表
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(',')]

            logger.info(f"内容生成完成: {len(titles)} 个标题, {len(tags)} 个标签")

            return {
                "success": True,
                "titles": titles,
                "copywriting": copywriting,
                "tags": tags
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"内容生成失败: {error_msg}")

            # 根据错误类型提供更详细的错误信息
            if "api_key" in error_msg.lower() or "unauthorized" in error_msg.lower():
                detailed_error = f"API 认证失败。请检查 API Key 配置。\n错误: {error_msg}"
            elif "model" in error_msg.lower() or "404" in error_msg:
                detailed_error = f"模型访问失败。请检查模型配置。\n错误: {error_msg}"
            else:
                detailed_error = f"内容生成失败。\n错误: {error_msg}"

            return {
                "success": False,
                "error": detailed_error
            }


# 单例实例
_service_instance = None


def get_content_service() -> ContentService:
    """获取内容服务实例"""
    global _service_instance
    if _service_instance is None:
        _service_instance = ContentService()
    return _service_instance
