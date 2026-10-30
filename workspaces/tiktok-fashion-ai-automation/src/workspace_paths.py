#!/usr/bin/env python3
"""
Cross-platform path resolver for TikTok Fashion AI Studio.
Works seamlessly on both Windows Local (d:/ai-business-simulator) and Linux Cloud Docker (/app).
Prevents Linux from accidentally creating relative folders named 'G:\\My Drive\\...'.
"""

import os
from pathlib import Path
from typing import Optional

SRC_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SRC_DIR.parent
PROJECT_ROOT = Path(os.getenv("APP_BASE_DIR", str(WORKSPACE_DIR.parent.parent))).resolve()


def get_windows_gdrive_root() -> Optional[Path]:
    """Returns G:\\My Drive\\TikTok_Fashion_AI_Studio ONLY when mounted on Windows."""
    custom = os.getenv("GDRIVE_STUDIO_ROOT", "").strip()
    if custom:
        p = Path(custom)
        if p.exists():
            return p
    if os.name == "nt" and Path("G:/My Drive").exists():
        root = Path(r"G:\My Drive\TikTok_Fashion_AI_Studio")
        try:
            root.mkdir(parents=True, exist_ok=True)
            return root
        except Exception:
            return None
    return None


def get_windows_legacy_gdrive_models() -> Optional[Path]:
    if os.name == "nt" and Path("G:/My Drive").exists():
        p = Path(r"G:\My Drive\Anh_Nguoi_Mau_Fashion_AI")
        if p.exists():
            return p
    return None
