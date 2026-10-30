#!/usr/bin/env python3
"""
TikTok Fashion AI — Specialized Google Omni Prompt & Script Generator.
Generates ultra-realistic, cinematic fashion video prompts for Google Omni,
along with viral TikTok captions, trending hashtags, and product affiliate hooks.
"""
import random
import argparse
from typing import Dict, Any, List

FASHION_STYLES = {
    "KOREAN_CHIC": {
        "name": "Hàn Quốc Tiểu Thư (Korean Chic & Aesthetic)",
        "outfit": "an elegant pastel beige pleated A-line midi dress with delicate puffy chiffon sleeves, paired with minimalist pearl jewelry and stylish pointed cream heels",
        "setting": "a cozy modern aesthetic glasshouse cafe in Seoul with soft morning sunlight filtering through Monstera plants",
        "action": "the graceful young East Asian woman smiling gently at the camera, doing a subtle elegant 360-degree spin to showcase the dress flow, natural hair floating softly",
        "caption": "Mẫu đầm tiểu thư chuẩn Hàn Quốc khiến ai gặp cũng phải ngoái nhìn ✨ Nàng chấm set này mấy điểm? Link sản phẩm ở góc trái màn hình nhé nàng ơi! 👇",
        "hashtags": ["#thoitrangnu", "#outfitideas", "#koreanstyle", "#vayxinh", "#tiktokshopvn", "#xuhuong2026", "#learnontiktok", "#reviewthoitrang"]
    },
    "OLD_MONEY": {
        "name": "Quý Tộc Sang Chảnh (Old Money Aesthetic)",
        "outfit": "a luxurious ivory white French tweed buttoned blazer vest with tailored high-waisted wide-leg cream trousers, designer leather handbag, gold minimalist watch",
        "setting": "a classic European marble corridor with grand archways, warm neoclassical ambient chandelier lighting and luxurious wooden paneling",
        "action": "the poised sophisticated model walking slowly towards the camera with confident catwalk poise, buttoning her blazer, soft cinematic slow-motion",
        "caption": "Phong cách Old Money sang ngút ngàn mà giá chỉ bằng 3 ly trà sữa 💎 Mặc set này đi làm hay đi tiệc đều tôn dáng đỉnh cao! Nàng xem chi tiết ở giỏ hàng nhé ✨",
        "hashtags": ["#oldmoneyaesthetic", "#sangchanh", "#phoido", "#blazer", "#congso", "#tiktokshopvn", "#reviewlamdep", "#thoitrangcongso"]
    },
    "OFFICE_LADY": {
        "name": "Công Sở Hiện Đại (Modern Office & Smart Chic)",
        "outfit": "a sleek tailored navy blue slim-fit power suit with a soft silk inner camisole, high-waisted ankle trousers that elongate legs, elegant nude pumps",
        "setting": "a bright high-rise corporate executive glass office overlooking a panoramic modern city skyline at daytime",
        "action": "the confident modern businesswoman walking gracefully, adjusting her collar with a warm professional smile, looking stunning and charismatic",
        "caption": "Bí quyết hack chân dài thêm 5cm cho các nàng công sở đây rồi 💼 Set blazer tôn dáng cực chuẩn, chất vải mát không nhăn. Link ưu đãi độc quyền ở giỏ hàng nha!",
        "hashtags": ["#thoitrangcongso", "#hackdang", "#setbo", "#blazernu", "#tiktokshopvietnam", "#ootdvietnam", "#xuhuong", "#goclamdep"]
    },
    "Y2K_STREETWEAR": {
        "name": "Y2K Năng Động Dạo Phố (Y2K & Trendy Streetwear)",
        "outfit": "a trendy cropped vintage baby tee with rhinestone graphic, low-rise baggy cargo pants with metallic utility chains, platform sneakers and a mini shoulder bag",
        "setting": "a sun-drenched aesthetic city street with neon storefronts, trendy graffiti walls and vibrant street life at golden hour",
        "action": "the cool energetic young woman posing dynamically, swaying side-to-side to upbeat music, playfully brushing her hair behind her ear",
        "caption": "Outfit dạo phố bao ngầu cho các nàng cá tính 🔥 Ai bảo mặc đồ thoải mái là không thời trang? Bấm vào giỏ hàng rinh ngay set này giá hời nha!",
        "hashtags": ["#streetwearvn", "#y2kfashion", "#cargopants", "#aocroptop", "#phoidonamnu", "#genzfashion", "#tiktokshopvn", "#trending"]
    },
    "EVENING_PARTY": {
        "name": "Dạ Tiệc Quyến Rũ (Glamour Evening & Night Out)",
        "outfit": "a breathtaking emerald green silk satin backless halter-neck maxi gown with a tasteful high side slit, sparkling diamond drop earrings and strappy silver heels",
        "setting": "an opulent rooftop lounge overlooking shimmering city lights at night, soft candle illumination and romantic golden bokeh background",
        "action": "the glamorous woman stepping gracefully onto the rooftop terrace, turning back with a captivating alluring smile, the silk dress shimmering under soft lights",
        "caption": "Chiếc đầm dạ hội biến nàng thành tâm điểm của mọi bữa tiệc đêm nay 🌙 Chất lụa satin mướt mát ôm trọn đường cong. Đang có mã giảm 30% ở giỏ hàng đó nha!",
        "hashtags": ["#damdatiec", "#damsexy", "#vaybody", "#sangtrong", "#partydress", "#tiktokshopvn", "#goclamdep", "#reviewthoitrang"]
    },
    "SUMMER_RESORT": {
        "name": "Nàng Thơ Đi Biển & Du Lịch (Summer Floral & Resort)",
        "outfit": "a romantic lightweight floral chiffon maxi sundress with ruffle hem, off-shoulder neckline, wide-brim straw hat, and woven bohemian tote bag",
        "setting": "a picturesque tropical coastal beach with turquoise waves gently breaking on white sand, golden sunset glow in the sky",
        "action": "the cheerful radiant woman walking barefoot near the water edge, holding her straw hat against the ocean breeze, dress flowing beautifully in the wind",
        "caption": "Đi biển hay du lịch hè mà không có chiếc váy hoa nhí này là tiếc lắm nè các nàng ơi 🌊 Váy bay bổng lên ảnh sống ảo 10/10. Link ở góc trái màn hình nha!",
        "hashtags": ["#vaydibien", "#vayhoanhi", "#dulich", "#ootdbeach", "#thoitranghe", "#tiktokshopvn", "#xuhuong", "#vayxinhgiare"]
    }
}

OMNI_CAMERA_SPECS = (
    "Vertical 9:16 aspect ratio smartphone cinematic video, 4K UHD, photorealistic high fashion runway catalog, "
    "ultra-detailed natural skin texture with subtle pores, silky realistic hair moving in gentle airflow, "
    "accurate hyper-detailed fabric weave and drape (silk, satin, cotton, tweed, chiffon), "
    "natural human facial anatomy, expressive realistic eyes, cinematic 35mm lens, f/1.8 shallow depth of field, "
    "soft flattering natural lighting, high dynamic range, 60fps smooth fluid motion, zero AI glitch, Vogue editorial quality"
)

NEGATIVE_PROMPT = (
    "deformed fingers, extra limbs, bad anatomy, cartoon, anime, 3d render, plastic skin, oversaturated, "
    "blurry, distorted face, awkward hands, unnatural posture, jitter, flicker, low resolution, watermark, text"
)

def generate_fashion_prompt(style_key: str = None) -> Dict[str, Any]:
    if not style_key or style_key not in FASHION_STYLES:
        style_key = random.choice(list(FASHION_STYLES.keys()))
    
    style_data = FASHION_STYLES[style_key]
    
    full_prompt = (
        f"A cinematic high-fashion video of an attractive, stylish 22-year-old Asian woman wearing {style_data['outfit']}. "
        f"Scene takes place in {style_data['setting']}. "
        f"Action: {style_data['action']}. "
        f"{OMNI_CAMERA_SPECS}."
    )
    
    return {
        "style_key": style_key,
        "style_name": style_data["name"],
        "google_omni_prompt": full_prompt,
        "negative_prompt": NEGATIVE_PROMPT,
        "aspect_ratio": "9:16 (Vertical Mobile)",
        "tiktok_caption": style_data["caption"],
        "recommended_hashtags": " ".join(style_data["hashtags"]),
        "ai_trace_removal_checklist": [
            "1. Xóa metadata C2PA và SynthID watermark từ file video Google Omni",
            "2. Ghi đè metadata EXIF thiết bị quay: Apple iPhone 15 Pro Max (Camera 24mm f/1.78, 60fps)",
            "3. Thêm lớp nhiễu hạt nhẹ (grain 0.5%) hoặc filter màu film tự nhiên để phá vỡ mẫu pixel nhân tạo",
            "4. Chèn âm thanh thịnh hành (Trending Sound) trực tiếp trong ứng dụng TikTok khi đăng"
        ]
    }

def main():
    parser = argparse.ArgumentParser(description="TikTok Fashion AI — Google Omni Prompt Generator")
    parser.add_argument("--style", type=str, choices=list(FASHION_STYLES.keys()), help="Style key (KOREAN_CHIC, OLD_MONEY, OFFICE_LADY, etc.)")
    parser.add_argument("--all", action="store_true", help="Generate prompts for all styles")
    args = parser.parse_args()

    if args.all:
        print("=== BỘ TẬP HỢP PROMPT GOOGLE OMNI CHO TẤT CẢ PHONG CÁCH THỜI TRANG NỮ ===")
        for k in FASHION_STYLES:
            res = generate_fashion_prompt(k)
            print(f"\n[{res['style_key']}] {res['style_name']}")
            print("Prompt Omni:")
            print(f"  {res['google_omni_prompt']}")
            print("Caption TikTok:")
            print(f"  {res['tiktok_caption']}")
            print("Hashtags:")
            print(f"  {res['recommended_hashtags']}")
            print("-" * 60)
    else:
        res = generate_fashion_prompt(args.style)
        print(f"=== TIKTOK FASHION PROMPT: {res['style_name']} ===")
        print("\n[PROMPT GOOGLE OMNI]:")
        print(res["google_omni_prompt"])
        print("\n[NEGATIVE PROMPT]:")
        print(res["negative_prompt"])
        print("\n[CAPTION TIKTOK GIẬT TÍT]:")
        print(res["tiktok_caption"])
        print("\n[HASHTAGS TRIỆU VIEW]:")
        print(res["recommended_hashtags"])
        print("\n[CHECKLIST XÓA AI TRACE]:")
        for step in res["ai_trace_removal_checklist"]:
            print(f"  ✓ {step}")

if __name__ == "__main__":
    main()
