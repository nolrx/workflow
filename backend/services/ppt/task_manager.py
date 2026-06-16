"""
PPT Task Manager - handles background tasks using ThreadPoolExecutor
No need for Celery or Redis, uses in-memory task tracking
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable

from sqlalchemy import func

from backend.extensions import db
from backend.models.ppt import PPTPage, PPTPageImageVersion, PPTTask
from backend.services import pricing
from backend.services.credit_service import charge, check_sufficient_credits

logger = logging.getLogger(__name__)


class PPTTaskManager:
    """Simple task manager using ThreadPoolExecutor"""

    def __init__(self, max_workers: int = 4):
        """Initialize task manager"""
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.active_tasks = {}  # task_id -> Future
        self.lock = threading.Lock()

    def submit_task(self, task_id: str, func: Callable, *args, **kwargs):
        """Submit a background task"""
        future = self.executor.submit(func, task_id, *args, **kwargs)

        with self.lock:
            self.active_tasks[task_id] = future

        # Add callback to clean up when done and log exceptions
        future.add_done_callback(lambda f: self._task_done_callback(task_id, f))

    def _task_done_callback(self, task_id: str, future):
        """Handle task completion and log any exceptions"""
        try:
            # Check if task raised an exception
            exception = future.exception()
            if exception:
                logger.error(f"Task {task_id} failed with exception: {exception}", exc_info=exception)
        except Exception as e:
            logger.error(f"Error in task callback for {task_id}: {e}", exc_info=True)
        finally:
            self._cleanup_task(task_id)

    def _cleanup_task(self, task_id: str):
        """Clean up completed task"""
        with self.lock:
            if task_id in self.active_tasks:
                del self.active_tasks[task_id]

    def is_task_active(self, task_id: str) -> bool:
        """Check if task is still running"""
        with self.lock:
            return task_id in self.active_tasks

    def shutdown(self):
        """Shutdown the executor"""
        self.executor.shutdown(wait=True)


# Global task manager instance
ppt_task_manager = PPTTaskManager(max_workers=4)


def save_image_with_version(image, project_id: str, page_id: str, file_service,
                            page_obj=None, image_format: str = 'PNG') -> tuple:
    """
    Save image and create version record

    Args:
        image: PIL Image object
        project_id: Project ID
        page_id: Page ID
        file_service: PPTFileService instance
        page_obj: PPTPage object (optional, if provided updates page status)
        image_format: Image format, default PNG

    Returns:
        tuple: (image_path, version_number)
    """
    # Use MAX query to ensure version number safety
    max_version = db.session.query(func.max(PPTPageImageVersion.version_number)).filter_by(page_id=page_id).scalar() or 0
    next_version = max_version + 1

    # Batch update: mark all old versions as not current
    PPTPageImageVersion.query.filter_by(page_id=page_id).update({'is_current': False})

    # Save image to final location
    image_path = file_service.save_generated_image(
        image, project_id, page_id,
        version_number=next_version,
        image_format=image_format
    )

    # Create new version record
    new_version = PPTPageImageVersion(
        page_id=page_id,
        image_path=image_path,
        version_number=next_version,
        is_current=True
    )
    db.session.add(new_version)

    # Update page status and image path if page_obj provided
    if page_obj:
        page_obj.generated_image_path = image_path
        page_obj.status = 'COMPLETED'
        page_obj.updated_at = datetime.utcnow()

    # Commit transaction
    db.session.commit()

    logger.debug(f"Page {page_id} image saved as version {next_version}: {image_path}")

    return image_path, next_version


def update_task_progress(task_id: str, completed: int, failed: int, total: int):
    """
    Update task progress in database

    Args:
        task_id: Task ID
        completed: Number of completed items
        failed: Number of failed items
        total: Total number of items
    """
    task = PPTTask.query.get(task_id)
    if task:
        task.update_progress(completed=completed, failed=failed)
        db.session.commit()
        logger.info(f"Task {task_id} progress: {completed}/{total} completed, {failed} failed")


def mark_task_completed(task_id: str, **extra_progress):
    """
    Mark task as completed

    Args:
        task_id: Task ID
        **extra_progress: Extra fields to add to progress
    """
    task = PPTTask.query.get(task_id)
    if task:
        task.status = 'COMPLETED'
        task.completed_at = datetime.utcnow()
        if extra_progress:
            progress = task.get_progress() or {}
            progress.update(extra_progress)
            task.set_progress(progress)
        db.session.commit()
        logger.info(f"Task {task_id} marked as COMPLETED")


def mark_task_failed(task_id: str, error_message: str):
    """
    Mark task as failed

    Args:
        task_id: Task ID
        error_message: Error message
    """
    task = PPTTask.query.get(task_id)
    if task:
        task.status = 'FAILED'
        task.error_message = error_message
        task.completed_at = datetime.utcnow()
        db.session.commit()
        logger.error(f"Task {task_id} marked as FAILED: {error_message}")


def mark_task_processing(task_id: str):
    """
    Mark task as processing

    Args:
        task_id: Task ID
    """
    task = PPTTask.query.get(task_id)
    if task:
        task.status = 'PROCESSING'
        db.session.commit()
        logger.info(f"Task {task_id} status updated to PROCESSING")


def generate_descriptions_task(task_id: str, app, project_id: str, language: str = 'zh'):
    """
    Background task to generate descriptions for all pages.

    Args:
        task_id: Task ID
        app: Flask app instance
        project_id: Project ID
        language: Output language code
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from backend.models.ppt import PPTPage, PPTProject
    from backend.services.ai.factory import get_text_provider
    from backend.services.ppt.generation_service import (
        build_outline_text_from_pages,
        generate_page_description,
    )

    with app.app_context():
        try:
            # Mark task as processing
            mark_task_processing(task_id)

            # Get project and pages
            project = PPTProject.query.get(project_id)
            if not project:
                mark_task_failed(task_id, "Project not found")
                return

            # Resolve the payer from the project (background thread has no request ctx).
            payer_user_id = project.user_id
            team_id = project.team_id

            pages = PPTPage.query.filter_by(project_id=project_id).order_by(PPTPage.order_index).all()
            if not pages:
                mark_task_failed(task_id, "No pages found")
                return

            # Build outline data for context
            full_outline = []
            for page in pages:
                outline = page.get_outline_content() or {}
                full_outline.append({
                    "title": outline.get("title", "Untitled"),
                    "points": outline.get("points", []),
                    "part": page.part
                })

            # Get AI provider (new instance for thread safety)
            ai_provider = get_text_provider(force_new=True)
            if not ai_provider or not ai_provider.is_configured():
                mark_task_failed(task_id, "AI service not configured")
                return

            completed_count = 0
            failed_count = 0
            stopped_insufficient = False

            # Process pages sequentially (to avoid rate limits)
            for i, page in enumerate(pages):
                # Stop early if the payer can no longer afford the next page.
                if not check_sufficient_credits(payer_user_id, pricing.PPT_DESCRIPTION_PAGE, team_id):
                    stopped_insufficient = True
                    logger.warning(
                        f"Descriptions stopped at page {i + 1}/{len(pages)}: insufficient credits"
                    )
                    break
                try:
                    page_outline = page.get_outline_content() or {}
                    page_outline["part"] = page.part

                    description = generate_page_description(
                        page_outline=page_outline,
                        page_index=i + 1,
                        full_outline=full_outline,
                        idea_prompt=project.idea_prompt or "",
                        ai_provider=ai_provider,
                        language=language
                    )

                    # Update page
                    page.set_description_content(description)
                    page.status = "DESCRIPTION_GENERATED"
                    page.updated_at = datetime.utcnow()
                    db.session.commit()

                    # Bill this page only after it was persisted successfully.
                    charge(payer_user_id, pricing.PPT_DESCRIPTION_PAGE, "ppt_description",
                           "ppt_project", project_id,
                           description=f"PPT description page {i + 1}", team_id=team_id)

                    completed_count += 1
                    update_task_progress(task_id, completed_count, failed_count, len(pages))
                    logger.info(f"Description generated for page {i + 1}/{len(pages)}")

                except Exception as e:
                    logger.error(f"Failed to generate description for page {i + 1}: {e}")
                    failed_count += 1
                    update_task_progress(task_id, completed_count, failed_count, len(pages))

            # Update project status
            project.status = "DESCRIPTIONS_GENERATED"
            project.updated_at = datetime.utcnow()
            db.session.commit()

            if stopped_insufficient and completed_count == 0:
                mark_task_failed(task_id, "积分不足,无法生成描述")
            else:
                mark_task_completed(task_id)
            logger.info(f"Description generation completed: {completed_count} success, {failed_count} failed")

        except Exception as e:
            logger.error(f"generate_descriptions_task failed: {e}", exc_info=True)
            mark_task_failed(task_id, str(e))


def generate_images_task(task_id: str, app, project_id: str, page_ids: list = None, language: str = 'zh'):
    """
    Background task to generate images for pages.

    Args:
        task_id: Task ID
        app: Flask app instance
        project_id: Project ID
        page_ids: Optional list of specific page IDs to generate
        language: Output language code
    """
    from io import BytesIO

    from flask import current_app
    from PIL import Image

    from backend.models.ppt import PPTPage, PPTProject
    from backend.services.ai.factory import get_image_provider
    from backend.services.ppt.file_service import PPTFileService
    from backend.services.ppt.generation_service import (
        build_outline_text_from_pages,
        generate_page_image,
    )

    with app.app_context():
        try:
            # Mark task as processing
            mark_task_processing(task_id)

            # Get project
            project = PPTProject.query.get(project_id)
            if not project:
                mark_task_failed(task_id, "Project not found")
                return

            # Resolve the payer from the project (background thread has no request ctx).
            payer_user_id = project.user_id
            team_id = project.team_id

            # Get pages to generate
            query = PPTPage.query.filter_by(project_id=project_id).order_by(PPTPage.order_index)
            if page_ids:
                query = query.filter(PPTPage.id.in_(page_ids))
            pages = query.all()

            if not pages:
                mark_task_failed(task_id, "No pages found")
                return

            # Build outline text for context
            all_pages = PPTPage.query.filter_by(project_id=project_id).order_by(PPTPage.order_index).all()
            outline_text = build_outline_text_from_pages(all_pages)

            # Get AI provider (new instance for thread safety)
            ai_provider = get_image_provider(force_new=True)
            if not ai_provider or not ai_provider.is_configured():
                mark_task_failed(task_id, "AI service not configured")
                return

            # Initialize file service
            upload_folder = current_app.config.get("UPLOAD_FOLDER", "uploads")
            file_service = PPTFileService(upload_folder)

            # Load template image if exists
            template_image = None
            template_path = file_service.get_template_path(project_id)
            if template_path:
                try:
                    with open(template_path, 'rb') as f:
                        template_image = f.read()
                except Exception as e:
                    logger.warning(f"Failed to load template: {e}")

            completed_count = 0
            failed_count = 0
            stopped_insufficient = False

            # Process pages sequentially (Gemini has rate limits)
            for i, page in enumerate(pages):
                # Stop early if the payer can no longer afford the next image.
                if not check_sufficient_credits(payer_user_id, pricing.PPT_IMAGE_PAGE, team_id):
                    stopped_insufficient = True
                    logger.warning(
                        f"Images stopped at page {page.order_index + 1}/{len(pages)}: insufficient credits"
                    )
                    break
                try:
                    # Get description content
                    desc_content = page.get_description_content() or {}
                    page_description = desc_content.get("text", "")

                    # If no description, build one from outline
                    if not page_description:
                        outline = page.get_outline_content() or {}
                        title = outline.get("title", "Untitled")
                        points = outline.get("points", [])
                        page_description = f"Page title: {title}\n\nPage text:\n" + "\n".join(f"- {p}" for p in points)

                    current_section = page.part or ""

                    # Generate image
                    image_data = generate_page_image(
                        page_description=page_description,
                        page_index=page.order_index + 1,
                        outline_text=outline_text,
                        current_section=current_section,
                        ai_provider=ai_provider,
                        template_image=template_image,
                        extra_requirements=project.extra_requirements,
                        language=language
                    )

                    # Convert bytes to PIL Image
                    pil_image = Image.open(BytesIO(image_data))

                    # Save image with versioning
                    image_path, version_number = save_image_with_version(
                        image=pil_image,
                        project_id=project_id,
                        page_id=page.id,
                        file_service=file_service,
                        page_obj=page
                    )

                    # Bill this image only after it was persisted successfully.
                    charge(payer_user_id, pricing.PPT_IMAGE_PAGE, "ppt_image",
                           "ppt_project", project_id,
                           description=f"PPT image page {page.order_index + 1}", team_id=team_id)

                    completed_count += 1
                    update_task_progress(task_id, completed_count, failed_count, len(pages))
                    logger.info(f"Image generated for page {page.order_index + 1}/{len(pages)} (version {version_number})")

                except Exception as e:
                    logger.error(f"Failed to generate image for page {page.order_index + 1}: {e}", exc_info=True)
                    failed_count += 1
                    page.status = "FAILED"
                    db.session.commit()
                    update_task_progress(task_id, completed_count, failed_count, len(pages))

            # Update project status — PARTIAL if any page failed or was skipped for credits.
            if stopped_insufficient or failed_count > 0:
                project.status = "PARTIAL"
            else:
                project.status = "COMPLETED"
            project.updated_at = datetime.utcnow()
            db.session.commit()

            if stopped_insufficient and completed_count == 0:
                mark_task_failed(task_id, "积分不足,无法生成图片")
            else:
                mark_task_completed(task_id)
            logger.info(f"Image generation completed: {completed_count} success, {failed_count} failed")

        except Exception as e:
            logger.error(f"generate_images_task failed: {e}", exc_info=True)
            mark_task_failed(task_id, str(e))
