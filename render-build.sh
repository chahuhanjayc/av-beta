#!/usr/bin/env bash
# exit on error
set -o errexit

# Install System Dependencies (Tesseract & Poppler)
# This requires Render's "Apt" Buildpack if not using Docker
apt-get update && apt-get install -y tesseract-ocr poppler-utils libpq-dev

# Install Python Dependencies
pip install -r requirements.txt

# Collect Static Files
python manage.py collectstatic --no-input

# Run Migrations
python manage.py migrate
