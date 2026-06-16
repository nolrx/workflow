"""
RedBook Image Utilities

图片处理工具函数
"""
import io
import logging
from PIL import Image

logger = logging.getLogger(__name__)


def compress_image(image_data: bytes, max_size_kb: int = 50) -> bytes:
    """
    压缩图片到指定大小

    Args:
        image_data: 原始图片数据
        max_size_kb: 目标最大大小（KB）

    Returns:
        压缩后的图片数据
    """
    try:
        img = Image.open(io.BytesIO(image_data))

        # 转换为 RGB（如果是 RGBA）
        if img.mode == "RGBA":
            img = img.convert("RGB")

        # 计算缩放比例
        max_dimension = 400  # 缩略图最大边长
        width, height = img.size
        if width > height:
            if width > max_dimension:
                ratio = max_dimension / width
                new_size = (max_dimension, int(height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
        else:
            if height > max_dimension:
                ratio = max_dimension / height
                new_size = (int(width * ratio), max_dimension)
                img = img.resize(new_size, Image.Resampling.LANCZOS)

        # 压缩质量
        quality = 85
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True)

        # 如果还是太大，继续降低质量
        while output.tell() > max_size_kb * 1024 and quality > 20:
            quality -= 10
            output = io.BytesIO()
            img.save(output, format="JPEG", quality=quality, optimize=True)

        return output.getvalue()

    except Exception as e:
        logger.error(f"图片压缩失败: {e}")
        # 如果压缩失败，返回原始数据
        return image_data


def get_image_dimensions(image_data: bytes) -> tuple:
    """
    获取图片尺寸

    Args:
        image_data: 图片数据

    Returns:
        (width, height)
    """
    try:
        img = Image.open(io.BytesIO(image_data))
        return img.size
    except Exception as e:
        logger.error(f"获取图片尺寸失败: {e}")
        return (0, 0)
