"""
RedBook Image routes

图片生成 API（SSE 流式返回）
"""
import base64
import json
import logging
import os
import re

from flask import Blueprint, Response, jsonify, request, send_file, stream_with_context
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.models.redbook import RedBookTask
from backend.services import pricing
from backend.services.credit_service import charge, check_sufficient_credits
from backend.utils.response import error_response

logger = logging.getLogger(__name__)

redbook_image_bp = Blueprint("redbook_image", __name__)


# ==================== 图片生成 ====================

@redbook_image_bp.route("/generate", methods=["POST"])
@jwt_required()
def generate_images():
    """
    批量生成图片（SSE 流式返回）

    请求体：
    - task_id: 任务 ID（必填，需先创建任务）
    - pages: 页面列表（必填）
    - full_outline: 完整大纲文本
    - user_topic: 用户原始输入主题
    - user_images: base64 编码的用户参考图片列表

    返回：
    SSE 事件流，包含以下事件类型：
    - image: 单张图片生成完成
    - error: 生成错误
    - complete: 全部完成
    """
    try:
        user_id = get_jwt_identity()
        data = request.get_json()

        task_id = data.get("task_id")
        pages = data.get("pages")
        full_outline = data.get("full_outline", "")
        user_topic = data.get("user_topic", "")

        # 解析 base64 格式的用户参考图片
        user_images = _parse_base64_images(data.get("user_images", []))

        if not task_id:
            return error_response("VALIDATION_ERROR", "参数错误：task_id 不能为空", 400)

        if not pages:
            return error_response("VALIDATION_ERROR", "参数错误：pages 不能为空", 400)

        # 验证任务存在且属于当前用户
        task = RedBookTask.query.filter_by(id=task_id, user_id=user_id).first()
        if not task:
            return error_response("NOT_FOUND", f"任务不存在: {task_id}", 404)

        # 积分预检:连一张都付不起则直接拒绝;流式过程中按成功逐张扣费
        team_id = task.team_id
        if not check_sufficient_credits(user_id, pricing.REDBOOK_IMAGE_PAGE, team_id):
            return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)

        logger.info(f"开始图片生成任务: {task_id}, 共 {len(pages)} 页")

        from backend.services.redbook import get_image_service
        image_service = get_image_service()

        def generate():
            """SSE 事件生成器"""
            for event in image_service.generate_images(
                task_id,
                pages,
                full_outline,
                user_images=user_images if user_images else None,
                user_topic=user_topic,
                payer_user_id=user_id,
                team_id=team_id
            ):
                event_type = event["event"]
                event_data = event["data"]

                # 格式化为 SSE 格式
                yield f"event: {event_type}\n"
                yield f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )

    except Exception as e:
        logger.error(f"图片生成异常: {e}")
        return error_response("SERVER_ERROR", f"图片生成异常: {str(e)}", 500)


# ==================== 图片获取 ====================

@redbook_image_bp.route("/files/<image_task_id>/<filename>", methods=["GET"])
def get_image(image_task_id, filename):
    """
    获取图片文件（无需认证）

    路径参数：
    - image_task_id: 图片任务 ID
    - filename: 文件名

    查询参数：
    - thumbnail: 是否返回缩略图（默认 true）

    返回：
    - 成功：图片文件
    - 失败：JSON 错误信息
    """
    try:
        # 安全检查：防止路径穿越攻击
        if not re.match(r'^[\w\-\.]+$', filename):
            return error_response("VALIDATION_ERROR", "Invalid filename", 400)

        thumbnail = request.args.get("thumbnail", "true").lower() == "true"

        from backend.services.redbook import RedBookFileService
        file_service = RedBookFileService()
        task_dir = file_service.get_task_dir(image_task_id)

        if thumbnail:
            # 尝试返回缩略图
            thumb_filename = f"thumb_{filename}"
            thumb_filepath = os.path.join(task_dir, thumb_filename)
            if os.path.exists(thumb_filepath):
                return send_file(thumb_filepath, mimetype="image/png")

        # 返回原图
        filepath = os.path.join(task_dir, filename)
        if not os.path.exists(filepath):
            return error_response("NOT_FOUND", f"图片不存在: {image_task_id}/{filename}", 404)

        return send_file(filepath, mimetype="image/png")

    except Exception as e:
        logger.error(f"获取图片失败: {e}")
        return error_response("SERVER_ERROR", f"获取图片失败: {str(e)}", 500)


# ==================== 重试和重新生成 ====================

@redbook_image_bp.route("/retry", methods=["POST"])
@jwt_required()
def retry_single_image():
    """
    重试生成单张失败的图片

    请求体：
    - task_id: 任务 ID（必填）
    - page: 页面信息（必填）
    - use_reference: 是否使用参考图（默认 true）

    返回：
    - success: 是否成功
    - data: { image_url }
    """
    try:
        user_id = get_jwt_identity()
        data = request.get_json()

        task_id = data.get("task_id")
        page = data.get("page")
        use_reference = data.get("use_reference", True)

        if not task_id or not page:
            return error_response("VALIDATION_ERROR", "参数错误：task_id 和 page 不能为空", 400)

        # 验证任务
        task = RedBookTask.query.filter_by(id=task_id, user_id=user_id).first()
        if not task:
            return error_response("NOT_FOUND", f"任务不存在: {task_id}", 404)

        # 积分预检
        if not check_sufficient_credits(user_id, pricing.REDBOOK_IMAGE_PAGE, task.team_id):
            return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)

        logger.info(f"重试生成图片: task={task_id}, page={page.get('index')}")

        from backend.services.redbook import get_image_service
        image_service = get_image_service()
        result = image_service.retry_single_image(
            task.image_task_id or task_id,
            page,
            use_reference
        )

        if result["success"]:
            # 重试成功按成功扣一次图片积分
            charge(user_id, pricing.REDBOOK_IMAGE_PAGE, "redbook_image", "redbook_task",
                   task_id, description="小红书图片重试", team_id=task.team_id)
            return jsonify({
                "success": True,
                "data": {
                    "image_url": result.get("image_url")
                }
            }), 200
        else:
            return error_response("SERVER_ERROR", result.get("error", "重试失败"), 500)

    except Exception as e:
        logger.error(f"重试图片生成失败: {e}")
        return error_response("SERVER_ERROR", f"重试图片生成失败: {str(e)}", 500)


@redbook_image_bp.route("/retry-failed", methods=["POST"])
@jwt_required()
def retry_failed_images():
    """
    批量重试失败的图片（SSE 流式返回）

    请求体：
    - task_id: 任务 ID（必填）
    - pages: 要重试的页面列表（必填）

    返回：
    SSE 事件流
    """
    try:
        user_id = get_jwt_identity()
        data = request.get_json()

        task_id = data.get("task_id")
        pages = data.get("pages")

        if not task_id or not pages:
            return error_response("VALIDATION_ERROR", "参数错误：task_id 和 pages 不能为空", 400)

        # 验证任务
        task = RedBookTask.query.filter_by(id=task_id, user_id=user_id).first()
        if not task:
            return error_response("NOT_FOUND", f"任务不存在: {task_id}", 404)

        # 积分预检:连一张都付不起则直接拒绝;过程中按成功逐张扣费
        team_id = task.team_id
        if not check_sufficient_credits(user_id, pricing.REDBOOK_IMAGE_PAGE, team_id):
            return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)

        logger.info(f"批量重试失败图片: task={task_id}, 共 {len(pages)} 页")

        from backend.services.redbook import get_image_service
        image_service = get_image_service()

        def generate():
            """SSE 事件生成器"""
            for event in image_service.retry_failed_images(
                task.image_task_id or task_id,
                pages,
                payer_user_id=user_id,
                team_id=team_id
            ):
                event_type = event["event"]
                event_data = event["data"]
                yield f"event: {event_type}\n"
                yield f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )

    except Exception as e:
        logger.error(f"批量重试失败: {e}")
        return error_response("SERVER_ERROR", f"批量重试失败: {str(e)}", 500)


@redbook_image_bp.route("/regenerate", methods=["POST"])
@jwt_required()
def regenerate_image():
    """
    重新生成图片（即使成功的也可以重新生成）

    请求体：
    - task_id: 任务 ID（必填）
    - page: 页面信息（必填）
    - use_reference: 是否使用参考图（默认 true）
    - full_outline: 完整大纲文本（用于上下文）
    - user_topic: 用户原始输入主题

    返回：
    - success: 是否成功
    - data: { image_url }
    """
    try:
        user_id = get_jwt_identity()
        data = request.get_json()

        task_id = data.get("task_id")
        page = data.get("page")
        use_reference = data.get("use_reference", True)
        full_outline = data.get("full_outline", "")
        user_topic = data.get("user_topic", "")

        if not task_id or not page:
            return error_response("VALIDATION_ERROR", "参数错误：task_id 和 page 不能为空", 400)

        # 验证任务
        task = RedBookTask.query.filter_by(id=task_id, user_id=user_id).first()
        if not task:
            return error_response("NOT_FOUND", f"任务不存在: {task_id}", 404)

        # 积分预检
        if not check_sufficient_credits(user_id, pricing.REDBOOK_IMAGE_PAGE, task.team_id):
            return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)

        logger.info(f"重新生成图片: task={task_id}, page={page.get('index')}")

        from backend.services.redbook import get_image_service
        image_service = get_image_service()
        result = image_service.regenerate_image(
            task.image_task_id or task_id,
            page,
            use_reference,
            full_outline=full_outline,
            user_topic=user_topic
        )

        if result["success"]:
            # 重新生成成功按成功扣一次图片积分
            charge(user_id, pricing.REDBOOK_IMAGE_PAGE, "redbook_image", "redbook_task",
                   task_id, description="小红书图片重新生成", team_id=task.team_id)
            return jsonify({
                "success": True,
                "data": {
                    "image_url": result.get("image_url")
                }
            }), 200
        else:
            return error_response("SERVER_ERROR", result.get("error", "重新生成失败"), 500)

    except Exception as e:
        logger.error(f"重新生成图片失败: {e}")
        return error_response("SERVER_ERROR", f"重新生成图片失败: {str(e)}", 500)


# ==================== 辅助函数 ====================

def _parse_base64_images(images_base64: list) -> list:
    """
    解析 base64 编码的图片列表
    """
    if not images_base64:
        return []

    images = []
    for img_b64 in images_base64:
        # 移除可能的 data URL 前缀
        if "," in img_b64:
            img_b64 = img_b64.split(",")[1]
        images.append(base64.b64decode(img_b64))

    return images
