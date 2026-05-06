"""
core/management/commands/generate_pwa_icons.py

Generates simple PNG icons for the PWA manifest using Pillow.
Run once after setup:
    python manage.py generate_pwa_icons

Creates:
    static/icons/icon-192.png
    static/icons/icon-512.png
"""

import os
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = "Generate simple PWA icons (192×192 and 512×512) using Pillow"

    def handle(self, *args, **options):
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            self.stderr.write("Pillow not installed. Run: pip install Pillow")
            return

        icons_dir = os.path.join(settings.BASE_DIR, "static", "icons")
        os.makedirs(icons_dir, exist_ok=True)

        for size in (192, 512):
            img = Image.new("RGB", (size, size), color="#4f46e5")  # Indigo background
            draw = ImageDraw.Draw(img)

            # Draw a simple "T" letter for Akshaya Vistara
            font_size = size // 2
            text = "T"
            # Use default font (no external font needed)
            try:
                font = ImageFont.truetype("arial.ttf", font_size)
            except Exception:
                font = ImageFont.load_default()

            # Center the text
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            x = (size - tw) // 2
            y = (size - th) // 2
            draw.text((x, y), text, fill="white", font=font)

            # Draw rounded corner overlay (decorative bar at bottom)
            bar_h = size // 8
            draw.rectangle([(0, size - bar_h), (size, size)], fill="#3730a3")

            path = os.path.join(icons_dir, f"icon-{size}.png")
            img.save(path, "PNG")
            self.stdout.write(self.style.SUCCESS(f"Created: {path}"))

        self.stdout.write(self.style.SUCCESS("PWA icons generated successfully!"))
