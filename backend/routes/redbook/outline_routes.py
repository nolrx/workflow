"""
RedBook Outline routes

大纲生成 API
"""
import base64
import logging
import time

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.services import pricing
from backend.services.credit_service import charge, check_sufficient_credits
from backend.utils.response import error_response

logger = logging.getLogger(__name__)

redbook_outline_bp = Blueprint("redbook_outline", __name__)


@redbook_outline_bp.route("/outline", methods=["POST"])
@jwt_required()
def generate_outline():
    """
    生成大纲（支持图片上传）

    请求格式：
    1. multipart/form-data（带图片文件）
       - topic: 主题文本
       - images: 图片文件列表

    2. application/json（无图片或 base64 图片）
       - topic: 主题文本
       - images: base64 编码的图片数组（可选）

    返回：
    - success: 是否成功
    - data: { outline, pages }
    """
    start_time = time.time()

    try:
        user_id = get_jwt_identity()

        # 解析请求数据
        try:
            topic, images = _parse_outline_request()
        except UploadValidationError as e:
            return error_response("VALIDATION_ERROR", str(e), 400)

        if not topic:
            return error_response("VALIDATION_ERROR", "参数错误：topic 不能为空", 400)

        # 积分预检(大纲生成发生在任务创建前,按个人余额计费)
        if not check_sufficient_credits(user_id, pricing.REDBOOK_OUTLINE):
            return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)

        # 调用大纲生成服务
        logger.info(f"开始生成大纲，主题: {topic[:50]}...")

        from backend.services.redbook import get_outline_service
        outline_service = get_outline_service()
        result = outline_service.generate_outline(topic, images if images else None)

        elapsed = time.time() - start_time
        if result["success"]:
            # 大纲生成成功,按成功计费
            charge(user_id, pricing.REDBOOK_OUTLINE, "redbook_outline", "redbook_task", "",
                   description="小红书大纲生成")
            logger.info(f"大纲生成成功，耗时 {elapsed:.2f}s，共 {len(result.get('pages', []))} 页")
            return jsonify({
                "success": True,
                "data": {
                    "outline": result.get("outline"),
                    "pages": result.get("pages", [])
                }
            }), 200
        else:
            logger.error(f"大纲生成失败: {result.get('error')}")
            return error_response("SERVER_ERROR", result.get("error", "大纲生成失败"), 500)

    except Exception as e:
        logger.error(f"大纲生成异常: {e}")
        return error_response("SERVER_ERROR", f"大纲生成异常: {str(e)}", 500)


# Upload limits
MAX_UPLOAD_IMAGES = 10
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB per image


class UploadValidationError(Exception):
    """Exception raised for upload validation failures"""
    pass


def _parse_outline_request():
    """
    解析大纲生成请求

    支持两种格式：
    1. multipart/form-data - 用于文件上传
    2. application/json - 用于 base64 图片

    返回：
        tuple: (topic, images) - 主题和图片列表

    Raises:
        UploadValidationError: If upload limits are exceeded
    """
    # 检查是否是 multipart/form-data（带图片文件）
    if request.content_type and "multipart/form-data" in request.content_type:
        topic = request.form.get("topic")
        images = []

        # 获取上传的图片文件
        if "images" in request.files:
            files = request.files.getlist("images")

            # Validate number of images
            if len(files) > MAX_UPLOAD_IMAGES:
                raise UploadValidationError(f"最多上传 {MAX_UPLOAD_IMAGES} 张图片")

            for file in files:
                if file and file.filename:
                    # Check file size
                    file.seek(0, 2)  # Seek to end
                    size = file.tell()
                    file.seek(0)  # Reset to beginning

                    if size > MAX_IMAGE_SIZE:
                        raise UploadValidationError(f"单张图片大小不能超过 {MAX_IMAGE_SIZE // (1024 * 1024)}MB")

                    image_data = file.read()
                    images.append(image_data)

        return topic, images

    # JSON 请求（无图片或 base64 图片）
    data = request.get_json()
    topic = data.get("topic")
    images = []

    # 支持 base64 格式的图片
    images_base64 = data.get("images", [])
    if images_base64:
        # Validate number of images
        if len(images_base64) > MAX_UPLOAD_IMAGES:
            raise UploadValidationError(f"最多上传 {MAX_UPLOAD_IMAGES} 张图片")

        for img_b64 in images_base64:
            # 移除可能的 data URL 前缀
            if "," in img_b64:
                img_b64 = img_b64.split(",")[1]
            image_data = base64.b64decode(img_b64)

            # Check decoded size
            if len(image_data) > MAX_IMAGE_SIZE:
                raise UploadValidationError(f"单张图片大小不能超过 {MAX_IMAGE_SIZE // (1024 * 1024)}MB")

            images.append(image_data)

    return topic, images
