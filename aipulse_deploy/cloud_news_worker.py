#!/usr/bin/env python3
"""
Standalone Cloud 24/7 Autonomous AI News Publisher for GitHub Actions + GitHub Pages.
Runs every 30 minutes directly on GitHub Cloud Runners ($0 cost, zero local PC required).
"""

import datetime
import json
import random
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "index.html"
FALLBACK_200 = ROOT / "200.html"

FEEDS = [
    {
        "url": "https://news.google.com/rss/search?q=AI+tr%C3%AD+tu%E1%BB%87+nh%C3%A2n+t%E1%BA%A1o+Vi%E1%BB%87t+Nam&hl=vi&gl=VN&ceid=VN:vi",
        "source": "Google News AI",
    },
    {"url": "https://vnexpress.net/rss/so-hoa.rss", "source": "VNExpress Số Hóa"},
    {
        "url": "https://news.google.com/rss/search?q=c%C3%B4ng+ngh%E1%BB%87+ph%E1%BA%A7n+m%E1%BB%81m+startup&hl=vi&gl=VN&ceid=VN:vi",
        "source": "Google News Công Nghệ",
    },
    {"url": "https://thanhnien.vn/rss/cong-nghe.rss", "source": "Thanh Niên Công Nghệ"},
]

TECH_IMAGES = [
    "https://images.unsplash.com/photo-1486406146926-c627a92ad1ab?auto=format&fit=crop&w=900&q=80",
    "https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=900&q=80",
    "https://images.unsplash.com/photo-1526374965328-7f61d4dc18c5?auto=format&fit=crop&w=900&q=80",
    "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?auto=format&fit=crop&w=900&q=80",
    "https://images.unsplash.com/photo-1511578314322-379afb476865?auto=format&fit=crop&w=900&q=80",
    "https://images.unsplash.com/photo-1550751827-4bd374c3f58b?auto=format&fit=crop&w=900&q=80",
]


def categorize_article(title: str, snippet: str) -> tuple[str, str, str]:
    text = (title + " " + snippet).lower()
    if any(k in text for k in ["kiếm tiền", "mmo", "doanh thu", "quảng cáo", "kinh doanh", "thương mại", "chi phí", "giá"]):
        return "MMO", "KINH DOANH SỐ & MMO", "📈"
    if any(k in text for k in ["chip", "apple", "macbook", "điện thoại", "thiết bị", "laptop", "phần cứng", "camera"]):
        return "TOOLS", "THIẾT BỊ & PHẦN CỨNG", "💻"
    if any(k in text for k in ["công cụ", "tool", "prompt", "chatgpt", "gemini", "claude", "mẹo", "hướng dẫn"]):
        return "PROMPT", "PROMPT & CÔNG CỤ", "⚡"
    return "AI", "TRÍ TUỆ NHÂN TẠO", "🤖"


def fetch_candidates() -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 AI-Pulse-Cloud/2.0"}
    candidates = []
    for f in FEEDS:
        try:
            req = urllib.request.Request(f["url"], headers=headers)
            with urllib.request.urlopen(req, timeout=12) as resp:
                xml_data = resp.read()
            root = ET.fromstring(xml_data)
            for it in root.findall(".//item")[:8]:
                title = (it.findtext("title") or "").split(" - ")[0].strip()
                link = (it.findtext("link") or "").strip()
                desc = re.sub(r"<[^>]+>", "", (it.findtext("description") or "")).strip()
                if len(title) > 15 and link:
                    candidates.append(
                        {"title": title, "source": f["source"], "source_url": link, "snippet": desc or title}
                    )
        except Exception as exc:
            print(f"Feed warning ({f['source']}): {exc}")
    return candidates


def main() -> None:
    if not INDEX_HTML.exists():
        print("index.html not found.")
        return

    html_text = INDEX_HTML.read_text(encoding="utf-8")
    candidates = fetch_candidates()
    chosen = next((c for c in candidates if c["title"] not in html_text), None)
    if not chosen:
        print("No unseen articles at this tick.")
        return

    cat_code, cat_label, cat_icon = categorize_article(chosen["title"], chosen["snippet"])
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_vn = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=7)))
    article_id = int(now_utc.timestamp()) % 100000

    new_art = {
        "id": article_id,
        "title": chosen["title"],
        "category": cat_code,
        "categoryLabel": cat_label,
        "snippet": (chosen["snippet"][:220] + "...") if len(chosen["snippet"]) > 220 else chosen["snippet"],
        "readTime": "3 phút",
        "author": f"Ban Biên Tập AI Pulse Cloud (Theo {chosen['source']})",
        "views": 0,
        "likes": random.randint(45, 115),
        "icon": cat_icon,
        "source": chosen["source"],
        "source_url": chosen["source_url"],
        "content": (
            f"<p><strong>Bản tin cập nhật tự động 24/7 từ {chosen['source']}:</strong> {chosen['snippet']}</p>"
            f"<h2>1. Bối Cảnh & Diễn Biến Sự Kiện</h2>"
            f"<p>Theo ghi nhận từ nguồn tin chính thống <em>{chosen['source']}</em>, chủ đề '{chosen['title']}' "
            f"đang thu hút sự quan tâm lớn trong cộng đồng công nghệ và AI tại Việt Nam.</p>"
            f"<h2>2. Tác Động & Góc Nhìn Ứng Dụng</h2>"
            f"<p>Xu hướng này mở ra cơ hội tối ưu hóa vận hành số và nâng cao năng suất tự động hóa cho cá nhân lẫn doanh nghiệp.</p>"
            f"<p style='margin-top:1.25rem;font-size:12px;color:#94a3b8;border-top:1px dashed #334155;padding-top:10px;'>"
            f"📌 <em>Nguồn tin xác thực:</em> <strong>{chosen['source']}</strong> — "
            f"<a href='{chosen['source_url']}' target='_blank' rel='noreferrer' style='color:#38bdf8;text-decoration:underline;'>"
            f"Đọc bài gốc trên {chosen['source']} ↗</a></p>"
        ),
        "created_at": now_utc.isoformat(),
        "publish_time": now_vn.strftime("%H:%M • %d/%m/%Y"),
        "has_infographic": True,
        "image_url": random.choice(TECH_IMAGES),
    }

    marker = "var ARTICLES = ["
    idx = html_text.find(marker)
    if idx == -1:
        print("Marker var ARTICLES = [ not found.")
        return

    ins = idx + len(marker)
    updated = html_text[:ins] + "\n  " + json.dumps(new_art, ensure_ascii=False, indent=2) + "," + html_text[ins:]
    INDEX_HTML.write_text(updated, encoding="utf-8")
    FALLBACK_200.write_text(updated, encoding="utf-8")
    print(f"Published Cloud article #{article_id}: {chosen['title']}")


if __name__ == "__main__":
    main()
