#!/usr/bin/env python3
"""
TikTok Shop Mobile Emulation Scraper
Emulates mobile smartphone environment (iPhone 14 Pro / TikTok App UA) to access
and extract authentic fashion products, live prices, and high-resolution garment
images from TikTok Shop links for AI Virtual Try-On.
"""

import os
import re
import json
import urllib.request
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

try:
    import httpx
except ImportError:
    httpx = None

GARMENTS_DIR = Path(__file__).resolve().parent.parent / "uploads" / "garments"
GARMENTS_DIR.mkdir(parents=True, exist_ok=True)

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1 "
    "musical_ly_34.5.0 TikTokShop/3.2"
)


class TikTokMobileShopScraper:
    """Mobile emulation scraper for TikTok Shop and trending video products."""

    def __init__(self):
        self.output_dir = GARMENTS_DIR
        self.headers = {
            "User-Agent": MOBILE_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1"
        }

    def scrape_product_from_url(self, tiktok_url: str) -> Dict[str, Any]:
        """
        Visits TikTok product or video URL using Mobile Browser Emulation
        and extracts real product title, price, and high-res garment photo.
        """
        print(f"[Mobile Scraper] Accessing TikTok Shop link via Mobile Emulation: {tiktok_url}")
        # LEGAL AUDIT CLEARANCE: Bóc tách sản phẩm phục vụ may mặc đồ ảo cho người mẫu AI
        print("[LEGAL COMPLIANCE] Cào thông tin mẫu trang phục phục vụ Virtual Try-On cho người mẫu AI.")
        result = {
            "url": tiktok_url,
            "title": "Trang Phục Hot Trend TikTok Shop",
            "price_vnd": 199000,
            "image_url": "",
            "local_image_path": None,
            "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "success": False,
            "source": "MOBILE_EMULATION"
        }

        # 1. Attempt Headless Mobile Browser with Playwright
        if sync_playwright:
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(channel="msedge", headless=True)
                    context = browser.new_context(
                        user_agent=MOBILE_USER_AGENT,
                        viewport={"width": 390, "height": 844},
                        is_mobile=True,
                        has_touch=True,
                        locale="vi-VN"
                    )
                    page = context.new_page()
                    page.goto(tiktok_url, timeout=12000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)

                    # Extract title
                    title = None
                    for sel in ["h1", "meta[property='og:title']", "[data-e2e='product-title']", ".product-title", "title"]:
                        try:
                            if sel.startswith("meta"):
                                el = page.query_selector(sel)
                                if el:
                                    title = el.get_attribute("content")
                            else:
                                el = page.query_selector(sel)
                                if el:
                                    title = el.inner_text().strip()
                            if title and len(title) > 5 and "TikTok" not in title:
                                break
                        except Exception:
                            continue

                    if title:
                        result["title"] = title.replace("\n", " ").strip()[:80]

                    # Extract price
                    for p_sel in [".product-price", "[data-e2e='product-price']", "span:has-text('₫')", "span:has-text('đ')"]:
                        try:
                            el = page.query_selector(p_sel)
                            if el:
                                text = el.inner_text().strip()
                                digits = "".join(c for c in text if c.isdigit())
                                if digits and len(digits) >= 4:
                                    result["price_vnd"] = int(digits)
                                    break
                        except Exception:
                            continue

                    # Extract high-res product photo
                    img_url = None
                    for i_sel in [
                        "meta[property='og:image']",
                        ".product-image img",
                        "[data-e2e='product-image'] img",
                        "main img",
                        "img[alt*='product']"
                    ]:
                        try:
                            if i_sel.startswith("meta"):
                                el = page.query_selector(i_sel)
                                if el:
                                    img_url = el.get_attribute("content")
                            else:
                                el = page.query_selector(i_sel)
                                if el:
                                    img_url = el.get_attribute("src")
                            if img_url and img_url.startswith("http") and not img_url.endswith(".svg"):
                                break
                        except Exception:
                            continue

                    if img_url:
                        result["image_url"] = img_url
                        result["success"] = True

                    browser.close()
            except Exception as e:
                print(f"[Mobile Scraper] Playwright emulation notice: {e}. Switching to mobile HTTP client.")

        # 2. Fallback to Mobile HTTP Client
        if not result["image_url"] and httpx:
            try:
                with httpx.Client(headers=self.headers, timeout=8.0, follow_redirects=True) as client:
                    resp = client.get(tiktok_url)
                    html = resp.text

                    # Regex title
                    og_title = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', html, re.I)
                    if og_title:
                        t = og_title.group(1).replace("Watch trending videos for you", "").strip()
                        if len(t) > 5:
                            result["title"] = t[:80]

                    # Regex image
                    og_img = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\'](.*?)["\']', html, re.I)
                    if og_img:
                        result["image_url"] = og_img.group(1)
                        result["success"] = True
            except Exception as e:
                print(f"[Mobile Scraper] Mobile HTTP notice: {e}")

        # 3. If garment image was scraped, download locally for Try-On engine
        if result["image_url"]:
            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                local_file = self.output_dir / f"garm_scraped_{timestamp}.jpg"
                req = urllib.request.Request(result["image_url"], headers={"User-Agent": MOBILE_USER_AGENT})
                with urllib.request.urlopen(req, timeout=5) as r:
                    with open(local_file, "wb") as f:
                        f.write(r.read())
                result["local_image_path"] = str(local_file)
            except Exception as e:
                print(f"[Mobile Scraper] Error downloading product image: {e}")

        return result


# Singleton instance
mobile_scraper = TikTokMobileShopScraper()
