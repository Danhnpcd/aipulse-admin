#!/usr/bin/env python3
"""
Scene Keyframes Engine — Module 4 for TikTok Fashion AI Automation.
Generates 4 distinct cinematic keyframe images for the 4 storyboard shots:
- Scene 1: Hook bắt gặp quay lén (Telephoto voyeur framing through foreground)
- Scene 2: Chuyển động sải bước tự nhiên (Ambient motion & swaying dress)
- Scene 3: Soi cận cảnh chất vải & phom dáng (Close-up texture & silhouette)
- Scene 4: Outro rời khung hình thần thái & biểu tượng giỏ hàng (Departure outro)
Saves keyframes to disk and mirrors to G:\\My Drive\\TikTok_Fashion_AI_Studio\\01_Anh_Nguoi_Mau\\Cac_Canh_Quay
"""

import io
import urllib.request
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
from font_utils import get_vietnamese_font

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageEnhance
except ImportError:
    Image = None

import os as _os
KEYFRAMES_DIR = Path(__file__).resolve().parent.parent / "published" / "scene_keyframes"
KEYFRAMES_DIR.mkdir(parents=True, exist_ok=True)

DRIVE_KEYFRAMES_DIR = Path(r"G:\My Drive\TikTok_Fashion_AI_Studio\01_Anh_Nguoi_Mau\Cac_Canh_Quay") if (_os.name == "nt" and Path("G:/My Drive").exists()) else Path("/nonexistent/gdrive_keyframes")
if _os.name == "nt" and Path("G:/My Drive").exists():
    try:
        DRIVE_KEYFRAMES_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


class SceneKeyframesEngine:
    """Generates 4 scene-specific keyframe images of the dressed model for video synthesis."""

    def __init__(self):
        self.output_dir = KEYFRAMES_DIR
        self.drive_dir = DRIVE_KEYFRAMES_DIR

    def generate_4_scene_keyframes(
        self,
        model_image_path: str,
        garment_image_url: str,
        product_title: str,
        price_vnd: int,
        storyboard: Dict[str, Any],
        scene_info: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Creates 4 distinct visual keyframe images representing the 4 scenes of the storyboard.
        """
        if not Image:
            return []

        # 1. Load base model image (prefer clean dressed image if available)
        base_model = None
        p = Path(model_image_path)
        if p.exists():
            clean_cand = p.parent / f"clean_{p.name}"
            target_file = clean_cand if clean_cand.exists() else p
            base_model = Image.open(target_file).convert("RGB")
        elif model_image_path.startswith("http"):
            try:
                req = urllib.request.Request(model_image_path, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    base_model = Image.open(io.BytesIO(resp.read())).convert("RGB")
            except Exception:
                pass

        if not base_model:
            base_model = Image.new("RGB", (720, 1280), (15, 23, 42))

        # 2. Load garment image
        garment_img = None
        if garment_image_url and garment_image_url.startswith("http"):
            try:
                req = urllib.request.Request(garment_image_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    garment_img = Image.open(io.BytesIO(resp.read())).convert("RGBA")
            except Exception:
                pass

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_w, target_h = 720, 1280
        short_title = product_title.split()[0] if product_title else "Váy"

        scenes_meta = [
            {
                "index": 1,
                "key": "scene_1",
                "timing": "0:00 - 0:03",
                "name": "Nhịp 1: Bắt Gặp Quay Lén (Voyeur Hook)",
                "shot_type": "Góc máy tele 85mm xuyên qua tiền cảnh làm mờ (foreground bokeh)",
                "action": "Người mẫu ở tư thế tự nhiên trong ảnh, lơ đãng nhìn xa, không nhìn camera."
            },
            {
                "index": 2,
                "key": "scene_2",
                "timing": "0:03 - 0:08",
                "name": "Nhịp 2: Chuyển Động Tự Nhiên (Ambient Motion)",
                "shot_type": "Trung cảnh chuyển động theo nhịp bước sải nhẹ nhàng",
                "action": f"Người mẫu chuyển động nhẹ nhàng, tà {short_title.lower()} đung đưa theo dáng đi."
            },
            {
                "index": 3,
                "key": "scene_3",
                "timing": "0:08 - 0:12",
                "name": "Nhịp 3: Soi Chi Tiết Vải & Phom (Texture Close-up)",
                "shot_type": "Cận cảnh đặc tả đường may, chất vải và đường cong eo",
                "action": "Camera zoom mượt mà, bàn tay người mẫu khẽ chỉnh lại vạt áo sang chảnh."
            },
            {
                "index": 4,
                "key": "scene_4",
                "timing": "0:12 - 0:15",
                "name": "Nhịp 4: Outro Rời Khung Hình (Mystery Departure)",
                "shot_type": "Góc nhìn phía sau / nghiêng 45 độ tôn dáng lưng và độ rủ của váy",
                "action": "Người mẫu bước tiếp rời khỏi góc quay, hiển thị nhãn giỏ hàng TikTok Shop."
            }
        ]

        results = []

        # Prepare dressed model composite base
        target_w, target_h = 720, 1280
        if abs((base_model.width / base_model.height) - (target_w / target_h)) < 0.15:
            canvas_base = base_model.resize((target_w, target_h), Image.Resampling.LANCZOS)
        else:
            model_scaled = base_model.copy()
            model_scaled.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
            canvas_base = Image.new("RGB", (target_w, target_h), (10, 15, 29))
            offset_x = (target_w - model_scaled.width) // 2
            offset_y = (target_h - model_scaled.height) // 2
            canvas_base.paste(model_scaled, (offset_x, offset_y))

        # Check if model is already dressed
        model_str = str(model_image_path).lower()
        is_already_dressed = any(k in model_str for k in ["tryon", "dressed", "mau_mac", "preview", "mac_trang_phuc"])
        if garment_img and not is_already_dressed:
            gw = int(canvas_base.width * 0.58)
            gh = int(canvas_base.height * 0.44)
            g_resized = garment_img.resize((gw, gh), Image.Resampling.LANCZOS)
            mask = Image.new("L", (gw, gh), 0)
            d_mask = ImageDraw.Draw(mask)
            d_mask.rounded_rectangle([6, 6, gw - 6, gh - 6], radius=28, fill=230)
            mask = mask.filter(ImageFilter.GaussianBlur(10))
            torso_x = int(canvas_base.width * 0.21)
            torso_y = int(canvas_base.height * 0.28)
            canvas_base.paste(g_resized.convert("RGB"), (torso_x, torso_y), mask)

        for s in scenes_meta:
            scene_canvas = canvas_base.copy()
            draw = ImageDraw.Draw(scene_canvas, "RGBA")

            # Scene 1: Subtle voyeur framing (candid telephoto crop 1.1x with soft natural cinematic vignette)
            if s["index"] == 1:
                # 1.1x subtle telephoto crop centered on model upper body
                cw = int(target_w / 1.1)
                ch = int(target_h / 1.1)
                cx = (target_w - cw) // 2
                cy = int(target_h * 0.05)  # slightly higher to focus on face and upper garment
                cropped = scene_canvas.crop((cx, cy, cx + cw, cy + ch))
                scene_canvas = cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)
                draw = ImageDraw.Draw(scene_canvas, "RGBA")
                
                # Soft subtle vignette (no harsh solid rectangles)
                vig = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
                d_vig = ImageDraw.Draw(vig)
                d_vig.rectangle([0, 0, target_w, target_h], outline=(0, 0, 0, 90), width=35)
                vig = vig.filter(ImageFilter.GaussianBlur(25))
                scene_canvas.paste(vig, (0, 0), vig)
                draw = ImageDraw.Draw(scene_canvas, "RGBA")

            # Scene 2: Ambient Motion Catwalk (Full body tracking in authentic environment)
            elif s["index"] == 2:
                # Enhance natural contrast and warm sunlight
                enhancer = ImageEnhance.Color(scene_canvas)
                scene_canvas = enhancer.enhance(1.08)
                enhancer_b = ImageEnhance.Brightness(scene_canvas)
                scene_canvas = enhancer_b.enhance(1.03)
                draw = ImageDraw.Draw(scene_canvas, "RGBA")

            # Scene 3: Texture & Silhouette Detail (1.35x Macro crop focusing on garment)
            elif s["index"] == 3:
                crop_box = (
                    int(target_w * 0.10),
                    int(target_h * 0.22),
                    int(target_w * 0.90),
                    int(target_h * 0.82)
                )
                cropped = scene_canvas.crop(crop_box)
                scene_canvas = cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)
                draw = ImageDraw.Draw(scene_canvas, "RGBA")

            # Load Unicode fonts
            font_header = get_vietnamese_font(18, bold=True)
            font_cart = get_vietnamese_font(14, bold=True)
            font_cart_sub = get_vietnamese_font(11, bold=False)
            font_bottom = get_vietnamese_font(12, bold=False)

            # Scene 4: Cinematic Outro with Official TikTok Shop Yellow Cart Badge
            if s["index"] == 4:
                # Draw official TikTok Shop Yellow Cart badge
                cart_box = [24, target_h - 135, 340, target_h - 75]
                draw.rounded_rectangle(cart_box, radius=16, fill=(254, 218, 3, 250), outline=(255, 255, 255, 240), width=2)
                draw.text((42, target_h - 120), "🛒 Mua ngay tại giỏ hàng TikTok Shop", font=font_cart, fill=(15, 23, 42, 255))
                draw.text((42, target_h - 98), "Freeship toàn quốc • Đổi trả 7 ngày", font=font_cart_sub, fill=(100, 116, 139, 255))

            # Draw Header Pill on all scene cards
            draw.rectangle([0, 0, target_w, 65], fill=(15, 23, 42, 210))
            draw.line([(0, 65), (target_w, 65)], fill=(255, 45, 85, 255), width=2)
            header_text = f"CẢNH {s['index']} ({s['timing']}) — {s['name'].split(':')[1].strip()}"
            draw.text((20, 22), header_text, font=font_header, fill=(255, 255, 255, 255))

            # Draw bottom prompt note
            draw.rectangle([0, target_h - 55, target_w, target_h], fill=(15, 23, 42, 220))
            draw.text((20, target_h - 40), f"Camera: {s['shot_type'][:65]}...", font=font_bottom, fill=(203, 213, 225, 255))

            # Save file
            filename = f"scene_{s['index']}_{timestamp}.jpg"
            local_path = self.output_dir / filename
            scene_canvas.save(local_path, "JPEG", quality=90)

            # Mirror to Google Drive
            drive_path = None
            if self.drive_dir.exists():
                drive_file = self.drive_dir / filename
                try:
                    scene_canvas.save(drive_file, "JPEG", quality=90)
                    drive_path = str(drive_file)
                except Exception:
                    pass

            results.append({
                "scene_index": s["index"],
                "timing": s["timing"],
                "name": s["name"],
                "shot_type": s["shot_type"],
                "action": s["action"],
                "filename": filename,
                "local_path": str(local_path),
                "drive_path": drive_path,
                "image_url": f"/api/tiktok-fashion/scene-keyframe/{filename}",
                "omni_prompt": (
                    f"Shot {s['index']} ({s['timing']}): {s['shot_type']}. "
                    f"Subject: Attractive Asian model dressed in {product_title}. "
                    f"Action: {s['action']} "
                    f"Authentic Douyin candid aesthetic, 0% eye contact with camera, natural ambient lighting, 9:16 vertical 4K 60fps."
                )
            })

        return results


keyframes_engine = SceneKeyframesEngine()
