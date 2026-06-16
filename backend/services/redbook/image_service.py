"""
RedBook Image Service

图片生成服务（SSE 流式返回）
"""
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from backend.extensions import db
from backend.models.redbook import RedBookImage, RedBookImageStatus, RedBookTask, RedBookTaskStatus
from backend.services import pricing
from backend.services.ai import get_image_provider
from backend.services.credit_service import charge, check_sufficient_credits

logger = logging.getLogger(__name__)


class ImageService:
    """图片生成服务"""

    MAX_CONCURRENT = 5  # 最大并发数

    def __init__(self):
        logger.debug("初始化 ImageService...")
        self.prompt_template = self._load_prompt_template()
        self.prompt_template_short = self._load_prompt_template(short=True)
        logger.info("ImageService 初始化完成")

    def _load_prompt_template(self, short: bool = False) -> str:
        """加载提示词模板"""
        filename = "image_prompt_short.txt" if short else "image_prompt.txt"
        prompt_path = Path(__file__).parent.parent.parent / "prompts" / "redbook" / filename
        if prompt_path.exists():
            with open(prompt_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def generate_images(
        self,
        task_id: str,
        pages: List[Dict],
        full_outline: str = "",
        user_images: Optional[List[bytes]] = None,
        user_topic: str = "",
        payer_user_id: Optional[str] = None,
        team_id: Optional[str] = None
    ) -> Generator[Dict[str, Any], None, None]:
        """
        批量生成图片（SSE 流式返回）

        Args:
            task_id: 任务 ID
            pages: 页面列表
            full_outline: 完整大纲文本
            user_images: 用户参考图片
            user_topic: 用户主题
            payer_user_id: 付费用户（用于逐张扣费；为空则不计费）
            team_id: 若提供则从团队余额扣费

        Yields:
            SSE 事件：{ "event": str, "data": dict }
        """
        from backend.services.redbook import RedBookFileService
        file_service = RedBookFileService()

        # 生成图片任务 ID
        image_task_id = str(uuid.uuid4())

        # 更新任务的 image_task_id
        task = RedBookTask.query.get(task_id)
        if task:
            task.image_task_id = image_task_id
            task.status = RedBookTaskStatus.GENERATING
            db.session.commit()

        logger.info(f"开始图片生成: task={task_id}, image_task={image_task_id}, pages={len(pages)}")

        cover_image = None
        completed = 0
        failed = 0

        for page in pages:
            index = page.get("index", 0)
            page_type = page.get("type", "内容")
            page_content = page.get("content", "")

            # 积分预检:付费方余额不足以支付下一张时停止生成（避免产出未付费图片）
            if payer_user_id and not check_sufficient_credits(
                payer_user_id, pricing.REDBOOK_IMAGE_PAGE, team_id
            ):
                logger.warning(f"图片生成因积分不足在第 {index} 张前停止: task={task_id}")
                yield {
                    "event": "error",
                    "data": {"index": index, "error": "积分不足,已停止生成"}
                }
                break

            try:
                # 构建提示词
                prompt = self.prompt_template.format(
                    page_content=page_content,
                    page_type=page_type,
                    user_topic=user_topic,
                    full_outline=full_outline
                )

                # 生成图片
                image_data = self._generate_single_image(
                    prompt,
                    reference_image=cover_image if page_type != "封面" else None,
                    user_images=user_images
                )

                if image_data:
                    # 保存图片
                    filename = f"{index}.png"
                    image_path, thumbnail_path = file_service.save_image(
                        image_task_id, filename, image_data
                    )

                    # 保存封面图作为参考
                    if page_type == "封面" and cover_image is None:
                        cover_image = image_data

                    # 更新第一张图为缩略图
                    if index == 0 and task:
                        task.thumbnail_path = filename
                        db.session.commit()

                    # 创建图片记录
                    image_record = RedBookImage(
                        task_id=task_id,
                        order_index=index,
                        filename=filename,
                        thumbnail_path=f"thumb_{filename}" if thumbnail_path else None,
                        status=RedBookImageStatus.COMPLETED,
                        generated_at=datetime.utcnow()
                    )
                    db.session.add(image_record)
                    db.session.commit()

                    # 图片已落库,按成功逐张扣费(失败/异常不扣)
                    if payer_user_id:
                        charge(
                            payer_user_id, pricing.REDBOOK_IMAGE_PAGE, "redbook_image",
                            "redbook_task", task_id,
                            description=f"小红书图片 第{index}张", team_id=team_id
                        )

                    completed += 1

                    yield {
                        "event": "image",
                        "data": {
                            "index": index,
                            "success": True,
                            "image_url": f"/api/redbook/files/{image_task_id}/{filename}",
                            "thumbnail_url": f"/api/redbook/files/{image_task_id}/thumb_{filename}"
                        }
                    }
                else:
                    failed += 1
                    yield {
                        "event": "error",
                        "data": {
                            "index": index,
                            "error": "图片生成失败"
                        }
                    }

            except Exception as e:
                failed += 1
                logger.error(f"生成图片 [{index}] 失败: {e}")
                yield {
                    "event": "error",
                    "data": {
                        "index": index,
                        "error": str(e)
                    }
                }

        # 更新任务状态
        if task:
            if failed == 0:
                task.status = RedBookTaskStatus.COMPLETED
            elif completed > 0:
                task.status = RedBookTaskStatus.PARTIAL
            else:
                task.status = RedBookTaskStatus.ERROR
            db.session.commit()

        yield {
            "event": "complete",
            "data": {
                "task_id": task_id,
                "image_task_id": image_task_id,
                "total": len(pages),
                "completed": completed,
                "failed": failed
            }
        }

    def _generate_single_image(
        self,
        prompt: str,
        reference_image: Optional[bytes] = None,
        user_images: Optional[List[bytes]] = None
    ) -> Optional[bytes]:
        """生成单张图片"""
        try:
            # 使用统一的 AI 服务
            provider = get_image_provider()
            if not provider or not provider.is_configured():
                logger.error("AI 客户端未配置")
                return None

            # 合并参考图片
            reference_images = []
            if reference_image:
                reference_images.append(reference_image)
            if user_images:
                reference_images.extend(user_images[:2])  # 最多2张用户参考图

            # 调用 AI 生成图片
            result = provider.generate_image(
                prompt,
                reference_images=reference_images if reference_images else None
            )

            if result.success and result.image_data:
                return result.image_data

            logger.warning(f"图片生成失败: {result.error}")
            return None

        except Exception as e:
            logger.error(f"生成图片失败: {e}")
            return None

    def retry_single_image(
        self,
        image_task_id: str,
        page: Dict,
        use_reference: bool = True
    ) -> Dict[str, Any]:
        """重试生成单张图片"""
        try:
            index = page.get("index", 0)
            page_type = page.get("type", "内容")
            page_content = page.get("content", "")

            prompt = self.prompt_template.format(
                page_content=page_content,
                page_type=page_type,
                user_topic="",
                full_outline=""
            )

            image_data = self._generate_single_image(prompt)

            if image_data:
                from backend.services.redbook import RedBookFileService
                file_service = RedBookFileService()

                filename = f"{index}.png"
                file_service.save_image(image_task_id, filename, image_data)

                return {
                    "success": True,
                    "image_url": f"/api/redbook/files/{image_task_id}/{filename}"
                }
            else:
                return {
                    "success": False,
                    "error": "图片生成失败"
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def retry_failed_images(
        self,
        image_task_id: str,
        pages: List[Dict],
        payer_user_id: Optional[str] = None,
        team_id: Optional[str] = None
    ) -> Generator[Dict[str, Any], None, None]:
        """批量重试失败的图片"""
        for page in pages:
            # 积分预检:付费方余额不足以支付下一张时停止
            if payer_user_id and not check_sufficient_credits(
                payer_user_id, pricing.REDBOOK_IMAGE_PAGE, team_id
            ):
                yield {
                    "event": "error",
                    "data": {"index": page.get("index", 0), "error": "积分不足,已停止生成"}
                }
                break

            result = self.retry_single_image(image_task_id, page)

            if result["success"]:
                # 重试成功按成功扣一次图片积分
                if payer_user_id:
                    charge(
                        payer_user_id, pricing.REDBOOK_IMAGE_PAGE, "redbook_image",
                        "redbook_task", image_task_id,
                        description="小红书图片重试", team_id=team_id
                    )
                yield {
                    "event": "image",
                    "data": {
                        "index": page.get("index", 0),
                        "success": True,
                        "image_url": result["image_url"]
                    }
                }
            else:
                yield {
                    "event": "error",
                    "data": {
                        "index": page.get("index", 0),
                        "error": result.get("error", "重试失败")
                    }
                }

        yield {
            "event": "complete",
            "data": {
                "total": len(pages)
            }
        }

    def regenerate_image(
        self,
        image_task_id: str,
        page: Dict,
        use_reference: bool = True,
        full_outline: str = "",
        user_topic: str = ""
    ) -> Dict[str, Any]:
        """重新生成图片"""
        return self.retry_single_image(image_task_id, page, use_reference)


# 单例实例
_service_instance = None


def get_image_service() -> ImageService:
    """获取图片服务实例"""
    global _service_instance
    if _service_instance is None:
        _service_instance = ImageService()
    return _service_instance
