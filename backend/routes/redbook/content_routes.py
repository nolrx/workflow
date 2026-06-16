"""
RedBook Content routes

文案生成 API（标题、正文、标签）
"""
import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.extensions import db
from backend.models.redbook import RedBookTask
from backend.services import pricing
from backend.services.credit_service import charge, check_sufficient_credits
from backend.utils.response import error_response

logger = logging.getLogger(__name__)

redbook_content_bp = Blueprint("redbook_content", __name__)


@redbook_content_bp.route("/content", methods=["POST"])
@jwt_required()
def generate_content():
    """
    生成文案内容（标题、正文、标签）

    请求体：
    - task_id: 任务 ID（必填）
    - topic: 主题文本
    - outline: 大纲文本

    返回：
    - success: 是否成功
    - data: { titles, copywriting, tags }
    """
    try:
        user_id = get_jwt_identity()
        data = request.get_json()

        task_id = data.get("task_id")
        topic = data.get("topic")
        outline = data.get("outline")

        if not task_id:
            return error_response("VALIDATION_ERROR", "参数错误：task_id 不能为空", 400)

        # 验证任务存在
        task = RedBookTask.query.filter_by(id=task_id, user_id=user_id).first()
        if not task:
            return error_response("NOT_FOUND", f"任务不存在: {task_id}", 404)

        # 如果没有提供 topic 和 outline，从任务中获取
        if not topic:
            topic = task.topic or task.title
        if not outline:
            outline = task.outline_raw

        if not topic and not outline:
            return error_response("VALIDATION_ERROR", "参数错误：需要提供 topic 或 outline", 400)

        # 积分预检(团队任务从团队余额扣费)
        if not check_sufficient_credits(user_id, pricing.REDBOOK_CONTENT, task.team_id):
            return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)

        logger.info(f"开始生成文案: task={task_id}")

        from backend.services.redbook import get_content_service
        content_service = get_content_service()
        result = content_service.generate_content(topic, outline)

        if result["success"]:
            # 保存到任务
            content_data = result.get("data", {})
            task.content_titles = content_data.get("titles")
            task.content_copywriting = content_data.get("copywriting")
            task.content_tags = content_data.get("tags")
            db.session.commit()

            # 文案生成成功,按成功计费
            charge(user_id, pricing.REDBOOK_CONTENT, "redbook_content", "redbook_task",
                   task_id, description="小红书文案生成", team_id=task.team_id)

            logger.info(f"文案生成成功: task={task_id}")
            return jsonify({
                "success": True,
                "data": content_data
            }), 200
        else:
            logger.error(f"文案生成失败: {result.get('error')}")
            return error_response("SERVER_ERROR", result.get("error", "文案生成失败"), 500)

    except Exception as e:
        db.session.rollback()
        logger.error(f"文案生成异常: {e}")
        return error_response("SERVER_ERROR", f"文案生成异常: {str(e)}", 500)


@redbook_content_bp.route("/content/<task_id>", methods=["GET"])
@jwt_required()
def get_content(task_id):
    """
    获取任务的文案内容

    路径参数：
    - task_id: 任务 ID

    返回：
    - success: 是否成功
    - data: { titles, copywriting, tags }
    """
    try:
        user_id = get_jwt_identity()
        task = RedBookTask.query.filter_by(id=task_id, user_id=user_id).first()

        if not task:
            return error_response("NOT_FOUND", f"任务不存在: {task_id}", 404)

        return jsonify({
            "success": True,
            "data": {
                "titles": task.content_titles,
                "copywriting": task.content_copywriting,
                "tags": task.content_tags,
            }
        }), 200

    except Exception as e:
        logger.error(f"获取文案失败: {e}")
        return error_response("SERVER_ERROR", f"获取文案失败: {str(e)}", 500)
