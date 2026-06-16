"""
RedBook Task model

主任务记录
"""
import uuid
from datetime import datetime
from backend.extensions import db


class RedBookTaskStatus:
    """任务状态常量"""
    DRAFT = "draft"           # 草稿：已创建大纲，未开始生成
    GENERATING = "generating"  # 生成中：正在生成图片
    PARTIAL = "partial"       # 部分完成：有部分图片生成成功
    COMPLETED = "completed"   # 已完成：所有图片已生成
    ERROR = "error"           # 错误：生成过程中出现错误


class RedBookTask(db.Model):
    """RedBook 任务/历史记录模型"""
    __tablename__ = "redbook_tasks"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    # 基本信息
    title = db.Column(db.String(500), nullable=False)  # 主题/标题
    topic = db.Column(db.Text, nullable=True)  # 原始输入的主题文本

    # 大纲数据
    outline_raw = db.Column(db.Text, nullable=True)  # 原始大纲文本
    outline_parsed = db.Column(db.JSON, nullable=True)  # 解析后的大纲 JSON

    # 图片生成相关
    image_task_id = db.Column(db.String(36), nullable=True)  # 图片生成任务 ID
    thumbnail_path = db.Column(db.String(500), nullable=True)  # 缩略图路径

    # 文案内容
    content_titles = db.Column(db.JSON, nullable=True)  # 生成的标题列表
    content_copywriting = db.Column(db.Text, nullable=True)  # 生成的文案
    content_tags = db.Column(db.JSON, nullable=True)  # 生成的标签列表

    # 状态
    status = db.Column(
        db.String(20),
        nullable=False,
        default=RedBookTaskStatus.DRAFT,
        index=True
    )
    error_message = db.Column(db.Text, nullable=True)

    # 权限
    visibility = db.Column(db.String(20), default="private")  # private, team, public

    # 时间戳
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    user = db.relationship("User", backref=db.backref("redbook_tasks", lazy="dynamic"))
    team = db.relationship("Team", backref=db.backref("redbook_tasks", lazy="dynamic"))
    pages = db.relationship(
        "RedBookPage",
        backref="task",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="RedBookPage.order_index"
    )
    images = db.relationship(
        "RedBookImage",
        backref="task",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="RedBookImage.order_index"
    )

    def to_dict(self, include_pages: bool = True, include_images: bool = True) -> dict:
        """转换为字典"""
        data = {
            "id": self.id,
            "user_id": self.user_id,
            "team_id": self.team_id,
            "title": self.title,
            "topic": self.topic,
            "outline_raw": self.outline_raw,
            "outline_parsed": self.outline_parsed,
            "image_task_id": self.image_task_id,
            "thumbnail_path": self.thumbnail_path,
            "thumbnail_url": f"/api/redbook/files/{self.image_task_id}/{self.thumbnail_path}" if self.thumbnail_path and self.image_task_id else None,
            "content": {
                "titles": self.content_titles,
                "copywriting": self.content_copywriting,
                "tags": self.content_tags,
            } if self.content_titles or self.content_copywriting or self.content_tags else None,
            "status": self.status,
            "error_message": self.error_message,
            "visibility": self.visibility,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }

        if include_pages:
            pages_list = self.pages.all()
            data["pages"] = [page.to_dict() for page in pages_list]
            data["page_count"] = len(data["pages"])

        if include_images:
            images_list = self.images.all()
            data["images"] = [image.to_dict() for image in images_list]
            data["image_count"] = len(data["images"])
            data["completed_count"] = sum(1 for img in data["images"] if img["status"] == "completed")

        return data

    def to_list_dict(self) -> dict:
        """转换为列表展示用的简化字典"""
        return {
            "id": self.id,
            "title": self.title,
            "topic": self.topic,
            "status": self.status,
            "image_task_id": self.image_task_id,
            "thumbnail_path": self.thumbnail_path,
            "thumbnail_url": f"/api/redbook/files/{self.image_task_id}/{self.thumbnail_path}" if self.thumbnail_path and self.image_task_id else None,
            "outline_parsed": self.outline_parsed,
            "page_count": self.pages.count(),
            "image_count": self.images.count(),
            "completed_count": self.images.filter_by(status="completed").count(),
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }
