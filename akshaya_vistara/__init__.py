# This file makes akshaya_vistara a Python package.
# Importing the Celery app here ensures it is loaded when Django starts,
# so that shared_task decorators use this app automatically.
from .celery import app as celery_app  # noqa: F401

__all__ = ["celery_app"]
