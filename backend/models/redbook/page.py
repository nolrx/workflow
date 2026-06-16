"""
RedBook Page model

大纲页面
"""
import uuid
from datetime import datetime
from backend.extensions import db


class RedBookPageType:
    """页面类型常量"""
    COVER = "封面"      # 封面页
    CONTENT = "内容"    # 内容页
    SUMMARY = "总结"    # 总结页


class RedBookPage(db.Model):
    """RedBook 大纲页面模型"""
    __tablename__ = "redbook_pages"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = db.Column(
        db.String(36),
        db.ForeignKey("redbook_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # 页面信息
    order_index = db.Column(db.Integer, nullable=False, default=0)  # 顺序索引
    page_type = db.Column(db.String(20), nullable=True)  # 封面/内容/总结
    content = db.Column(db.Text, nullable=False)  # 页面内容文本

    # 图片生成提示词（可选自定义）
    image_prompt = db.Column(db.Text, nullable=True)

    # 时间戳
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "order_index": self.order_index,
            "page_type": self.page_type,
            "content": self.content,
            "image_prompt": self.image_prompt,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }

    @classmethod
    def from_outline_page(cls, task_id: str, index: int, page_data: dict) -> "RedBookPage":
        """从解析的大纲页面数据创建 Page 实例"""
        content = page_data.get("content", "")
        page_type = page_data.get("type", RedBookPageType.CONTENT)

        return cls(
            task_id=task_id,
            order_index=index,
            page_type=page_type,
            content=content,
        )
