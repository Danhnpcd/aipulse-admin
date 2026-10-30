#!/usr/bin/env python3
"""
Font Utility for PIL / Pillow (Cross-Platform Windows + Linux Cloud Docker)
Loads native Vietnamese-compatible TrueType Fonts (Segoe UI, Arial, DejaVu Sans, Noto Sans)
to eliminate tofu/box artifacts when rendering Vietnamese text on both Windows and Linux Cloud.
"""

import os
from typing import Optional, Any

try:
    from PIL import ImageFont
except ImportError:
    ImageFont = None

SYSTEM_FONTS = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

SYSTEM_BOLD_FONTS = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\tahomabd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def get_vietnamese_font(size: int = 18, bold: bool = False) -> Optional[Any]:
    """
    Returns a TrueType font supporting full Vietnamese Unicode characters.
    Falls back gracefully if not found.
    """
    if ImageFont is None:
        return None

    candidates = SYSTEM_BOLD_FONTS if bold else SYSTEM_FONTS
    for font_path in candidates:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size=size)
            except Exception:
                continue

    try:
        return ImageFont.load_default()
    except Exception:
        return None
