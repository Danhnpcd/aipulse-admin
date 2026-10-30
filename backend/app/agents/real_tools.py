import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.business.workspace_builder import (
    MAX_FILE_BYTES,
    MAX_WORKSPACE_FILES,
    SUBPROCESS_TIMEOUT_SECONDS,
    resolve_jailed_workspace,
    safe_write_workspace_file,
    scan_workspace_for_secrets,
)
from app.core.config import SecretRedactingFilter, get_settings

logger = logging.getLogger("app.agents.real_tools")

FORBIDDEN_COMMAND_PATTERNS = [
    re.compile(r"\bformat\b", re.IGNORECASE),
    re.compile(r"\bdel\s+/[sS]", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"\bdrop\s+database\b", re.IGNORECASE),
    re.compile(r"\bshutdown\b", re.IGNORECASE),
]


class AgentRealTools:
    """
    Production-grade Tool Execution Sandbox for AI Agents:
    1. tool_write_workspace_file: Real code authoring in jailed workspaces
    2. tool_run_terminal_command: Safe isolated subprocess execution
    3. tool_run_isolated_tests: Real automated Pytest verification with self-correction feedback
    4. tool_fetch_web_data: Real web intelligence scraper for market research
    """

    @staticmethod
    def tool_write_workspace_file(slug: str, rel_path: str, content: str) -> dict[str, Any]:
        ws_dir = resolve_jailed_workspace(slug)
        result = safe_write_workspace_file(ws_dir, rel_path, content)
        clean_secrets, findings = scan_workspace_for_secrets(ws_dir)
        if not clean_secrets:
            # Rollback if secret detected
            (ws_dir / rel_path).unlink(missing_ok=True)
            raise ValueError(f"Bảo mật: Từ chối lưu vì phát hiện chuỗi khóa nhạy cảm: {findings}")
        return {
            "success": True,
            "workspace_slug": slug,
            "file": result,
            "message": f"Đã ghi thành công tệp thực tế '{rel_path}' ({result['bytes']} bytes) vào dự án.",
        }

    @staticmethod
    def tool_run_terminal_command(slug: str, command: list[str]) -> dict[str, Any]:
        ws_dir = resolve_jailed_workspace(slug)
        cmd_str = " ".join(command)

        for pat in FORBIDDEN_COMMAND_PATTERNS:
            if pat.search(cmd_str):
                raise ValueError(f"Lệnh nguy hiểm bị chặn bởi bộ lọc an toàn: {cmd_str}")

        clean_env = {
            k: v
            for k, v in os.environ.items()
            if not k.upper().endswith("_API_KEY")
            and not k.upper().endswith("_SECRET")
            and k not in ("JWT_SECRET_KEY", "CREDENTIAL_ENCRYPTION_KEY")
        }
        clean_env["PYTHONPATH"] = str(ws_dir)
        clean_env["PYTHONDONTWRITEBYTECODE"] = "1"

        try:
            proc = subprocess.run(
                command,
                cwd=str(ws_dir),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
            raw = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
            sanitized = SecretRedactingFilter.sanitize_text(raw)[-3000:]
            return {
                "success": proc.returncode == 0,
                "exit_code": proc.returncode,
                "output": sanitized,
                "command": cmd_str,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "exit_code": -1,
                "output": f"Lệnh vượt quá thời gian thực thi an toàn ({SUBPROCESS_TIMEOUT_SECONDS}s).",
                "command": cmd_str,
            }
        except Exception as exc:
            return {"success": False, "exit_code": -2, "output": str(exc), "command": cmd_str}

    @staticmethod
    def tool_run_isolated_tests(slug: str) -> dict[str, Any]:
        """Runs real pytest in the workspace and extracts test metrics."""
        ws_dir = resolve_jailed_workspace(slug)
        res = AgentRealTools.tool_run_terminal_command(
            slug, [sys.executable, "-m", "pytest", "tests", "-v", "--tb=short"]
        )
        out = res.get("output", "")
        passed_m = re.search(r"(\d+)\s+passed", out)
        failed_m = re.search(r"(\d+)\s+failed", out)
        passed_cnt = int(passed_m.group(1)) if passed_m else 0
        failed_cnt = int(failed_m.group(1)) if failed_m else 0
        all_passed = (res.get("exit_code") == 0) and passed_cnt > 0 and failed_cnt == 0

        return {
            "all_passed": all_passed,
            "passed_count": passed_cnt,
            "failed_count": failed_cnt,
            "terminal_output": out[-2000:],
            "tested_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def tool_fetch_web_data(url: str, max_chars: int = 4000) -> dict[str, Any]:
        """Fetches real web intelligence for Business Researcher & Marketing agents."""
        clean_url = url.strip()
        if not clean_url.startswith(("http://", "https://")):
            raise ValueError("Chỉ hỗ trợ giao thức HTTP/HTTPS")

        headers = {"User-Agent": "AIPulse-MarketResearcher/2.0"}
        req = urllib.request.Request(clean_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw_bytes = resp.read(max_chars * 4)
                text = raw_bytes.decode("utf-8", errors="ignore")
                clean_text = re.sub(r"<[^>]+>", " ", text)
                clean_text = re.sub(r"\s+", " ", clean_text).strip()[:max_chars]
                return {"success": True, "url": clean_url, "content": clean_text}
        except Exception as exc:
            return {"success": False, "url": clean_url, "error": str(exc)}
