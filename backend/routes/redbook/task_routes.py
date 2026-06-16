"""
RedBook Task routes (History management)

任务/历史记录管理 API
"""

import io
import logging
import os
import zipfile

from flask import Blueprint, jsonify, request, send_file
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.extensions import db
from backend.models.redbook import RedBookPage, RedBookTask, RedBookTaskStatus
from backend.utils.response import error_response

logger = logging.getLogger(__name__)

redbook_task_bp = Blueprint("redbook_task", __name__)


# ==================== CRUD 操作 ====================


@redbook_task_bp.route("", methods=["POST"])
@jwt_required()
def create_task():
    """
    创建新的 RedBook 任务（草稿状态）

    请求体：
    - title: 主题标题（必填）
    - topic: 原始主题文本
    - outline_raw: 原始大纲文本
    - outline_parsed: 解析后的大纲 JSON
    - team_id: 团队 ID（可选）

    返回：
    - success: 是否成功
    - data: 任务详情
    """
    try:
        user_id = get_jwt_identity()
        data = request.get_json()

        title = data.get("title")
        if not title:
            return error_response("VALIDATION_ERROR", "参数错误：title 不能为空", 400)

        # 创建任务
        task = RedBookTask(
            user_id=user_id,
            team_id=data.get("team_id"),
            title=title,
            topic=data.get("topic"),
            outline_raw=data.get("outline_raw"),
            outline_parsed=data.get("outline_parsed"),
            status=RedBookTaskStatus.DRAFT,
        )
        db.session.add(task)
        db.session.flush()

        # 如果有解析的大纲，创建页面记录
        outline_parsed = data.get("outline_parsed")
        if outline_parsed and "pages" in outline_parsed:
            for i, page_data in enumerate(outline_parsed["pages"]):
                page = RedBookPage(
                    task_id=task.id,
                    order_index=i,
                    page_type=page_data.get("type", "内容"),
                    content=page_data.get("content", ""),
                )
                db.session.add(page)

        db.session.commit()

        return jsonify({"success": True, "data": task.to_dict()}), 201

    except Exception as e:
        db.session.rollback()
        logger.error(f"创建任务失败: {e}")
        return error_response("SERVER_ERROR", f"创建任务失败: {str(e)}", 500)


@redbook_task_bp.route("", methods=["GET"])
@jwt_required()
def list_tasks():
    """
    获取任务列表（分页）

    查询参数：
    - limit: 每页数量（默认 20，最大 100）
    - offset: 偏移量（默认 0）
    - status: 状态过滤（可选）
    - keyword: 搜索关键词（可选）

    返回：
    - success: 是否成功
    - data: { tasks, has_more, limit, offset }
    """
    try:
        user_id = get_jwt_identity()
        limit = min(max(1, int(request.args.get("limit", 20))), 100)
        offset = max(0, int(request.args.get("offset", 0)))
        status = request.args.get("status")
        keyword = request.args.get("keyword")

        # 构建查询
        query = RedBookTask.query.filter_by(user_id=user_id)

        if status and status != "all":
            query = query.filter_by(status=status)

        if keyword:
            query = query.filter(RedBookTask.title.ilike(f"%{keyword}%"))

        # 按更新时间倒序
        query = query.order_by(RedBookTask.updated_at.desc())

        # 分页: fetch limit + 1 to check for more
        tasks_with_extra = query.offset(offset).limit(limit + 1).all()
        has_more = len(tasks_with_extra) > limit
        tasks = tasks_with_extra[:limit]

        return jsonify(
            {
                "success": True,
                "data": {
                    "tasks": [task.to_list_dict() for task in tasks],
                    "has_more": has_more,
                    "limit": limit,
                    "offset": offset,
                },
            }
        ), 200

    except Exception as e:
        logger.error(f"获取任务列表失败: {e}")
        return error_response("SERVER_ERROR", f"获取任务列表失败: {str(e)}", 500)


# ==================== 统计信息 ====================
# NOTE: Stats endpoint must be defined BEFORE dynamic routes like /<task_id>
# to prevent Flask from matching "stats" as a task_id


@redbook_task_bp.route("/stats", methods=["GET"])
@jwt_required()
def get_stats():
    """
    获取任务统计信息

    返回：
    - success: 是否成功
    - data: { total, completed, generating, error, draft }
    """
    try:
        user_id = get_jwt_identity()

        # 总数
        total = RedBookTask.query.filter_by(user_id=user_id).count()

        # 按状态统计
        status_counts = (
            db.session.query(RedBookTask.status, db.func.count(RedBookTask.id))
            .filter_by(user_id=user_id)
            .group_by(RedBookTask.status)
            .all()
        )

        by_status = {status: count for status, count in status_counts}

        return jsonify({
            "success": True,
            "data": {
                "total": total,
                "completed": by_status.get("completed", 0),
                "generating": by_status.get("generating", 0),
                "error": by_status.get("error", 0),
                "draft": by_status.get("draft", 0),
            }
        }), 200

    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")
        return error_response("SERVER_ERROR", f"获取统计信息失败: {str(e)}", 500)


# ==================== 任务详情 ====================


@redbook_task_bp.route("/<task_id>", methods=["GET"])
@jwt_required()
def get_task(task_id):
    """
    获取任务详情

    路径参数：
    - task_id: 任务 ID

    返回：
    - success: 是否成功
    - data: 任务详情
    """
    try:
        user_id = get_jwt_identity()
        task = RedBookTask.query.filter_by(id=task_id, user_id=user_id).first()

        if not task:
            return error_response("NOT_FOUND", f"任务不存在: {task_id}", 404)

        return jsonify({"success": True, "data": task.to_dict()}), 200

    except Exception as e:
        logger.error(f"获取任务详情失败: {e}")
        return error_response("SERVER_ERROR", f"获取任务详情失败: {str(e)}", 500)


@redbook_task_bp.route("/<task_id>", methods=["PUT"])
@jwt_required()
def update_task(task_id):
    """
    更新任务

    路径参数：
    - task_id: 任务 ID

    请求体（均为可选）：
    - title: 标题
    - outline_raw: 原始大纲
    - outline_parsed: 解析后的大纲
    - status: 状态
    - image_task_id: 图片任务 ID
    - thumbnail_path: 缩略图路径
    - content: 文案内容 { titles, copywriting, tags }

    返回：
    - success: 是否成功
    """
    try:
        user_id = get_jwt_identity()
        task = RedBookTask.query.filter_by(id=task_id, user_id=user_id).first()

        if not task:
            return error_response("NOT_FOUND", f"任务不存在: {task_id}", 404)

        data = request.get_json()

        # 更新基本字段
        if "title" in data:
            task.title = data["title"]
        if "outline_raw" in data:
            task.outline_raw = data["outline_raw"]
        if "outline_parsed" in data:
            task.outline_parsed = data["outline_parsed"]
        if "status" in data:
            task.status = data["status"]
        if "image_task_id" in data:
            task.image_task_id = data["image_task_id"]
        if "thumbnail_path" in data:
            task.thumbnail_path = data["thumbnail_path"]
        if "error_message" in data:
            task.error_message = data["error_message"]

        # 更新文案内容
        if "content" in data:
            content = data["content"]
            if "titles" in content:
                task.content_titles = content["titles"]
            if "copywriting" in content:
                task.content_copywriting = content["copywriting"]
            if "tags" in content:
                task.content_tags = content["tags"]

        db.session.commit()

        return jsonify({"success": True, "data": task.to_dict()}), 200

    except Exception as e:
        db.session.rollback()
        logger.error(f"更新任务失败: {e}")
        return error_response("SERVER_ERROR", f"更新任务失败: {str(e)}", 500)


@redbook_task_bp.route("/<task_id>", methods=["DELETE"])
@jwt_required()
def delete_task(task_id):
    """
    删除任务

    路径参数：
    - task_id: 任务 ID

    返回：
    - success: 是否成功
    """
    try:
        user_id = get_jwt_identity()
        task = RedBookTask.query.filter_by(id=task_id, user_id=user_id).first()

        if not task:
            return error_response("NOT_FOUND", f"任务不存在: {task_id}", 404)

        # 删除关联的图片文件
        if task.image_task_id:
            from backend.services.redbook import RedBookFileService

            file_service = RedBookFileService()
            file_service.delete_task_files(task.image_task_id)

        # 删除数据库记录（级联删除 pages 和 images）
        db.session.delete(task)
        db.session.commit()

        return jsonify({"success": True}), 200

    except Exception as e:
        db.session.rollback()
        logger.error(f"删除任务失败: {e}")
        return error_response("SERVER_ERROR", f"删除任务失败: {str(e)}", 500)


# ==================== 下载功能 ====================


@redbook_task_bp.route("/<task_id>/download", methods=["GET"])
@jwt_required()
def download_task_images(task_id):
    """
    下载任务的所有图片为 ZIP 文件

    路径参数：
    - task_id: 任务 ID

    返回：
    - 成功：ZIP 文件
    - 失败：JSON 错误信息
    """
    try:
        user_id = get_jwt_identity()
        task = RedBookTask.query.filter_by(id=task_id, user_id=user_id).first()

        if not task:
            return error_response("NOT_FOUND", f"任务不存在: {task_id}", 404)

        if not task.image_task_id:
            return error_response("NOT_FOUND", "该任务没有生成的图片", 404)

        # 获取图片目录
        from backend.services.redbook import RedBookFileService

        file_service = RedBookFileService()
        task_dir = file_service.get_task_dir(task.image_task_id)

        if not os.path.exists(task_dir):
            return error_response("NOT_FOUND", "图片目录不存在", 404)

        # 创建 ZIP 文件
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename in os.listdir(task_dir):
                # 跳过缩略图
                if filename.startswith("thumb_"):
                    continue
                if filename.endswith((".png", ".jpg", ".jpeg")):
                    file_path = os.path.join(task_dir, filename)
                    # 重命名为 page_N.png
                    try:
                        index = int(filename.split(".")[0])
                        archive_name = f"page_{index + 1}.png"
                    except ValueError:
                        archive_name = filename
                    zf.write(file_path, archive_name)

        zip_buffer.seek(0)

        # 清理文件名
        safe_title = (
            "".join(
                c
                for c in task.title
                if c.isalnum() or c in (" ", "-", "_") or "\u4e00" <= c <= "\u9fff"
            ).strip()
            or "images"
        )

        return send_file(
            zip_buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{safe_title}.zip",
        )

    except Exception as e:
        logger.error(f"下载图片失败: {e}")
        return error_response("SERVER_ERROR", f"下载图片失败: {str(e)}", 500)
