"""
RedBook Image model

生成的图片记录
"""
import uuid
from datetime import datetime
from backend.extensions import db


class RedBookImageStatus:
    """图片状态常量"""
    PENDING = "pending"       # 待生成
    GENERATING = "generating"  # 生成中
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"         # 生成失败


class RedBookImage(db.Model):
    """RedBook 生成图片模型"""
    __tablename__ = "redbook_images"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = db.Column(
        db.String(36),
        db.ForeignKey("redbook_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    page_id = db.Column(
        db.String(36),
        db.ForeignKey("redbook_pages.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    # 图片信息
    order_index = db.Column(db.Integer, nullable=False, default=0)  # 顺序索引（对应页面索引）
    image_path = db.Column(db.String(500), nullable=True)  # 图片文件路径
    thumbnail_path = db.Column(db.String(500), nullable=True)  # 缩略图路径
    filename = db.Column(db.String(255), nullable=True)  # 文件名

    # 生成相关
    prompt_used = db.Column(db.Text, nullable=True)  # 实际使用的提示词
    seed = db.Column(db.Integer, nullable=True)  # 生成种子（如果支持）

    # 状态
    status = db.Column(
        db.String(20),
        nullable=False,
        default=RedBookImageStatus.PENDING,
        index=True
    )
    error_message = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, default=0)  # 重试次数

    # 时间戳
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    generated_at = db.Column(db.DateTime, nullable=True)  # 图片生成完成时间

    # 关系
    page = db.relationship("RedBookPage", backref=db.backref("image", uselist=False))

    def to_dict(self) -> dict:
        """转换为字典"""
        # 构建图片 URL
        image_url = None
        thumbnail_url = None
        if self.task and self.task.image_task_id:
            if self.filename:
                image_url = f"/api/redbook/files/{self.task.image_task_id}/{self.filename}"
            if self.thumbnail_path:
                thumbnail_url = f"/api/redbook/files/{self.task.image_task_id}/{self.thumbnail_path}"

        return {
            "id": self.id,
            "task_id": self.task_id,
            "page_id": self.page_id,
            "order_index": self.order_index,
            "filename": self.filename,
            "image_url": image_url,
            "thumbnail_url": thumbnail_url,
            "prompt_used": self.prompt_used,
            "status": self.status,
            "error_message": self.error_message,
            "retry_count": self.retry_count,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "generated_at": self.generated_at.isoformat() + "Z" if self.generated_at else None,
        }
