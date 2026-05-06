"""
ocr/tasks.py

Background tasks for OCR app.
(All OCR-heavy tasks removed. This file remains for future background utility.)
"""

import logging
from celery import shared_task

logger = logging.getLogger(__name__)

@shared_task(name="ocr.tasks.cleanup_old_submissions")
def cleanup_old_submissions():
    """Placeholder for periodic cleanup task."""
    pass
