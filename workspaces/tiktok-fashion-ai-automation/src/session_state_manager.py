#!/usr/bin/env python3
"""
Session State Manager — Persists TikTok Fashion Studio state (selected dress,
selected model, generated try-on card, storyboards, captions) to both
local disk and Google Drive (danhnpcd@gmail.com) so closing/refreshing
the browser never loses any work.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

import os as _os
DRIVE_DIR = Path(r"G:\My Drive\TikTok_Fashion_AI_Studio\04_Bao_Cao_Du_An") if (_os.name == "nt" and Path("G:/My Drive").exists()) else Path("/nonexistent/gdrive_reports")
LOCAL_DIR = Path(__file__).resolve().parent.parent / "config"
LOCAL_DIR.mkdir(parents=True, exist_ok=True)

DRIVE_STATE_FILE = DRIVE_DIR / "studio_session_state.json"
LOCAL_STATE_FILE = LOCAL_DIR / "studio_session_state.json"


class SessionStateManager:
    """Synchronizes and persists the active state of TikTok Fashion AI Studio."""

    def __init__(self):
        self.state = self.load_state()

    def load_state(self) -> Dict[str, Any]:
        """Loads state from Google Drive or local cache."""
        for target in [DRIVE_STATE_FILE, LOCAL_STATE_FILE]:
            if target.exists():
                try:
                    with open(target, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            return data
                except Exception:
                    pass

        # Default initial state
        return {
            "selected_product_id": "TREND-FASH-001",
            "selected_product": None,
            "selected_model_filename": "20260312_044514_803.jpg",
            "selected_model_url": "/api/tiktok-fashion/models/view/20260312_044514_803.jpg",
            "selected_model_name": "Người Mẫu #514_803",
            "current_vibe": "DOUYIN_IDOL_CANDID",
            "current_setting": "ORIGINAL_SCENE",
            "active_scenario_id": "scenario_1",
            "active_tab_id": "tab-dressing",
            "last_tryon_preview_url": None,
            "last_virtual_tryon_prompt": None,
            "last_master_prompt": None,
            "last_storyboard_data": None,
            "updated_at": datetime.now().isoformat()
        }

    def save_state(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Merges updates and writes to both Google Drive and local disk."""
        self.state.update(updates)
        self.state["updated_at"] = datetime.now().isoformat()

        # 1. Save local
        try:
            with open(LOCAL_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving local session state: {e}")

        # 2. Save to Google Drive
        try:
            if DRIVE_DIR.exists():
                with open(DRIVE_STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error syncing session state to Google Drive: {e}")

        return self.state


session_manager = SessionStateManager()
