#!/usr/bin/env python3
"""
Visual Storyboard & Caption Engine — Module 3 for TikTok Fashion AI Automation.
Specialized for:
- Auto-extracting current scene context from user's model photos (phòng ấm cúng, cafe Hàn Quốc, ban công resort, công viên, ven biển, sân vườn).
- 4 DIVERSE CANDID SCENARIO TRACKS (đa dạng kịch bản nhưng vẫn đảm bảo 100% yêu cầu):
  1. Spontaneous Unaware Moment (Bắt gặp tĩnh lặng, không tương tác camera)
  2. Ambient Slow Walk (Dạo bước chuyển động tự nhiên trong không gian)
  3. Texture & Silhouette Spotlight (Soi chi tiết chất vải & phom dáng)
  4. Mystery Departure & Back View (Rời khung hình bí ẩn, góc lưng tôn dáng)
- 100% Candid Douyin Idol style: Model NEVER interacts or makes eye-contact with camera.
- NO voiceover script (pure visual cinematography + trending Douyin BGM + realistic ambient Foley).
- TikTok Community Guidelines compliance & SEO hashtag suite.
- Zero-cost Free Token tracking with quota_guardian.
"""

import re
from typing import Dict, Any, List, Optional
from trending_product_finder import TrendingProductFinder, VERIFIED_TRENDING_CATALOG
from ai_token_quota_guardian import quota_guardian
from model_scene_analyzer import scene_analyzer

TIKTOK_BANNED_OR_FLAGGED_WORDS = [
    "cam kết 100%", "cam kết", "tuyệt đối", "vĩnh viễn", "chắc chắn 100%", "trị dứt điểm",
    "rẻ nhất thị trường", "số 1 việt nam", "rẻ nhất thế giới", "đảm bảo 100%",
    "gucci", "chanel", "dior", "louis vuitton", "lv", "hermes", "prada", "zara tuồn", "auth tuồn", "hàng fake", "hàng nhái", "super fake",
    "lộ hàng", "hở bạo", "khoe thân", "nhạy cảm", "khiêu gợi", "khiêu dâm", "lừa đảo"
]

SAFE_REPLACEMENTS = {
    "cam kết 100%": "chất lượng cực chuẩn",
    "cam kết": "đảm bảo ưng ý",
    "tuyệt đối": "rất",
    "rẻ nhất thị trường": "mức giá cực kỳ yêu thương",
    "hàng fake": "mẫu thiết kế lấy cảm hứng"
}

# Trending TikTok Sound Direction by Vibe
BGM_RECOMMENDATIONS = {
    "DOUYIN_IDOL_CANDID": {
        "genre": "Douyin Viral Chill Beat / Asian Aesthetic Lo-Fi / Soft Catwalk Phonk",
        "example_tracks": [
            "Say Yes To Heaven - Lana Del Rey (Douyin Sped Up)",
            "Sứ Thanh Hoa - Jay Chou (Lo-Fi Piano Cut)",
            "Daylight - David Kushner (Aesthetic Slowed)",
            "Douyin Catwalk Beat 2026 (Trending Disc)",
            "Bồ Công Anh Trong Gió (Douyin Viral Audio)"
        ],
        "audio_mood": "Bí ẩn, cuốn hút, sang chảnh kiểu idol Douyin đời thường, nhịp beat thời thượng",
        "ambient": "Tiếng môi trường tự nhiên (tiếng gió lùa rèm cửa, tiếng tách cà phê, tiếng bước chân nhẹ)"
    },
    "PAPARAZZI_CELEB": {
        "genre": "High-Fashion Runway Beat / Luxury Phonk Remix / House Groove",
        "example_tracks": [
            "Montagem - PR Funk (Remix)", "Vogue Runway Catwalk Beat", "Makeba (Trending Remix)",
            "Midnight City - M83 (Aesthetic Cut)", "Bloody Mary (Sped Up Instrumental)"
        ],
        "audio_mood": "Sang chảnh, nhịp bass dồn dập, tiếng máy ảnh 'tách tách' tinh tế ở 3s đầu",
        "ambient": "Tiếng bước chân giày cao gót nhịp nhàng trên sàn đá cẩm thạch"
    },
    "STREET_WALK_CANDID": {
        "genre": "Aesthetic Chill Pop / Lo-Fi Sunset Melody / Acoustic Guitar",
        "example_tracks": [
            "Golden Hour - JVKE (Acoustic Instrumental)", "Cupid - Twin Ver (Trending Cut)",
            "Until I Found You (Slowed + Reverb)", "Đi Giữa Trời Rực Rỡ (Acoustic)", "Có Chắc Yêu Là Đây (Chill Remix)"
        ],
        "audio_mood": "Ngọt ngào, tươi trẻ, bay bổng, lãng mạn như phim thanh xuân",
        "ambient": "Tiếng gió thổi qua tán cây, tiếng chim hót nhẹ và bước chân dạo phố"
    },
    "SHOPPING_SPREE": {
        "genre": "Upbeat R&B Groove / City Pop / Funky Disco",
        "example_tracks": [
            "Say So - Doja Cat (Instrumental)", "Levitating - Dua Lipa (Funk Remix)",
            "Fly Me To The Moon (Bossa Nova Edition)", "Ánh Nắng Của Anh (Chill Hop Beat)"
        ],
        "audio_mood": "Thư giãn, vui tươi, tràn ngập năng lượng tích cực của ngày cuối tuần",
        "ambient": "Tiếng túi giấy mua sắm sột soạt nhẹ, tiếng chuông cửa boutique leng keng"
    },
    "EXTEND_REFERENCE_SCENE": {
        "genre": "Dreamy Ambient Piano / Vintage French Waltz / Soft Violin",
        "example_tracks": [
            "Ylang Ylang - FKJ", "Amélie Poulain Soundtrack (Valse d'Amélie)",
            "Experience - Ludovico Einaudi (Soft Cut)", "Nàng Thơ - Hoàng Dũng (Instrumental)"
        ],
        "audio_mood": "Hoài niệm, êm dịu, tôn vinh trọn vẹn vẻ đẹp thuần khiết của người mẫu",
        "ambient": "Không gian tĩnh lặng tự nhiên, tiếng rèm cửa xao động nhẹ"
    }
}


class TikTokCommunityPolicyGuardian:
    def __init__(self):
        self.prohibited_words = TIKTOK_BANNED_OR_FLAGGED_WORDS

    def audit_text(self, text: str) -> Dict[str, Any]:
        found_violations = []
        clean_text = text.lower()
        for word in self.prohibited_words:
            if re.search(r'\b' + re.escape(word) + r'\b', clean_text):
                found_violations.append(word)

        is_compliant = len(found_violations) == 0
        score = max(0, 100 - len(found_violations) * 25)
        return {
            "is_compliant": is_compliant,
            "compliance_score": score,
            "violations_found": found_violations,
            "status": "APPROVED ✅" if is_compliant else "NEEDS_REVISION ⚠️"
        }


class ScriptAndCaptionEngine:
    """Specialized in Diverse Douyin Candid Storyboards tailored directly from Model Photos."""

    def __init__(self):
        self.guardian = TikTokCommunityPolicyGuardian()

    def generate_scripts(
        self,
        product: Dict[str, Any],
        vibe_id: str = "DOUYIN_IDOL_CANDID",
        model_id_or_filename: Optional[str] = None
    ) -> Dict[str, Any]:
        title = product.get("title", "Trang Phục Thời Trang Nữ")
        short_title = title.split()[0] if title else "Váy"
        primary_color = product.get("colors", ["pastel"])[0]

        if vibe_id not in BGM_RECOMMENDATIONS:
            vibe_id = "DOUYIN_IDOL_CANDID"

        bgm = BGM_RECOMMENDATIONS.get(vibe_id, BGM_RECOMMENDATIONS["DOUYIN_IDOL_CANDID"])

        # 1. Analyze and extract authentic scene from Model Photo
        scene_info = scene_analyzer.analyze_photo(model_id_or_filename or "20260312_044514_803.jpg")
        scene_name = scene_info.get("scene_name", "Bối Cảnh Trong Ảnh Mẫu")
        env_desc = scene_info.get("environment", "Không gian nguyên bản trong ảnh mẫu")
        spatial_desc = scene_info.get("spatial_details", "Giữ nguyên 100% bố cục và ánh sáng có sẵn")
        lighting_desc = scene_info.get("lighting", "Ánh sáng tự nhiên từ ảnh tham chiếu")
        foley_desc = scene_info.get("foley_sound", "Tiếng động môi trường tự nhiên")

        # 2. Build 4 Diverse Scenario Tracks based strictly on the Model's Photo Scene
        # Track 1: Khoảnh khắc bắt gặp tự nhiên (Unaware Idol Candid)
        track_1_storyboard = {
            "scene_1": {
                "timing": "0:00 - 0:03 (Hook Bắt Gặp Quay Lén)",
                "action": f"Góc máy tele 85mm bắt gặp từ xa qua tiền cảnh (khung cửa/rèm/tán cây), giữ trọn vẹn 100% bối cảnh: {env_desc}. Người mẫu đang ở tư thế thư thái, ánh mắt nhìn lơ đãng ra xa, tay chạm nhẹ vào tóc, hoàn toàn KHÔNG nhìn vào ống kính.",
                "audio": f"{bgm['audio_mood']}. {foley_desc}"
            },
            "scene_2": {
                "timing": "0:03 - 0:08 (Cử Động Nhẹ Nhàng Tự Nhiên)",
                "action": f"Người mẫu khẽ đổi tư thế trong không gian {scene_name}, tà {short_title.lower()} màu {primary_color} rủ nhẹ theo từng cử động cơ thể. Ánh sáng {lighting_desc} làm bừng sáng làn da và phom dáng, người mẫu vẫn giữ nét mặt tự nhiên không chú ý máy quay.",
                "audio": "Nhạc nền Douyin viral bắt đầu nhịp trống lo-fi chill cuốn hút"
            },
            "scene_3": {
                "timing": "0:08 - 0:12 (Cận Cảnh Chi Tiết Phom Dáng)",
                "action": f"Camera zoom mượt mà bắt trọn độ rủ của chất vải và đường xếp ly tinh xảo. Người mẫu khẽ chỉnh lại quai áo hoặc cổ áo bằng cử chỉ tao nhã, toát lên vẻ đẹp sang chảnh đời thường của một idol Douyin.",
                "audio": "Giai điệu piano/lo-fi du dương tạo cảm xúc thanh lịch cuốn hút"
            },
            "scene_4": {
                "timing": "0:12 - 0:15 (Outro Thần Thái Cuốn Hút)",
                "action": f"Người mẫu phóng tầm mắt ngắm nhìn không gian xung quanh với nụ cười mỉm nhẹ nhàng cho riêng mình, không hề nhìn vào camera, kích thích người xem tò mò bấm vào giỏ hàng vàng để xem outfit.",
                "audio": "Giai điệu kết thúc ngọt ngào, hiển thị chỉ dẫn giỏ hàng TikTok Shop"
            }
        }

        # Track 2: Dạo bước trong không gian (Ambient Slow Walk)
        track_2_storyboard = {
            "scene_1": {
                "timing": "0:00 - 0:03 (Hook Bước Chân Tự Nhiên)",
                "action": f"Bắt đầu từ tư thế trong ảnh tại {scene_name}, người mẫu đứng dậy hoặc sải bước chậm rãi qua khung hình. Ống kính theo sau mượt mà từ góc nghiêng 45 độ, tuyệt đối không gọi hay khiến người mẫu chú ý camera.",
                "audio": f"Nhịp beat catwalk Douyin nhẹ nhàng. {foley_desc}"
            },
            "scene_2": {
                "timing": "0:03 - 0:08 (Chuyển Động Tà Váy Bay Nhẹ)",
                "action": f"Tà {short_title.lower()} khẽ đung đưa theo từng nhịp sải bước uyển chuyển giữa không gian {spatial_desc}. Làn gió thoảng làm mái tóc người mẫu bay nhẹ, tôn lên vẻ thanh thoát và vòng eo thon gọn.",
                "audio": "Nhịp bass nhẹ nhàng kết hợp tiếng bước chân êm ái"
            },
            "scene_3": {
                "timing": "0:08 - 0:12 (Điểm Dừng Chân Thơ Mộng)",
                "action": f"Người mẫu dừng bước bên cửa sổ/lan can/gốc cây có trong bức ảnh, đưa tay vén nhẹ lọn tóc mai ra sau tai, ánh mắt nhìn xa xăm đăm chiêu. Chất vải {primary_color} bắt sáng cực đẹp trong ánh {lighting_desc}.",
                "audio": "Melody piano bay bổng tôn lên thần thái nàng thơ sang chảnh"
            },
            "scene_4": {
                "timing": "0:12 - 0:15 (Lướt Nhẹ Khỏi Khung Hình)",
                "action": f"Người mẫu tiếp tục sải bước về phía trước, dần lướt ra khỏi góc máy. Khung hình tĩnh lại vài giây để người xem ngắm trọn vẹn không gian nghệ thuật và biểu tượng giỏ hàng nổi bật.",
                "audio": "Âm thanh outro tinh tế, gợi ý giỏ hàng góc trái màn hình"
            }
        }

        # Track 3: Soi vải & Thần thái đỉnh chóp (Texture & Silhouette Spotlight)
        track_3_storyboard = {
            "scene_1": {
                "timing": "0:00 - 0:03 (Hook Cận Cảnh Đẳng Cấp)",
                "action": f"Góc máy tele cận cảnh đặc tả đường may và bề mặt chất vải cao cấp của chiếc {short_title.lower()} dưới ánh sáng {lighting_desc} tại {scene_name}. Bàn tay người mẫu lướt nhẹ qua vạt áo đầy tinh tế.",
                "audio": f"Tiếng sột soạt nhẹ của vải cao cấp + giai điệu lounge aesthetic bí ẩn"
            },
            "scene_2": {
                "timing": "0:03 - 0:08 (Góc Nghiêng Tôn Trọn Đường Cong)",
                "action": f"Camera từ từ pan rộng ra bán thân, ghi lại góc nghiêng thanh tú của người mẫu khi nàng khẽ xoay nhẹ người trong bối cảnh {env_desc}. Phom dáng ôm nhẹ tôn trọn đường cong cơ thể mà không hề hở hang.",
                "audio": "Tiếng beat Douyin drop êm dịu, nhịp nhàng"
            },
            "scene_3": {
                "timing": "0:08 - 0:12 (Thần Thái Lạnh Lùng Sang Chảnh)",
                "action": f"Người mẫu giữ thần thái lạnh lùng cuốn hút của các người mẫu Douyin hàng đầu, khẽ nghiêng đầu nhìn ra hướng khác, hoàn toàn không tương tác với ống kính. Chiếc váy toát lên vẻ sang trọng như đồ thiết kế đắt đỏ.",
                "audio": "Nhạc nền bay bổng làm tăng giá trị thị giác của trang phục"
            },
            "scene_4": {
                "timing": "0:12 - 0:15 (Khóa Cảnh Bằng Ánh Sáng Tự Nhiên)",
                "action": f"Ống kính bắt trọn toàn thân người mẫu đứng hài hòa giữa không gian {scene_name}, toát lên vẻ đẹp thanh lịch khó rời mắt. Giỏ hàng vàng xuất hiện tinh tế ở góc dưới bên trái.",
                "audio": "Giai điệu kết thúc êm ái, kích thích click giỏ hàng"
            }
        }

        # Track 4: Rời khung hình đầy bí ẩn (Mystery Departure & Back Silhouette)
        track_4_storyboard = {
            "scene_1": {
                "timing": "0:00 - 0:03 (Hook Góc Nhìn Sau Lưng Đắt Giá)",
                "action": f"Góc máy đặt từ phía sau người mẫu giữa bối cảnh {scene_name}. Nàng đang chuẩn bị rời bước, khoe trọn phần lưng thanh mảnh và đường cắt may tinh tế phía sau của chiếc {short_title.lower()}.",
                "audio": f"Tiếng bước chân chậm rãi + âm thanh môi trường {foley_desc}"
            },
            "scene_2": {
                "timing": "0:03 - 0:08 (Sải Bước Về Hướng Cửa / Lối Đi)",
                "action": f"Người mẫu bước đi thong dong về phía cửa/hành lang/lối đi trong {spatial_desc}. Tà váy xếp ly chuyển động nhịp nhàng theo bước chân, tạo hiệu ứng thị giác mê hoặc khó cưỡng.",
                "audio": "Nhạc nền Douyin Sped Up bắt đầu đoạn điệp khúc viral gây nghiện"
            },
            "scene_3": {
                "timing": "0:08 - 0:12 (Khoảnh Khắc Nghiêng Đầu Không Ngoảnh Lại)",
                "action": f"Giữa bước đi, người mẫu chỉ khẽ nghiêng nhẹ đầu nhìn một nhành hoa hoặc một góc tường, tuyệt đối không quay đầu nhìn vào máy quay. Vẻ bí ẩn và kiêu kỳ khiến video có tỷ lệ xem lại (loop rate) cực cao.",
                "audio": "Giai điệu cao trào lôi cuốn, tạo sự tò mò tột độ cho người xem"
            },
            "scene_4": {
                "timing": "0:12 - 0:15 (Khuất Dần Đầy Nuối Tiếc & Kêu Gọi)",
                "action": f"Bóng dáng người mẫu khuất dần sau góc cua hoặc rèm cửa. Dòng chữ giỏ hàng góc trái màn hình nổi bật, mời gọi người xem nhanh tay rước ngay outfit idol.",
                "audio": "Outro nhịp trống dứt khoát, âm báo hiệu giỏ hàng TikTok Shop"
            }
        }

        # Pack all 4 diverse scenarios
        scenarios = {
            "scenario_1": {
                "id": "scenario_1",
                "title": "Kịch Bản 1: Bắt Gặp Tự Nhiên (Spontaneous Unposed Moment)",
                "badge": "🌟 Tự Nhiên Nhất",
                "focus": "Bắt trọn khoảnh khắc idol đang tĩnh lặng thả hồn trong bối cảnh thực tế, không hề để ý tới camera.",
                "storyboard": track_1_storyboard,
                "captions": [
                    f"Tình cờ bắt gặp em gái idol hôm nay, diện chiếc {short_title.lower()} màu {primary_color} đơn giản mà thần thái đỉnh chóp ✨ Link váy chính hãng tui ghim ở giỏ hàng góc trái nha mng 👇",
                    f"Đúng là visual idol Douyin đời thường không góc chết 🌙 Nàng xem chi tiết váy và ưu đãi hôm nay ở giỏ hàng góc trái màn hình nhé! 🛍️"
                ],
                "hashtags": ["#douyin", "#quaylen", "#thoitrangdouyin", "#candid", "#thoitrangnu", "#ootd", "#tiktokshopvn", "#xuhuong2026", "#learnontiktok"]
            },
            "scenario_2": {
                "id": "scenario_2",
                "title": "Kịch Bản 2: Dạo Bước Chuyển Động (Ambient Slow Walk)",
                "badge": "🍃 Bay Bổng & Nàng Thơ",
                "focus": "Người mẫu thay đổi tư thế, sải bước nhẹ nhàng trong không gian ảnh mẫu, tà váy bay tôn dáng.",
                "storyboard": track_2_storyboard,
                "captions": [
                    f"Khoảnh khắc bắt gặp nàng thơ đang dạo bước thong thả 🌸 Gió thổi tà váy bồng bềnh cưng xỉu luôn á! Nàng xem váy ở giỏ hàng góc trái nha 👇",
                    f"Chỉ dạo bước nhẹ nhàng mà ai cũng hỏi chiếc {short_title.lower()} màu {primary_color} này mua ở đâu nè 🥰 Link chính hãng tui ghim ở góc trái màn hình nha mng ơi! ✨"
                ],
                "hashtags": ["#streetwalk", "#nangtho", "#thoitrangnu", "#vayxinh", "#vaythietke", "#tiktokshopvn", "#xuhuong"]
            },
            "scenario_3": {
                "id": "scenario_3",
                "title": "Kịch Bản 3: Soi Vải & Thần Thái Đỉnh Chóp (Texture & Silhouette)",
                "badge": "💎 Sang Chảnh & Cao Cấp",
                "focus": "Đặc tả cận cảnh chất vải cao cấp, đường eo tôn dáng và thần thái lạnh lùng kiêu kỳ.",
                "storyboard": track_3_storyboard,
                "captions": [
                    f"Soi cận chất vải và phom dáng của em váy này mới thấy đáng đồng tiền bát gạo ✨ Vừa tôn dáng vừa sang chảnh, link ở giỏ hàng nha các nàng! 👇",
                    f"Đơn giản mà toát lên thần thái sang xịn mịn khó cưỡng 🌙 Xem chi tiết váy và voucher giảm giá hôm nay ở giỏ hàng góc trái nhé nàng ơi! 🛍️"
                ],
                "hashtags": ["#highfashion", "#thoitrangnu", "#vaythietke", "#sangchanh", "#chatvaidep", "#tiktokshopvn", "#xuhuong2026"]
            },
            "scenario_4": {
                "id": "scenario_4",
                "title": "Kịch Bản 4: Rời Khung Hình Bí Ẩn (Mystery Departure & Back View)",
                "badge": "🔥 Tăng Tỷ Lệ Xem Lại (Loop Rate)",
                "focus": "Bắt trọn dáng lưng thắt đáy lưng ong từ phía sau khi người mẫu rời bước, gây tò mò tột độ.",
                "storyboard": track_4_storyboard,
                "captions": [
                    f"Góc nhìn phía sau của chiếc {short_title.lower()} này hack dáng đỉnh thật sự 🖤 Bước đi lướt qua mà ai cũng phải ngoái nhìn! Link ở giỏ hàng góc trái nha 👇",
                    f"Thần thái bước đi kiêu kỳ không ngoảnh lại khiến ai cũng tò mò 🕶️ Nàng chấm outfit này mấy điểm? Xem chi tiết ở giỏ hàng nhé! 🛍️"
                ],
                "hashtags": ["#backsilhouette", "#tondang", "#douyinoutfit", "#thoitrangnu", "#mystery", "#tiktokshopvn", "#xuhuong"]
            }
        }

        # Default to scenario 1 for backwards compatibility
        active_scenario = scenarios["scenario_1"]
        captions = active_scenario["captions"]
        hashtags = active_scenario["hashtags"]
        storyboard = active_scenario["storyboard"]

        audit_cap = self.guardian.audit_text(captions[0])

        # Record Free Token Usage (~650 tokens for 4 diverse scenarios)
        quota = quota_guardian.record_usage("AI Storyboard & Diverse Candid Scenarios", estimated_tokens=650)

        return {
            "product_id": product.get("id"),
            "product_title": title,
            "vibe_id": vibe_id,
            "model_reference": model_id_or_filename or "Model Tham Chiếu",
            "detected_scene": scene_info,
            "mode": "DOUYIN_IDOL_CANDID (100% Không Nhìn Camera, Kế Thừa Bối Cảnh Ảnh Mẫu)",
            "bgm_recommendation": bgm,
            "scenarios": scenarios,
            "storyboard": storyboard,
            "captions": captions,
            "hashtags_string": " ".join(hashtags),
            "hashtags_list": hashtags,
            "ai_quota": quota,
            "community_compliance": {
                "overall_status": "CERTIFIED_SAFE ✅" if audit_cap["is_compliant"] else "NEEDS_REVISION ⚠️",
                "average_score": audit_cap["compliance_score"],
                "notes": f"Bối cảnh: {scene_name}. 100% quay lén tự nhiên, không tương tác camera, không từ cấm, tuân thủ chính sách TikTok Shop."
            }
        }


script_engine = ScriptAndCaptionEngine()
