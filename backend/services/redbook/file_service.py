"""
RedBook File Service

文件操作服务，负责图片存储和管理
"""
import os
import shutil
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class RedBookFileService:
    """RedBook 文件服务"""

    def __init__(self):
        # 存储根目录
        self.root_dir = Path(__file__).parent.parent.parent.parent / "uploads" / "redbook"
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def get_task_dir(self, task_id: str) -> str:
        """获取任务目录路径"""
        task_dir = self.root_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        return str(task_dir)

    def save_image(
        self,
        task_id: str,
        filename: str,
        image_data: bytes,
        create_thumbnail: bool = True
    ) -> tuple:
        """
        保存图片

        Args:
            task_id: 任务 ID
            filename: 文件名
            image_data: 图片数据
            create_thumbnail: 是否创建缩略图

        Returns:
            (image_path, thumbnail_path)
        """
        task_dir = self.get_task_dir(task_id)

        # 保存原图
        image_path = os.path.join(task_dir, filename)
        with open(image_path, "wb") as f:
            f.write(image_data)

        thumbnail_path = None
        if create_thumbnail:
            # 创建缩略图
            thumbnail_filename = f"thumb_{filename}"
            thumbnail_path = os.path.join(task_dir, thumbnail_filename)
            try:
                from backend.services.redbook.image_utils import compress_image
                thumbnail_data = compress_image(image_data, max_size_kb=50)
                with open(thumbnail_path, "wb") as f:
                    f.write(thumbnail_data)
            except Exception as e:
                logger.warning(f"创建缩略图失败: {e}")
                thumbnail_path = None

        return image_path, thumbnail_path

    def delete_image(self, task_id: str, filename: str) -> bool:
        """删除图片（包括缩略图）"""
        task_dir = self.get_task_dir(task_id)

        try:
            # 删除原图
            image_path = os.path.join(task_dir, filename)
            if os.path.exists(image_path):
                os.remove(image_path)

            # 删除缩略图
            thumbnail_path = os.path.join(task_dir, f"thumb_{filename}")
            if os.path.exists(thumbnail_path):
                os.remove(thumbnail_path)

            return True
        except Exception as e:
            logger.error(f"删除图片失败: {e}")
            return False

    def delete_task_files(self, task_id: str) -> bool:
        """删除任务的所有文件"""
        task_dir = self.root_dir / task_id

        try:
            if task_dir.exists():
                shutil.rmtree(task_dir)
                logger.info(f"已删除任务目录: {task_dir}")
            return True
        except Exception as e:
            logger.error(f"删除任务目录失败: {e}")
            return False

    def list_images(self, task_id: str) -> list:
        """列出任务的所有图片（不包括缩略图）"""
        task_dir = self.root_dir / task_id

        if not task_dir.exists():
            return []

        images = []
        for filename in os.listdir(task_dir):
            if filename.startswith("thumb_"):
                continue
            if filename.endswith((".png", ".jpg", ".jpeg")):
                images.append(filename)

        # 按文件名排序
        def get_index(filename):
            try:
                return int(filename.split(".")[0])
            except ValueError:
                return 999

        images.sort(key=get_index)
        return images
