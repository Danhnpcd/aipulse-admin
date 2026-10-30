#!/usr/bin/env python3
"""
Google Drive Model & Project Sync — Upgraded Version with:
- Dual-folder scanning: G:\\My Drive\\Anh_Nguoi_Mau_Fashion_AI and G:\\My Drive\\TikTok_Fashion_AI_Studio\\01_Anh_Nguoi_Mau
- Automatic bidirectional file mirroring
- Pillow fast thumbnail caching
- Random model selector for effortless batch production
"""

import os
import sys
import shutil
import random
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

try:
    from PIL import Image
except ImportError:
    Image = None

_WS_DIR = Path(__file__).resolve().parent.parent
PRIMARY_ROOT = Path(r"G:\My Drive\TikTok_Fashion_AI_Studio") if os.name == "nt" else Path("/nonexistent/gdrive")
LEGACY_MODEL_FOLDER = Path(r"G:\My Drive\Anh_Nguoi_Mau_Fashion_AI") if os.name == "nt" else Path("/nonexistent/gdrive_legacy")
FALLBACK_ROOT = _WS_DIR / "drive_sync"
THUMBNAIL_CACHE_DIR = _WS_DIR / "cache" / "thumbnails"

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def get_project_root() -> Path:
    if os.name == "nt" and Path("G:/My Drive").exists():
        try:
            PRIMARY_ROOT.mkdir(parents=True, exist_ok=True)
            return PRIMARY_ROOT
        except Exception:
            pass
    FALLBACK_ROOT.mkdir(parents=True, exist_ok=True)
    return FALLBACK_ROOT


class GoogleDriveProjectSync:
    """Manages model photos and project outputs across Google Drive folders."""

    def __init__(self):
        self.user_email = "danhnpcd@gmail.com"
        self.root = get_project_root()
        self.dir_models = self.root / "01_Anh_Nguoi_Mau"
        self.dir_videos = self.root / "02_Video_Xuat_Ban"
        self.dir_prompts = self.root / "03_Prompts_Google_Omni"
        self.dir_reports = self.root / "04_Bao_Cao_Du_An"
        self.legacy_models = LEGACY_MODEL_FOLDER
        self.thumbnail_cache = THUMBNAIL_CACHE_DIR

        # Ensure all directories exist
        for d in [self.dir_models, self.dir_videos, self.dir_prompts, self.dir_reports, self.thumbnail_cache]:
            d.mkdir(parents=True, exist_ok=True)

        # Sync folders on startup
        self.sync_dual_folders()

    def sync_dual_folders(self) -> int:
        """Bidirectionally mirrors files between Anh_Nguoi_Mau_Fashion_AI and 01_Anh_Nguoi_Mau."""
        synced_count = 0
        if not self.legacy_models.exists() or not self.dir_models.exists():
            return 0

        # Legacy -> Studio
        for file in self.legacy_models.iterdir():
            if file.is_file() and file.suffix.lower() in SUPPORTED_EXTENSIONS:
                dest = self.dir_models / file.name
                if not dest.exists():
                    try:
                        shutil.copy2(file, dest)
                        synced_count += 1
                    except Exception:
                        pass

        # Studio -> Legacy
        for file in self.dir_models.iterdir():
            if file.is_file() and file.suffix.lower() in SUPPORTED_EXTENSIONS:
                dest = self.legacy_models / file.name
                if not dest.exists():
                    try:
                        shutil.copy2(file, dest)
                        synced_count += 1
                    except Exception:
                        pass

        return synced_count

    def _ensure_thumbnail(self, source_path: Path) -> Path:
        """Generates a compressed 240x320 thumbnail using Pillow."""
        thumb_filename = f"thumb_{source_path.stem}.jpg"
        thumb_path = self.thumbnail_cache / thumb_filename

        if thumb_path.exists() and thumb_path.stat().st_mtime >= source_path.stat().st_mtime:
            return thumb_path

        if Image:
            try:
                with Image.open(source_path) as img:
                    img = img.convert("RGB")
                    img.thumbnail((240, 320), Image.Resampling.LANCZOS)
                    img.save(thumb_path, "JPEG", quality=82)
                return thumb_path
            except Exception:
                pass

        return source_path

    def get_status(self) -> Dict[str, Any]:
        """Returns overview of the project workspace on Google Drive."""
        self.sync_dual_folders()
        photos = self.list_model_photos()
        videos = [f.name for f in self.dir_videos.iterdir() if f.is_file() and f.suffix.lower() in {".mp4", ".mov", ".webm"}]
        prompts = [f.name for f in self.dir_prompts.iterdir() if f.is_file()]
        reports = [f.name for f in self.dir_reports.iterdir() if f.is_file()]
        is_mounted = self.root.drive.upper() == "G:" or "Google Drive" in str(self.root)

        return {
            "account": self.user_email,
            "project_name": "TikTok Fashion AI Studio",
            "root_path": str(self.root),
            "is_gdrive_mounted": is_mounted,
            "drive_status_text": "🟢 GOOGLE DRIVE ĐÃ ĐỒNG BỘ (danhnpcd@gmail.com)" if is_mounted else "🟡 THƯ MỤC CỤC BỘ",
            "stats": {
                "total_models": len(photos),
                "total_videos": len(videos),
                "total_prompts": len(prompts),
                "total_reports": len(reports)
            },
            "subfolders": {
                "models": str(self.dir_models),
                "legacy_models": str(self.legacy_models) if self.legacy_models.exists() else str(self.dir_models),
                "videos": str(self.dir_videos),
                "prompts": str(self.dir_prompts),
                "reports": str(self.dir_reports)
            },
            "web_drive_url": "https://drive.google.com/drive/my-drive"
        }

    def list_model_photos(self, include_base64: bool = False) -> List[Dict[str, Any]]:
        """Scans model photos across both folders and returns metadata + preview URLs."""
        self.sync_dual_folders()
        seen_filenames = set()
        photos = []

        # Scan both directories
        scan_dirs = [self.dir_models]
        if self.legacy_models.exists() and self.legacy_models != self.dir_models:
            scan_dirs.append(self.legacy_models)

        for directory in scan_dirs:
            if not directory.exists():
                continue
            for file in directory.iterdir():
                if file.is_file() and file.suffix.lower() in SUPPORTED_EXTENSIONS:
                    if file.name in seen_filenames:
                        continue
                    seen_filenames.add(file.name)

                    stat = file.stat()
                    # Ensure thumbnail is prepared
                    thumb_path = self._ensure_thumbnail(file)

                    clean_name = file.stem.replace("_", " ").title()
                    if clean_name.startswith("2026"):
                        clean_name = f"Người Mẫu #{file.stem[-7:]}"

                    photo_item = {
                        "id": file.name,
                        "filename": file.name,
                        "display_name": clean_name,
                        "file_path": str(file),
                        "thumbnail_url": f"/api/tiktok-fashion/models/thumbnail/{file.name}",
                        "view_url": f"/api/tiktok-fashion/models/view/{file.name}",
                        "data_url": f"/api/tiktok-fashion/models/view/{file.name}",
                        "file_size_kb": round(stat.st_size / 1024, 1),
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                    }
                    photos.append(photo_item)

        photos.sort(key=lambda x: x["modified_at"], reverse=True)
        return photos

    def get_model_file_path(self, filename: str) -> Optional[Path]:
        """Finds full path to model image by filename across all model directories."""
        for d in [self.dir_models, self.legacy_models]:
            p = d / filename
            if p.exists():
                return p
        return None

    def get_random_model(self) -> Optional[Dict[str, Any]]:
        """Returns a randomly selected model photo from the available collection."""
        photos = self.list_model_photos()
        if not photos:
            return None
        return random.choice(photos)

    def save_omni_prompt_to_drive(
        self,
        product_title: str,
        style_vibe: str,
        master_prompt: str,
        virtual_tryon_prompt: str,
        storyboard_scenes: List[Dict[str, Any]],
        caption: str,
        hashtags: str
    ) -> str:
        """Saves Google Omni visual prompts and shot list directly to 03_Prompts_Google_Omni."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = "".join(c for c in product_title[:30] if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
        filename = f"Prompt_{safe_title}_{timestamp}.txt"
        file_path = self.dir_prompts / filename

        content = [
            f"=== GOOGLE OMNI FASHION PROMPT & VISUAL STORYBOARD ===",
            f"Sản phẩm: {product_title}",
            f"Phong cách: {style_vibe}",
            f"Thời gian tạo: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Tài khoản Google Drive: {self.user_email}",
            "-" * 60,
            "\n[1. GOOGLE OMNI MULTIMODAL VIRTUAL TRY-ON (DÙNG 2 ẢNH)]: ",
            virtual_tryon_prompt,
            "\n[2. MASTER PROMPT TEXT-TO-VIDEO 4K]: ",
            master_prompt,
            "\n[3. CHỈ ĐẠO PHÂN CẢNH THỊ GIÁC (VISUAL SHOT LIST)]: "
        ]

        for s in storyboard_scenes:
            content.append(f"- Cảnh {s.get('scene', 1)} ({s.get('duration', '3s')}): {s.get('type', '')}")
            content.append(f"  Chỉ đạo: {s.get('prompt', '')}")

        content.extend([
            "-" * 60,
            "\n[4. CAPTION & HASHTAGS ĐĂNG TIKTOK]:",
            caption,
            "\n" + hashtags
        ])

        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(content))

        return str(file_path)

    def export_video_to_drive(self, local_video_path: str, product_title: str) -> Optional[str]:
        """Copies cleaned video file to 02_Video_Xuat_Ban on Google Drive."""
        src = Path(local_video_path)
        if not src.exists():
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = "".join(c for c in product_title[:25] if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
        dest_filename = f"TikTok_Fashion_{safe_title}_{timestamp}.mp4"
        dest_path = self.dir_videos / dest_filename

        try:
            shutil.copy2(src, dest_path)
            return str(dest_path)
        except Exception as e:
            print(f"Error copying video to Google Drive: {e}")
            return None

    def open_subfolder(self, subfolder: str = "root") -> Dict[str, Any]:
        """Opens specified Google Drive subfolder in Windows File Explorer."""
        if subfolder == "models":
            # If user has Anh_Nguoi_Mau_Fashion_AI, open it, else dir_models
            target = self.legacy_models if self.legacy_models.exists() else self.dir_models
        elif subfolder == "videos":
            target = self.dir_videos
        elif subfolder == "prompts":
            target = self.dir_prompts
        elif subfolder == "reports":
            target = self.dir_reports
        else:
            target = self.root

        try:
            if target.exists():
                subprocess.Popen(["explorer.exe", str(target)])
                return {"success": True, "path": str(target)}
            return {"success": False, "error": f"Path not found: {target}"}
        except Exception as e:
            return {"success": False, "error": str(e)}


# Singleton alias
GoogleDriveModelSync = GoogleDriveProjectSync


if __name__ == "__main__":
    sync = GoogleDriveProjectSync()
    photos = sync.list_model_photos()
    print(f"Found {len(photos)} model photos:")
    for p in photos:
        print(f" - {p['display_name']} ({p['filename']}, {p['file_size_kb']} KB)")
