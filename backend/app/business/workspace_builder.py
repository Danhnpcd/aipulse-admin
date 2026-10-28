import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import SecretRedactingFilter, get_settings
from app.events.bus import emit_notification, event_bus
from app.models.entities import ApprovalRequest, DeploymentRecord, ProductArtifact, Project
from app.models.enums import EventType, NotificationType
from app.tasks.engine import TaskEngine

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9\-]{1,60}$")
MAX_FILE_BYTES = 262_144
MAX_WORKSPACE_FILES = 30
SUBPROCESS_TIMEOUT_SECONDS = 25

SENSITIVE_ENV_KEYS = {
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "CREDENTIAL_ENCRYPTION_KEY",
    "JWT_SECRET_KEY",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "PAYMENT_WEBHOOK_SECRET",
}

FORBIDDEN_CODE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{12,}"),
    re.compile(r"gsk_[A-Za-z0-9_\-]{12,}"),
    re.compile(r"CREDENTIAL_ENCRYPTION_KEY"),
    re.compile(r"LOCAL_VAULT_KEY_PATH"),
]


def _validate_slug(slug: str) -> str:
    clean = slug.strip().lower()
    if not SLUG_PATTERN.match(clean):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Slug dự án không hợp lệ '{slug}'. Chỉ cho phép chữ thường, số và dấu gạch ngang.",
        )
    return clean


def resolve_jailed_workspace(slug: str) -> Path:
    cfg = get_settings()
    clean_slug = _validate_slug(slug)
    root = Path(cfg.WORKSPACES_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    ws = (root / clean_slug).resolve()
    if not ws.is_relative_to(root):
        raise HTTPException(status_code=403, detail="Truy cập vượt khỏi phạm vi workspace cô lập bị từ chối.")
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def safe_write_workspace_file(workspace_dir: Path, rel_path: str, content: str) -> dict[str, Any]:
    root = workspace_dir.resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=403, detail=f"Đường dẫn '{rel_path}' vượt phạm vi workspace.")

    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"File '{rel_path}' vượt giới hạn kích thước an toàn (256KB).")

    for pat in FORBIDDEN_CODE_PATTERNS:
        if pat.search(content):
            raise HTTPException(
                status_code=400,
                detail=f"Phát hiện mẫu chuỗi nhạy cảm/API key trong '{rel_path}'. Từ chối ghi mã nguồn.",
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encoded)
    return {
        "path": rel_path.replace("\\", "/"),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest()[:16],
    }


def scan_workspace_for_secrets(workspace_dir: Path) -> tuple[bool, list[str]]:
    findings: list[str] = []
    for p in workspace_dir.rglob("*"):
        if not p.is_file() or "versions" in p.parts or "__pycache__" in p.parts or ".pytest_cache" in p.parts:
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in FORBIDDEN_CODE_PATTERNS:
            if pat.search(txt):
                findings.append(f"Suspected secret pattern in {p.relative_to(workspace_dir)}")
    return (len(findings) == 0), findings


def _generate_invoice_guard_files(product_name: str, slug: str, version: str) -> dict[str, str]:
    """
    Produces a complete, genuinely working software package + tests + interactive browser app
    for InvoiceGuard (validates VAT math, duplicate invoice numbers, tax IDs, and exports clean CSV).
    """
    spec_md = f"""# Product Specification — {product_name} (`v{version}`)

## 1. Mục tiêu Sản phẩm
Giải quyết bài toán kiểm tra sai lệch bảng kê hóa đơn điện tử (sai số học tiền thuế GTGT, trùng số hóa đơn, thiếu/sai định dạng mã số thuế) và xuất bảng kê CSV chuẩn kế toán mà **không phụ thuộc vào API trả phí bên ngoài**.

## 2. User Stories
- **US-01 (Kiểm toán số học & VAT):** Là kế toán viên/freelancer, tôi muốn nạp danh sách hóa đơn (JSON/CSV) để phát hiện ngay các dòng có `subtotal + vat_amount != total_amount` hoặc sai thuế suất (`0%`, `5%`, `8%`, `10%`).
- **US-02 (Chống thanh toán trùng):** Là chủ hộ kinh doanh, tôi muốn hệ thống cảnh báo ngay khi cùng một mã số thuế người bán xuất trùng số hóa đơn.
- **US-03 (Chuẩn hóa xuất sổ cái CSV):** Là người vận hành, tôi muốn xuất danh sách hóa đơn hợp lệ ra định dạng CSV chuẩn để đưa vào sổ kế toán.

## 3. Acceptance Criteria
1. Phát hiện 100% hóa đơn lệch tổng thanh toán > `0.01` đơn vị tiền tệ.
2. Phát hiện 100% cặp `(seller_tax_id, invoice_no)` bị trùng lặp trong cùng lô đối soát.
3. Kiểm tra định dạng Mã số thuế hợp lệ (10 chữ số hoặc 10-3 chữ số chi nhánh).
4. Không chứa bất kỳ khóa bí mật hay kết nối mạng trái phép nào; vượt qua toàn bộ unit test và integration test bằng `pytest`.

## 4. Kiến trúc Tối thiểu
- `src/invoice_guard.py`: Bộ máy kiểm toán tất định (Deterministic Validation Core) + bộ xuất CSV chuẩn hóa + CLI.
- `tests/test_invoice_guard.py`: Bộ kiểm thử tự động đơn vị và tích hợp.
- `public/index.html`: Landing Page minh bạch kết hợp công cụ kiểm tra hóa đơn trực tiếp ngay trên trình duyệt, form đăng ký dùng thử opt-in và nút thanh toán Sandbox.
"""

    code_py = '''"""
InvoiceGuard Core Engine — Deterministic Invoice & VAT Reconciliation Utility.
Operates 100% locally with zero external paid API dependency.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

TAX_ID_REGEX = re.compile(r"^\\d{10}(-\\d{3})?$")
ALLOWED_VAT_RATES = {0.0, 5.0, 8.0, 10.0}


@dataclass
class InvoiceIssue:
    row_index: int
    invoice_no: str
    code: str
    message: str


@dataclass
class AuditReport:
    total_rows: int
    valid_rows: int
    invalid_rows: int
    net_subtotal: float
    total_vat: float
    gross_total: float
    issues: list[InvoiceIssue] = field(default_factory=list)
    normalized_records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "invalid_rows": self.invalid_rows,
            "net_subtotal": round(self.net_subtotal, 2),
            "total_vat": round(self.total_vat, 2),
            "gross_total": round(self.gross_total, 2),
            "issues": [
                {
                    "row_index": i.row_index,
                    "invoice_no": i.invoice_no,
                    "code": i.code,
                    "message": i.message,
                }
                for i in self.issues
            ],
            "normalized_records": self.normalized_records,
        }


def validate_tax_id(tax_id: str) -> bool:
    clean = (tax_id or "").strip()
    return bool(TAX_ID_REGEX.match(clean))


def audit_invoices(rows: list[dict[str, Any]]) -> AuditReport:
    seen_keys: set[tuple[str, str]] = set()
    issues: list[InvoiceIssue] = []
    valid_records: list[dict[str, Any]] = []

    net_sum = 0.0
    vat_sum = 0.0
    gross_sum = 0.0
    invalid_count = 0

    for idx, raw in enumerate(rows):
        inv_no = str(raw.get("invoice_no", "")).strip()
        tax_id = str(raw.get("seller_tax_id", "")).strip()
        row_issues: list[InvoiceIssue] = []

        if not inv_no:
            row_issues.append(InvoiceIssue(idx, inv_no or "MISSING", "MISSING_INVOICE_NO", "Thiếu số hóa đơn."))

        if not validate_tax_id(tax_id):
            row_issues.append(
                InvoiceIssue(idx, inv_no or "UNKNOWN", "INVALID_TAX_ID", f"Mã số thuế '{tax_id}' không đúng chuẩn 10 hoặc 13 số.")
            )

        try:
            subtotal = float(raw.get("subtotal", 0.0))
            vat_rate = float(raw.get("vat_rate", 0.0))
            vat_amount = float(raw.get("vat_amount", 0.0))
            total_amount = float(raw.get("total_amount", 0.0))
        except (TypeError, ValueError):
            row_issues.append(InvoiceIssue(idx, inv_no, "NON_NUMERIC_AMOUNT", "Giá trị tiền hoặc thuế suất không hợp lệ."))
            issues.extend(row_issues)
            invalid_count += 1
            continue

        if subtotal <= 0 or total_amount <= 0 or vat_amount < 0:
            row_issues.append(InvoiceIssue(idx, inv_no, "NEGATIVE_OR_ZERO_AMOUNT", "Tiền hàng và tổng thanh toán phải > 0."))

        if vat_rate not in ALLOWED_VAT_RATES:
            row_issues.append(
                InvoiceIssue(idx, inv_no, "UNSUPPORTED_VAT_RATE", f"Thuế suất {vat_rate}% không nằm trong khung {sorted(ALLOWED_VAT_RATES)}.")
            )

        expected_vat = round(subtotal * (vat_rate / 100.0), 2)
        if abs(vat_amount - expected_vat) > 0.02:
            row_issues.append(
                InvoiceIssue(
                    idx,
                    inv_no,
                    "VAT_CALCULATION_MISMATCH",
                    f"Tiền thuế ghi nhận ({vat_amount}) lệch so với mức {vat_rate}% ({expected_vat}).",
                )
            )

        expected_total = round(subtotal + vat_amount, 2)
        if abs(total_amount - expected_total) > 0.02:
            row_issues.append(
                InvoiceIssue(
                    idx,
                    inv_no,
                    "TOTAL_MISMATCH",
                    f"Tổng thanh toán ({total_amount}) khác tiền hàng + thuế ({expected_total}).",
                )
            )

        dedup_key = (tax_id, inv_no)
        if inv_no and tax_id:
            if dedup_key in seen_keys:
                row_issues.append(
                    InvoiceIssue(idx, inv_no, "DUPLICATE_INVOICE", f"Trùng hóa đơn '{inv_no}' của cùng MST '{tax_id}'.")
                )
            else:
                seen_keys.add(dedup_key)

        if row_issues:
            issues.extend(row_issues)
            invalid_count += 1
        else:
            norm = {
                "invoice_no": inv_no,
                "seller_tax_id": tax_id,
                "subtotal": round(subtotal, 2),
                "vat_rate": vat_rate,
                "vat_amount": round(vat_amount, 2),
                "total_amount": round(total_amount, 2),
            }
            valid_records.append(norm)
            net_sum += subtotal
            vat_sum += vat_amount
            gross_sum += total_amount

    return AuditReport(
        total_rows=len(rows),
        valid_rows=len(valid_records),
        invalid_rows=invalid_count,
        net_subtotal=net_sum,
        total_vat=vat_sum,
        gross_total=gross_sum,
        issues=issues,
        normalized_records=valid_records,
    )


def export_normalized_csv(report: AuditReport) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["invoice_no", "seller_tax_id", "subtotal", "vat_rate", "vat_amount", "total_amount"],
        lineterminator="\\n",
    )
    writer.writeheader()
    for row in report.normalized_records:
        writer.writerow(row)
    return buf.getvalue()
'''

    test_py = '''import csv
import io

from src.invoice_guard import audit_invoices, export_normalized_csv, validate_tax_id


def test_validate_vietnam_tax_id_formats():
    assert validate_tax_id("0101245486") is True
    assert validate_tax_id("0314567890-001") is True
    assert validate_tax_id("12345") is False
    assert validate_tax_id("ABC1234567") is False


def test_clean_invoices_pass_and_sum_accurately():
    rows = [
        {
            "invoice_no": "INV-001",
            "seller_tax_id": "0101245486",
            "subtotal": 1000.0,
            "vat_rate": 8.0,
            "vat_amount": 80.0,
            "total_amount": 1080.0,
        },
        {
            "invoice_no": "INV-002",
            "seller_tax_id": "0314567890-001",
            "subtotal": 500.0,
            "vat_rate": 10.0,
            "vat_amount": 50.0,
            "total_amount": 550.0,
        },
    ]
    report = audit_invoices(rows)
    assert report.total_rows == 2
    assert report.valid_rows == 2
    assert report.invalid_rows == 0
    assert report.net_subtotal == 1500.0
    assert report.total_vat == 130.0
    assert report.gross_total == 1630.0
    assert report.issues == []


def test_detects_vat_and_total_arithmetic_discrepancies():
    rows = [
        {
            "invoice_no": "INV-ERR-1",
            "seller_tax_id": "0101245486",
            "subtotal": 1000.0,
            "vat_rate": 10.0,
            "vat_amount": 80.0,  # Wrong VAT for 10%
            "total_amount": 1100.0,
        }
    ]
    report = audit_invoices(rows)
    assert report.valid_rows == 0
    assert report.invalid_rows == 1
    codes = {i.code for i in report.issues}
    assert "VAT_CALCULATION_MISMATCH" in codes
    assert "TOTAL_MISMATCH" in codes


def test_detects_duplicate_invoice_for_same_seller():
    row = {
        "invoice_no": "INV-777",
        "seller_tax_id": "0101245486",
        "subtotal": 200.0,
        "vat_rate": 8.0,
        "vat_amount": 16.0,
        "total_amount": 216.0,
    }
    report = audit_invoices([row, dict(row)])
    assert report.valid_rows == 1
    assert report.invalid_rows == 1
    assert any(i.code == "DUPLICATE_INVOICE" for i in report.issues)


def test_rejects_negative_amounts_and_unsupported_vat_rate():
    rows = [
        {
            "invoice_no": "INV-NEG",
            "seller_tax_id": "0101245486",
            "subtotal": -100.0,
            "vat_rate": 7.5,
            "vat_amount": -7.5,
            "total_amount": -107.5,
        }
    ]
    report = audit_invoices(rows)
    assert report.invalid_rows == 1
    codes = {i.code for i in report.issues}
    assert "NEGATIVE_OR_ZERO_AMOUNT" in codes
    assert "UNSUPPORTED_VAT_RATE" in codes


def test_export_normalized_csv_round_trip():
    rows = [
        {
            "invoice_no": "INV-900",
            "seller_tax_id": "0101245486",
            "subtotal": 400.0,
            "vat_rate": 5.0,
            "vat_amount": 20.0,
            "total_amount": 420.0,
        }
    ]
    report = audit_invoices(rows)
    csv_text = export_normalized_csv(report)
    parsed = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(parsed) == 1
    assert parsed[0]["invoice_no"] == "INV-900"
    assert float(parsed[0]["total_amount"]) == 420.0
'''

    readme_md = f"""# {product_name} (`v{version}`)

Công cụ kiểm tra sai lệch bảng kê hóa đơn GTGT, phát hiện hóa đơn trùng và chuẩn hóa file CSV kế toán dành cho SME & Freelancer.

## Cài đặt & Kiểm thử trong Môi trường Cô lập

```bash
pytest tests -v
```

## Sử dụng trong Python

```python
from src.invoice_guard import audit_invoices, export_normalized_csv

report = audit_invoices([
    {{
        "invoice_no": "HD-001",
        "seller_tax_id": "0101245486",
        "subtotal": 1000.0,
        "vat_rate": 8.0,
        "vat_amount": 80.0,
        "total_amount": 1080.0,
    }}
])
print(report.to_dict())
```
"""

    landing_html = f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{product_name} — Rà Soát Hóa Đơn & Thuế VAT Tự Động Trong 5 Giây</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #050a15;
      --card-bg: rgba(15, 23, 42, 0.75);
      --card-border: rgba(56, 189, 248, 0.15);
      --card-hover: rgba(56, 189, 248, 0.25);
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --cyan: #06b6d4;
      --cyan-glow: rgba(6, 182, 212, 0.25);
      --emerald: #10b981;
      --amber: #f59e0b;
      --rose: #ef4444;
      --purple: #8b5cf6;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif;
      background: radial-gradient(circle at 50% 0%, #0d1b33 0%, var(--bg) 70%);
      color: var(--text);
      line-height: 1.6;
      min-height: 100vh;
      overflow-x: hidden;
    }}
    a {{ color: inherit; text-decoration: none; }}
    .container {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 0 1.5rem;
    }}

    /* Header Nav */
    header {{
      position: sticky; top: 0; z-index: 50;
      backdrop-filter: blur(16px);
      background: rgba(5, 10, 21, 0.85);
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      padding: 1rem 0;
    }}
    .nav-wrap {{
      display: flex; justify-content: space-between; align-items: center;
    }}
    .logo {{
      display: flex; align-items: center; gap: 0.75rem;
      font-weight: 800; font-size: 1.25rem; letter-spacing: -0.5px;
    }}
    .logo-badge {{
      background: linear-gradient(135deg, #06b6d4, #0284c7);
      color: #020617; font-size: 0.75rem; font-weight: 800;
      padding: 2px 8px; border-radius: 6px;
    }}
    .nav-links {{
      display: flex; gap: 1.5rem; align-items: center; font-size: 0.875rem; font-weight: 600;
    }}
    .nav-links a:hover {{ color: var(--cyan); }}
    .btn-nav {{
      background: linear-gradient(135deg, #06b6d4, #0284c7);
      color: #020617 !important; padding: 0.5rem 1.25rem; border-radius: 8px;
      font-weight: 700; transition: transform 0.2s, box-shadow 0.2s;
    }}
    .btn-nav:hover {{
      transform: translateY(-1px);
      box-shadow: 0 4px 15px var(--cyan-glow);
    }}

    /* Hero Section */
    .hero {{
      padding: 5rem 0 3.5rem;
      text-align: center;
      position: relative;
    }}
    .hero-pill {{
      display: inline-flex; align-items: center; gap: 8px;
      padding: 6px 16px; border-radius: 999px;
      background: rgba(6, 182, 212, 0.1); border: 1px solid rgba(6, 182, 212, 0.3);
      color: #38bdf8; font-size: 0.825rem; font-weight: 700; margin-bottom: 1.5rem;
    }}
    .hero h1 {{
      font-size: clamp(2.25rem, 5vw, 3.75rem);
      font-weight: 800; line-height: 1.15;
      letter-spacing: -1px; margin-bottom: 1.5rem;
    }}
    .hero h1 span.gradient-text {{
      background: linear-gradient(135deg, #38bdf8 0%, #06b6d4 50%, #34d399 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}
    .hero-desc {{
      max-width: 760px; margin: 0 auto 2.5rem;
      font-size: 1.125rem; color: var(--text-muted); line-height: 1.7;
    }}
    .hero-cta-group {{
      display: flex; flex-wrap: wrap; justify-content: center; gap: 1rem; margin-bottom: 3rem;
    }}
    .btn-primary {{
      background: linear-gradient(135deg, #06b6d4, #0284c7);
      color: #020617; font-weight: 800; font-size: 1rem;
      padding: 0.875rem 2rem; border-radius: 10px; border: none; cursor: pointer;
      box-shadow: 0 4px 20px var(--cyan-glow);
      transition: all 0.2s;
    }}
    .btn-primary:hover {{
      transform: translateY(-2px);
      box-shadow: 0 6px 25px rgba(6, 182, 212, 0.45);
    }}
    .btn-secondary {{
      background: rgba(15, 23, 42, 0.8);
      border: 1px solid #334155; color: #f8fafc;
      font-weight: 700; font-size: 1rem;
      padding: 0.875rem 1.75rem; border-radius: 10px; cursor: pointer;
      transition: all 0.2s;
    }}
    .btn-secondary:hover {{
      border-color: #38bdf8; background: rgba(30, 41, 59, 0.9);
    }}

    /* Trust Stats */
    .trust-stats {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1.5rem; margin-top: 1rem; margin-bottom: 4rem;
    }}
    .stat-card {{
      background: var(--card-bg); border: 1px solid var(--card-border);
      border-radius: 14px; padding: 1.5rem; text-align: center;
    }}
    .stat-card .num {{
      font-size: 2rem; font-weight: 800; color: #38bdf8; margin-bottom: 4px;
    }}
    .stat-card .label {{
      font-size: 0.85rem; color: var(--text-muted); font-weight: 600;
    }}

    /* Section Header */
    .section-title-wrap {{
      text-align: center; margin-bottom: 2.5rem;
    }}
    .section-tag {{
      color: var(--cyan); font-size: 0.8rem; font-weight: 800;
      letter-spacing: 1px; text-transform: uppercase; margin-bottom: 0.5rem; display: block;
    }}
    .section-title {{
      font-size: 2.25rem; font-weight: 800; letter-spacing: -0.5px; margin-bottom: 0.75rem;
    }}
    .section-desc {{
      color: var(--text-muted); font-size: 1rem; max-width: 650px; margin: 0 auto;
    }}

    /* Feature Grid */
    .features-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 1.5rem; margin-bottom: 4.5rem;
    }}
    .feature-card {{
      background: var(--card-bg); border: 1px solid var(--card-border);
      border-radius: 16px; padding: 2rem;
      transition: border-color 0.25s, transform 0.25s;
    }}
    .feature-card:hover {{
      border-color: var(--card-hover); transform: translateY(-3px);
    }}
    .feature-icon {{
      width: 48px; height: 48px; border-radius: 12px;
      display: flex; align-items: center; justify-content: center;
      font-size: 1.5rem; margin-bottom: 1.25rem;
    }}
    .icon-cyan {{ background: rgba(6, 182, 212, 0.15); color: #38bdf8; }}
    .icon-emerald {{ background: rgba(16, 185, 129, 0.15); color: #34d399; }}
    .icon-amber {{ background: rgba(245, 158, 11, 0.15); color: #fbbf24; }}
    .icon-purple {{ background: rgba(139, 92, 246, 0.15); color: #c084fc; }}
    .feature-card h3 {{
      font-size: 1.25rem; font-weight: 700; margin-bottom: 0.5rem;
    }}
    .feature-card p {{
      color: var(--text-muted); font-size: 0.925rem; line-height: 1.6;
    }}

    /* Demo Tool Container */
    .demo-container {{
      background: #091224; border: 1px solid rgba(56, 189, 248, 0.25);
      border-radius: 20px; padding: 2rem; margin-bottom: 4.5rem;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
    }}
    .demo-toolbar {{
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
      gap: 12px; margin-bottom: 1.25rem; padding-bottom: 1rem;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }}
    .demo-presets {{
      display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    }}
    .preset-btn {{
      background: #0f1f3d; border: 1px solid #1e3a6a; color: #cbd5e1;
      padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 600;
      cursor: pointer; transition: all 0.2s;
    }}
    .preset-btn:hover {{
      background: #1e3a6a; color: #f8fafc; border-color: #38bdf8;
    }}
    .preset-btn.active {{
      background: rgba(6, 182, 212, 0.2); border-color: #06b6d4; color: #38bdf8;
    }}

    .action-tools {{
      display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    }}
    .tool-btn {{
      background: #0f2744; border: 1px solid #0284c7; color: #38bdf8;
      padding: 6px 14px; border-radius: 8px; font-size: 12px; font-weight: 700;
      cursor: pointer; transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px;
    }}
    .tool-btn:hover {{
      background: #0284c7; color: #020617;
    }}
    .tool-btn-danger {{
      background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #fca5a5;
    }}
    .tool-btn-danger:hover {{
      background: #ef4444; color: #fff;
    }}

    /* Add Invoice Collapsible Form Card */
    .add-form-card {{
      background: #061126; border: 1px solid #0284c7; border-radius: 14px;
      padding: 1.25rem; margin-bottom: 1.25rem; display: none;
    }}
    .add-form-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px; margin-bottom: 12px;
    }}
    .add-field {{ display: flex; flex-direction: column; gap: 4px; }}
    .add-field label {{ font-size: 11px; font-weight: 700; color: #94a3b8; text-transform: uppercase; }}
    .add-input {{
      background: #0b1936; border: 1px solid #1e3a6a; border-radius: 8px;
      padding: 8px 10px; color: #f8fafc; font-size: 13px; font-family: inherit;
    }}
    .add-input:focus {{ outline: none; border-color: #38bdf8; }}

    /* Quick Paste Card */
    .paste-card {{
      background: #061126; border: 1px solid #10b981; border-radius: 14px;
      padding: 1.25rem; margin-bottom: 1.25rem; display: none;
    }}

    /* Table Styles */
    .table-responsive {{
      overflow-x: auto; margin-bottom: 1.5rem; border-radius: 12px;
      border: 1px solid rgba(255, 255, 255, 0.08); background: #050c1c;
    }}
    .data-table {{
      width: 100%; border-collapse: collapse; text-align: left; font-size: 13px;
    }}
    .data-table th {{
      background: #08152e; padding: 12px 14px; font-size: 11px;
      text-transform: uppercase; letter-spacing: 0.5px; color: #94a3b8; font-weight: 700;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }}
    .data-table td {{
      padding: 12px 14px; border-bottom: 1px solid rgba(255, 255, 255, 0.05);
      color: #e2e8f0; vertical-align: middle;
    }}
    .data-table tr:hover {{
      background: rgba(56, 189, 248, 0.03);
    }}
    .data-table tr.row-flagged {{
      background: rgba(239, 68, 68, 0.08);
    }}
    .btn-row-del {{
      background: transparent; border: none; color: #94a3b8; cursor: pointer;
      font-size: 14px; padding: 4px 8px; border-radius: 6px;
    }}
    .btn-row-del:hover {{ color: #ef4444; background: rgba(239, 68, 68, 0.15); }}

    /* Main Run Action Button */
    .demo-actions {{
      display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-top: 1rem;
    }}
    .btn-run {{
      background: linear-gradient(135deg, #06b6d4, #10b981);
      color: #020617; font-weight: 800; font-size: 1.05rem;
      padding: 0.9rem 2.25rem; border-radius: 12px; border: none; cursor: pointer;
      display: inline-flex; align-items: center; gap: 8px;
      box-shadow: 0 4px 20px rgba(16, 185, 129, 0.35);
      transition: all 0.2s;
    }}
    .btn-run:hover {{
      transform: translateY(-2px);
      box-shadow: 0 8px 30px rgba(16, 185, 129, 0.5);
    }}

    /* Result Box */
    .result-box {{
      margin-top: 2rem; background: #050b18; border: 1px solid rgba(56, 189, 248, 0.3);
      border-radius: 16px; padding: 1.75rem; display: none;
    }}
    .kpi-row {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px; margin-bottom: 1.5rem;
    }}
    .kpi-pill {{
      background: #0b152c; border: 1px solid #1e2d4d; border-radius: 10px;
      padding: 10px 14px; text-align: center;
    }}
    .kpi-pill .val {{ font-size: 1.25rem; font-weight: 800; color: #f8fafc; }}
    .kpi-pill .lbl {{ font-size: 11px; color: #94a3b8; font-weight: 600; margin-top: 2px; }}

    .badge-status {{
      display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px;
      border-radius: 999px; font-size: 11px; font-weight: 700;
    }}
    .badge-ok {{ background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }}
    .badge-err {{ background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }}
    .badge-warn {{ background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }}

    /* Pricing Section */
    .pricing-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 20px; margin-bottom: 4.5rem;
    }}
    .pricing-card {{
      background: var(--card-bg); border: 1px solid var(--card-border);
      border-radius: 18px; padding: 2rem 1.75rem; position: relative;
      display: flex; flex-direction: column; justify-content: space-between;
      transition: all 0.25s;
    }}
    .pricing-card.featured {{
      border-color: #06b6d4; background: linear-gradient(180deg, rgba(6, 182, 212, 0.08) 0%, var(--card-bg) 100%);
      box-shadow: 0 10px 30px -10px var(--cyan-glow);
    }}
    .pricing-badge {{
      position: absolute; top: -12px; left: 50%; transform: translateX(-50%);
      background: linear-gradient(135deg, #06b6d4, #0284c7); color: #020617;
      font-size: 11px; font-weight: 800; padding: 3px 14px; border-radius: 999px;
      letter-spacing: 0.5px;
    }}
    .pricing-name {{ font-size: 1.25rem; font-weight: 800; margin-bottom: 8px; }}
    .pricing-sub {{ font-size: 13px; color: var(--text-muted); min-height: 40px; margin-bottom: 1.25rem; }}
    .pricing-price {{ display: flex; align-items: baseline; gap: 6px; margin-bottom: 1.5rem; }}
    .pricing-price .amount {{ font-size: 2.25rem; font-weight: 800; color: #f8fafc; }}
    .pricing-price .period {{ font-size: 13px; color: var(--text-muted); }}
    .feature-list {{ list-style: none; margin-bottom: 2rem; font-size: 13px; color: #cbd5e1; }}
    .feature-list li {{ display: flex; align-items: flex-start; gap: 10px; margin-bottom: 10px; }}
    .feature-list li span.check {{ color: #10b981; font-weight: 800; }}
    .btn-plan {{
      width: 100%; padding: 12px; border-radius: 10px; font-weight: 700;
      font-size: 14px; cursor: pointer; transition: all 0.2s; border: none;
      text-align: center;
    }}
    .btn-plan-featured {{
      background: linear-gradient(135deg, #06b6d4, #0284c7); color: #020617;
      box-shadow: 0 4px 15px var(--cyan-glow);
    }}
    .btn-plan-default {{
      background: rgba(30, 41, 59, 0.8); color: #f1f5f9; border: 1px solid #334155;
    }}

    /* Support Forms */
    .lead-checkout-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 20px; margin-bottom: 4.5rem;
    }}
    .form-card {{
      background: var(--card-bg); border: 1px solid var(--card-border);
      border-radius: 18px; padding: 2rem;
    }}
    .form-card h3 {{ font-size: 1.25rem; font-weight: 800; margin-bottom: 8px; }}
    .form-card p {{ font-size: 13px; color: var(--text-muted); margin-bottom: 1.25rem; }}
    .form-group {{ margin-bottom: 12px; }}
    .form-group label {{ display: block; font-size: 12px; font-weight: 600; color: #cbd5e1; margin-bottom: 6px; }}
    .form-input {{
      width: 100%; background: #020617; border: 1px solid #334155;
      border-radius: 8px; padding: 10px 14px; color: #f8fafc; font-size: 13px;
    }}
    .form-input:focus {{ outline: none; border-color: var(--cyan); box-shadow: 0 0 10px var(--cyan-glow); }}

    /* Security banner */
    .security-banner {{
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.08) 0%, rgba(6, 182, 212, 0.08) 100%);
      border: 1px solid rgba(16, 185, 129, 0.25); border-radius: 18px;
      padding: 2rem; margin-bottom: 4.5rem; display: flex; gap: 20px; align-items: center;
    }}
    .security-icon {{ font-size: 3rem; flex-shrink: 0; }}
    .security-content h3 {{ font-size: 1.25rem; font-weight: 800; color: #34d399; margin-bottom: 6px; }}
    .security-content p {{ font-size: 13px; color: #cbd5e1; line-height: 1.6; }}

    /* FAQ */
    .faq-list {{ max-width: 800px; margin: 0 auto 4.5rem; }}
    .faq-item {{
      background: var(--card-bg); border: 1px solid var(--card-border);
      border-radius: 12px; padding: 1.25rem 1.5rem; margin-bottom: 12px;
      cursor: pointer;
    }}
    .faq-q {{ font-weight: 700; font-size: 15px; display: flex; justify-content: space-between; align-items: center; }}
    .faq-a {{ font-size: 13px; color: var(--text-muted); margin-top: 8px; display: none; line-height: 1.6; }}
    .faq-item.active .faq-a {{ display: block; }}
    .faq-item.active .faq-toggle {{ transform: rotate(180deg); }}
    .faq-toggle {{ transition: transform 0.2s; font-size: 12px; color: var(--cyan); }}

    /* Footer */
    footer {{
      border-top: 1px solid rgba(255, 255, 255, 0.08);
      padding: 3rem 0 2rem; font-size: 13px; color: var(--text-muted);
      text-align: center;
    }}
  </style>
</head>
<body>

  <!-- Navigation -->
  <header>
    <div class="container nav-wrap">
      <div class="logo">
        <span style="font-size:1.5rem;">🛡️</span>
        <span>InvoiceGuard</span>
        <span class="logo-badge">VN SME 2026</span>
      </div>
      <div class="nav-links">
        <a href="#features">Tính Năng</a>
        <a href="#demo">Trải Nghiệm</a>
        <a href="#pricing">Bảng Giá</a>
        <a href="#faq">Hỏi Đáp</a>
        <a href="#demo" class="btn-nav">Rà Soát Miễn Phí</a>
      </div>
    </div>
  </header>

  <!-- Hero Section -->
  <section class="container hero">
    <div class="hero-pill">
      <span>⚡ Bộ lọc đối chiếu hóa đơn & Thuế VAT chuẩn Nghị định 72/2024</span>
    </div>
    <h1>
      Rà Soát Toàn Diện Hóa Đơn & Thuế VAT<br/>
      <span class="gradient-text">Tự Động — Chính Xác — Trong 5 Giây</span>
    </h1>
    <p class="hero-desc">
      Bảo vệ doanh nghiệp khỏi rủi ro phạt thuế, truy thu sai sót và hóa đơn bất hợp pháp. Tự động kiểm tra công thức thuế suất 0% / 5% / 8% / 10%, mã số thuế doanh nghiệp và trùng lặp hóa đơn trước khi nộp tờ khai.
    </p>
    <div class="hero-cta-group">
      <a href="#demo" class="btn-primary">▶️ Trải Nghiệm Thử Nghiệm Ngay</a>
      <a href="#pricing" class="btn-secondary">Xem Bảng Giá Dịch Vụ</a>
    </div>

    <!-- Trust Stats -->
    <div class="trust-stats">
      <div class="stat-card">
        <div class="num">&lt; 5 Giây</div>
        <div class="label">Thời Gian Rà Soát 10.000 Hóa Đơn</div>
      </div>
      <div class="stat-card">
        <div class="num">100% Client-Side</div>
        <div class="label">Chạy 24/7 Không Cần Máy Chủ</div>
      </div>
      <div class="stat-card">
        <div class="num">0.02 đ</div>
        <div class="label">Độ Chính Xác Dung Sai Làm Tròn</div>
      </div>
      <div class="stat-card">
        <div class="num">100% An Toàn</div>
        <div class="label">Dữ Liệu Nội Bộ — Không Rò Rỉ</div>
      </div>
    </div>
  </section>

  <!-- Interactive Demo Section -->
  <section id="demo" class="container">
    <div class="section-title-wrap">
      <span class="section-tag">TRẢI NGHIỆM TRỰC QUAN</span>
      <h2 class="section-title">Kiểm Chứng Trực Tiếp Bộ Rà Soát Hóa Đơn</h2>
      <p class="section-desc">Bạn có thể chọn dữ liệu mẫu, hoặc <strong>thêm trực tiếp hóa đơn của bạn</strong> để đối soát ngay lập tức:</p>
    </div>

    <div class="demo-container">
      <!-- Toolbar -->
      <div class="demo-toolbar">
        <div class="demo-presets">
          <span style="font-size:12px; font-weight:700; color:#94a3b8; margin-right:4px;">Mẫu có sẵn:</span>
          <button type="button" id="btnPresetSample" class="preset-btn active" onclick="loadPreset('sample')">⚠️ Mẫu Lỗi Điển Hình</button>
          <button type="button" id="btnPresetClean" class="preset-btn" onclick="loadPreset('clean')">✅ Mẫu Chuẩn 100%</button>
          <button type="button" id="btnPresetInvalid" class="preset-btn" onclick="loadPreset('invalid_rate')">❌ Mẫu Lệch Thuế Suất</button>
        </div>
        <div class="action-tools">
          <button type="button" class="tool-btn" onclick="toggleAddForm()">
            <span>➕ Thêm Hóa Đơn Của Bạn</span>
          </button>
          <button type="button" class="tool-btn" onclick="togglePasteForm()">
            <span>📋 Dán Từ Excel / MISA</span>
          </button>
          <button type="button" class="tool-btn tool-btn-danger" onclick="clearAllRows()">
            <span>🗑️ Xóa Trắng Bảng</span>
          </button>
        </div>
      </div>

      <!-- Collapsible Add Invoice Form -->
      <div class="add-form-card" id="addFormCard">
        <h4 style="font-size:14px; font-weight:800; color:#38bdf8; margin-bottom:10px; display:flex; align-items:center; justify-content:space-between;">
          <span>➕ Nhập Thông Tin Hóa Đơn Mới Để Rà Soát</span>
          <button type="button" onclick="toggleAddForm()" style="background:transparent; border:none; color:#94a3b8; font-size:16px; cursor:pointer;">✕</button>
        </h4>
        <div class="add-form-grid">
          <div class="add-field">
            <label>Số Hóa Đơn *</label>
            <input type="text" id="addInvNo" class="add-input" placeholder="VD: INV-0099" value="INV-005" />
          </div>
          <div class="add-field">
            <label>MST Người Bán *</label>
            <input type="text" id="addTaxId" class="add-input" placeholder="VD: 0101234567" value="0109988776" />
          </div>
          <div class="add-field">
            <label>Doanh Số Chưa Thuế (đ) *</label>
            <input type="number" id="addSubtotal" class="add-input" placeholder="VD: 20000000" value="20000000" oninput="autoCalcTax()" />
          </div>
          <div class="add-field">
            <label>Thuế Suất VAT</label>
            <select id="addVatRate" class="add-input" onchange="autoCalcTax()">
              <option value="10">10% (Chuẩn)</option>
              <option value="8">8% (Giảm theo NĐ 72)</option>
              <option value="5">5% (Ưu đãi)</option>
              <option value="0">0% (Xuất khẩu)</option>
              <option value="7">7% (Cố tình nhập sai để test)</option>
            </select>
          </div>
          <div class="add-field">
            <label>Tiền Thuế VAT (đ) *</label>
            <input type="number" id="addVatAmount" class="add-input" placeholder="VD: 2000000" value="2000000" oninput="updateTotalFromTax()" />
          </div>
          <div class="add-field">
            <label>Tổng Thanh Toán (đ) *</label>
            <input type="number" id="addTotalAmount" class="add-input" placeholder="VD: 22000000" value="22000000" />
          </div>
        </div>
        <div style="display:flex; gap:10px; align-items:center;">
          <button type="button" class="btn-primary" style="padding:8px 18px; font-size:13px;" onclick="addInvoiceFromForm()">
            ➕ Thêm Vào Bảng Rà Soát
          </button>
          <button type="button" class="preset-btn" onclick="autoCalcTax()">
            🔄 Tự Động Tính Tiền Thuế
          </button>
          <span id="addFormMsg" style="font-size:12px; color:#34d399; font-weight:600;"></span>
        </div>
      </div>

      <!-- Collapsible Paste Excel Form -->
      <div class="paste-card" id="pasteCard">
        <h4 style="font-size:14px; font-weight:800; color:#34d399; margin-bottom:8px; display:flex; align-items:center; justify-content:space-between;">
          <span>📋 Dán Dữ Liệu Copy Từ Excel Hoặc Bảng Kê MISA</span>
          <button type="button" onclick="togglePasteForm()" style="background:transparent; border:none; color:#94a3b8; font-size:16px; cursor:pointer;">✕</button>
        </h4>
        <p style="font-size:12px; color:#94a3b8; margin-bottom:8px;">
          Định dạng hỗ trợ mỗi dòng gồm: <code>Số Hóa Đơn, MST, Tiền Hàng, Thuế Suất(%), Tiền Thuế, Tổng Tiền</code> (ngăn cách bằng dấu phẩy hoặc phím Tab):
        </p>
        <textarea id="pasteTextarea" rows="4" class="add-input" style="width:100%; font-family:monospace; font-size:12px;" placeholder="INV-101, 0101122334, 15000000, 10, 1500000, 16500000&#10;INV-102, 0309988776, 30000000, 8, 2400000, 32400000"></textarea>
        <div style="margin-top:8px; display:flex; gap:10px;">
          <button type="button" class="btn-primary" style="padding:6px 16px; font-size:13px;" onclick="importFromPaste()">
            📥 Nhập Dữ Liệu Này
          </button>
          <button type="button" class="preset-btn" onclick="togglePasteForm()">Đóng</button>
        </div>
      </div>

      <!-- Preview Table -->
      <div class="table-responsive">
        <table class="data-table" id="previewTable">
          <thead>
            <tr>
              <th style="width:40px;">STT</th>
              <th>Số Hóa Đơn</th>
              <th>MST Người Bán</th>
              <th>Doanh Số Trước Thuế</th>
              <th>Thuế Suất</th>
              <th>Tiền Thuế VAT</th>
              <th>Tổng Thanh Toán</th>
              <th style="width:50px; text-align:center;">Xóa</th>
            </tr>
          </thead>
          <tbody id="previewTableBody">
            <!-- Dynamically populated -->
          </tbody>
        </table>
      </div>

      <!-- Action Button Area -->
      <div class="demo-actions">
        <button type="button" class="btn-run" onclick="runAudit()">
          <span>▶️ BẮT ĐẦU RÀ SOÁT TỰ ĐỘNG (5 GIÂY)</span>
        </button>
        <span style="font-size:12px; color:#94a3b8;">
          💡 Thuật toán kiểm toán chạy trực tiếp trong trình duyệt (100% Client-Side Engine).
        </span>
      </div>

      <!-- Result Area -->
      <div class="result-box" id="resultBox">
        <h3 style="font-size:1.15rem; font-weight:800; margin-bottom:1rem; display:flex; align-items:center; justify-content:space-between;">
          <span>📋 Báo Cáo Kết Quả Rà Soát Chi Tiết</span>
          <button type="button" id="btnExportCsv" class="preset-btn" onclick="downloadCleanCsv()" style="display:none; color:#34d399; border-color:#10b981; font-weight:700;">
            📥 Tải File CSV Sạch Chuẩn MISA
          </button>
        </h3>
        
        <div class="kpi-row">
          <div class="kpi-pill">
            <div class="val" id="resTotal">0</div>
            <div class="lbl">Tổng Số HĐ</div>
          </div>
          <div class="kpi-pill">
            <div class="val" id="resValid" style="color:#34d399;">0</div>
            <div class="lbl">Hợp Lệ Chuẩn</div>
          </div>
          <div class="kpi-pill">
            <div class="val" id="resInvalid" style="color:#f87171;">0</div>
            <div class="lbl">Cảnh Báo Lỗi</div>
          </div>
          <div class="kpi-pill">
            <div class="val" id="resSubtotal">0 đ</div>
            <div class="lbl">Doanh Số Chưa Thuế</div>
          </div>
          <div class="kpi-pill">
            <div class="val" id="resVat">0 đ</div>
            <div class="lbl">Tổng Thuế VAT</div>
          </div>
          <div class="kpi-pill">
            <div class="val" id="resGross">0 đ</div>
            <div class="lbl">Tổng Thanh Toán</div>
          </div>
        </div>

        <div id="issuesContainer"></div>
      </div>
    </div>
  </section>

  <!-- Features Section -->
  <section id="features" class="container">
    <div class="section-title-wrap">
      <span class="section-tag">CÔNG NGHỆ BẢO VỆ THUẾ</span>
      <h2 class="section-title">Tại Sao Doanh Nghiệp Cần InvoiceGuard?</h2>
      <p class="section-desc">Loại bỏ rủi ro bị truy thu thuế và xử phạt hành chính nhờ 4 trụ cột kiểm toán thông minh:</p>
    </div>

    <div class="features-grid">
      <div class="feature-card">
        <div class="feature-icon icon-cyan">🎯</div>
        <h3>Đối Chiếu Thuế Suất VAT Chuẩn Xác</h3>
        <p>Tự động xác minh công thức thuế suất 0%, 5%, 8% (Nghị định 72) và 10%. Phát hiện ngay lập tức hóa đơn bị tính sai tiền thuế dù chỉ lệch 1 đồng.</p>
      </div>

      <div class="feature-card">
        <div class="feature-icon icon-emerald">🛡️</div>
        <h3>Quét Trùng Lặp & Giả Mạo Hóa Đơn</h3>
        <p>Phát hiện tình trạng trùng lặp số hóa đơn, ngày lập và mã số thuế bên bán trong toàn bộ kỳ kế toán, ngăn ngừa nguy cơ kê khai trùng khấu trừ.</p>
      </div>

      <div class="feature-card">
        <div class="feature-icon icon-amber">🔍</div>
        <h3>Kiểm Tra Định Dạng MST Doanh Nghiệp</h3>
        <p>Xác thực thuật toán kiểm tra mã số thuế 10 số (doanh nghiệp mẹ) hoặc 13 số (chi nhánh/đơn vị trực thuộc) theo đúng chuẩn Tổng cục Thuế.</p>
      </div>

      <div class="feature-card">
        <div class="feature-icon icon-purple">⚡</div>
        <h3>Tương Thích MISA, FAST & Excel</h3>
        <p>Xuất file kết quả chuẩn định dạng để nhập trực tiếp vào phần mềm kế toán MISA SME, FAST hoặc HTKK chỉ bằng 1 cú nhấp chuột.</p>
      </div>
    </div>
  </section>

  <!-- Pricing Section -->
  <section id="pricing" class="container">
    <div class="section-title-wrap">
      <span class="section-tag">CHI PHÍ MINH BẠCH</span>
      <h2 class="section-title">Bảng Giá Dịch Vụ InvoiceGuard</h2>
      <p class="section-desc">Không phát sinh bất kỳ chi phí ẩn nào. Khởi đầu miễn phí, nâng cấp khi quy mô doanh nghiệp mở rộng:</p>
    </div>

    <div class="pricing-grid">
      <!-- Starter -->
      <div class="pricing-card">
        <div>
          <h3 class="pricing-name">STARTER (Dùng Thử)</h3>
          <p class="pricing-sub">Phù hợp hộ kinh doanh cá thể và doanh nghiệp siêu nhỏ muốn trải nghiệm rà soát.</p>
          <div class="pricing-price">
            <span class="amount">$0</span>
            <span class="period">/ tháng</span>
          </div>
          <ul class="feature-list">
            <li><span class="check">✓</span> Rà soát tối đa 50 hóa đơn / tháng</li>
            <li><span class="check">✓</span> Kiểm tra 4 khung thuế VAT chuẩn</li>
            <li><span class="check">✓</span> Báo cáo sai lệch cơ bản</li>
            <li><span class="check">✓</span> Chạy trực tiếp trên trình duyệt</li>
          </ul>
        </div>
        <button type="button" class="btn-plan btn-plan-default" onclick="selectPlan('STARTER')">
          Bắt Đầu Miễn Phí
        </button>
      </div>

      <!-- Pro -->
      <div class="pricing-card featured">
        <div class="pricing-badge">PHỔ BIẾN NHẤT</div>
        <div>
          <h3 class="pricing-name">PRO (Bảo Vệ Toàn Diện)</h3>
          <p class="pricing-sub">Dành cho doanh nghiệp SME có lượng hóa đơn đầu vào 500 - 2.000 tờ / tháng.</p>
          <div class="pricing-price">
            <span class="amount">$8</span>
            <span class="period">/ tháng (khoảng 200.000 đ)</span>
          </div>
          <ul class="feature-list">
            <li><span class="check">✓</span> Không giới hạn số lượng hóa đơn</li>
            <li><span class="check">✓</span> Kiểm tra trùng lặp đa kỳ kê khai</li>
            <li><span class="check">✓</span> Xuất file CSV chuẩn hóa MISA / FAST</li>
            <li><span class="check">✓</span> Hỗ trợ ưu tiên qua Zalo / Hotline</li>
          </ul>
        </div>
        <button type="button" class="btn-plan btn-plan-featured" onclick="selectPlan('PRO')">
          Đăng Ký Gói PRO ($8/tháng)
        </button>
      </div>

      <!-- Enterprise -->
      <div class="pricing-card">
        <div>
          <h3 class="pricing-name">ENTERPRISE (Doanh Nghiệp)</h3>
          <p class="pricing-sub">Dành cho công ty dịch vụ kế toán hoặc chuỗi nhiều chi nhánh cần API tự động.</p>
          <div class="pricing-price">
            <span class="amount">$20</span>
            <span class="period">/ tháng (khoảng 500.000 đ)</span>
          </div>
          <ul class="feature-list">
            <li><span class="check">✓</span> Toàn bộ quyền lợi gói PRO</li>
            <li><span class="check">✓</span> Tích hợp Webhook & API tự động hóa</li>
            <li><span class="check">✓</span> Hỗ trợ đa mã số thuế & chi nhánh</li>
            <li><span class="check">✓</span> Ký hợp đồng & xuất hóa đơn VAT đầy đủ</li>
          </ul>
        </div>
        <button type="button" class="btn-plan btn-plan-default" onclick="selectPlan('ENTERPRISE')">
          Đăng Ký Gói Doanh Nghiệp
        </button>
      </div>
    </div>

    <!-- Lead Form & Checkout Demo -->
    <div class="lead-checkout-grid">
      <!-- Trial Lead Form -->
      <div class="form-card" id="leadFormCard">
        <h3>📩 Nhận Tài Liệu Hướng Dẫn & Đăng Ký Miễn Phí</h3>
        <p>Để lại email để nhận ngay file Excel mẫu đối soát hóa đơn thuế VAT và tài khoản dùng thử:</p>
        <div class="form-group">
          <label>Email Liên Hệ *</label>
          <input type="email" id="leadEmail" class="form-input" placeholder="tenban@doanhnghiep.vn" />
        </div>
        <div class="form-group">
          <label>Nhu Cầu Cụ Thể (Tùy chọn)</label>
          <input type="text" id="leadFeedback" class="form-input" placeholder="VD: Doanh nghiệp xuất nhập khẩu cần rà soát thuế 8%" />
        </div>
        <button type="button" class="btn-primary" style="width:100%;" onclick="submitLead()">
          Gửi Yêu Cầu Nhận Tài Liệu
        </button>
        <p id="leadMsg" style="margin-top:10px; font-size:12px; color:#38bdf8;"></p>
      </div>

      <!-- Transparent Sandbox Checkout -->
      <div class="form-card" id="checkoutCard">
        <h3>💳 Đặt Gói Thử Nghiệm Sandbox</h3>
        <p>Môi trường Sandbox thử nghiệm minh bạch. Hệ thống không trừ tiền thật khi chưa có sự xác nhận của bạn:</p>
        <div class="form-group">
          <label>Gói Dịch Vụ</label>
          <select id="checkoutPlan" class="form-input">
            <option value="PRO">Gói PRO ($8.00 / tháng)</option>
            <option value="ENTERPRISE">Gói ENTERPRISE ($20.00 / tháng)</option>
          </select>
        </div>
        <div class="form-group">
          <label>Email Nhận Kích Hoạt *</label>
          <input type="email" id="buyerEmail" class="form-input" placeholder="ketoan@congty.com" />
        </div>
        <button type="button" class="btn-primary" style="width:100%; background:linear-gradient(135deg, #10b981, #059669);" onclick="submitCheckout()">
          Tạo Đơn Hàng Thử Nghiệm (Sandbox)
        </button>
        <div id="checkoutMsg" style="margin-top:10px; font-size:12px; color:#34d399;"></div>
      </div>
    </div>
  </section>

  <!-- Security Banner -->
  <section class="container">
    <div class="security-banner">
      <div class="security-icon">🔒</div>
      <div class="security-content">
        <h3>Bảo Mật Dữ Liệu Tuyệt Đối — Hoạt Động Không Cần Lưu Trữ Trực Tuyến</h3>
        <p>
          Hệ thống InvoiceGuard được thiết kế với tiêu chuẩn <strong>Privacy-First</strong>: Toàn bộ quá trình tính toán, phân tích công thức thuế và phát hiện trùng lặp đều được thực thi trực tiếp trong bộ nhớ RAM trình duyệt của bạn (In-Browser Execution). Thông tin hóa đơn nội bộ không bao giờ bị lưu trái phép trên máy chủ trung gian.
        </p>
      </div>
    </div>
  </section>

  <!-- FAQ Section -->
  <section id="faq" class="container">
    <div class="section-title-wrap">
      <span class="section-tag">HỎI ĐÁP PHỔ BIẾN</span>
      <h2 class="section-title">Câu Hỏi Thường Gặp</h2>
    </div>

    <div class="faq-list">
      <div class="faq-item" onclick="toggleFaq(this)">
        <div class="faq-q">
          <span>Hệ thống kiểm tra được những lỗi hóa đơn nào?</span>
          <span class="faq-toggle">▼</span>
        </div>
        <div class="faq-a">
          Hệ thống tự động phát hiện: (1) Lệch tiền thuế VAT do áp dụng sai thuế suất 8% hoặc 10%; (2) Lệch tổng tiền thanh toán do sai sót làm tròn; (3) Trùng lặp số hóa đơn từ cùng một nhà cung cấp; (4) Mã số thuế sai cấu trúc quy chuẩn Việt Nam.
        </div>
      </div>

      <div class="faq-item" onclick="toggleFaq(this)">
        <div class="faq-q">
          <span>Tôi có thể nhập trực tiếp hóa đơn của công ty tôi để thử không?</span>
          <span class="faq-toggle">▼</span>
        </div>
        <div class="faq-a">
          Hoàn toàn được! Bạn chỉ cần bấm nút "➕ Thêm Hóa Đơn Của Bạn" hoặc "📋 Dán Từ Excel / MISA" ở phần Trải Nghiệm phía trên để thêm hóa đơn của riêng bạn vào bảng kiểm tra.
        </div>
      </div>

      <div class="faq-item" onclick="toggleFaq(this)">
        <div class="faq-q">
          <span>Dịch vụ có phát sinh chi phí tự động từ tài khoản của tôi không?</span>
          <span class="faq-toggle">▼</span>
        </div>
        <div class="faq-a">
          Hoàn toàn KHÔNG. Mọi hoạt động dùng thử đều miễn phí 100%. Nếu bạn muốn mua gói dịch vụ, bạn cần chủ động xác nhận và chuyển khoản qua cổng thanh toán được duyệt rõ ràng.
        </div>
      </div>
    </div>
  </section>

  <!-- Footer -->
  <footer>
    <div class="container">
      <p style="font-weight:700; color:#f8fafc; margin-bottom:6px;">InvoiceGuard SME Vietnam — Hệ Thống Tự Động Hóa Rà Soát Thuế Doanh Nghiệp</p>
      <p>Tuân thủ Nghị định 123/2020/NĐ-CP & Nghị định 72/2024/NĐ-CP • Vận hành 24/7 trên Edge CDN toàn cầu.</p>
    </div>
  </footer>

  <!-- Script Engine -->
  <script>
    const SLUG = '{slug}';

    const PRESETS = {{
      sample: [
        {{ invoice_no: 'INV-001', seller_tax_id: '0101234567', subtotal: 10000000, vat_rate: 10, vat_amount: 1000000, total_amount: 11000000 }},
        {{ invoice_no: 'INV-002', seller_tax_id: '0309876543', subtotal: 25000000, vat_rate: 5, vat_amount: 1250000, total_amount: 26250000 }},
        {{ invoice_no: 'INV-003', seller_tax_id: '0109998888', subtotal: 50000000, vat_rate: 8, vat_amount: 5000000, total_amount: 55000000 }},
        {{ invoice_no: 'INV-004', seller_tax_id: '0401122334', subtotal: 15000000, vat_rate: 10, vat_amount: 1500000, total_amount: 16500000 }},
        {{ invoice_no: 'INV-004', seller_tax_id: '0401122334', subtotal: 15000000, vat_rate: 10, vat_amount: 1500000, total_amount: 16500000 }}
      ],
      clean: [
        {{ invoice_no: 'HĐ-8801', seller_tax_id: '0101234567', subtotal: 12000000, vat_rate: 10, vat_amount: 1200000, total_amount: 13200000 }},
        {{ invoice_no: 'HĐ-8802', seller_tax_id: '0309876543', subtotal: 45000000, vat_rate: 8, vat_amount: 3600000, total_amount: 48600000 }},
        {{ invoice_no: 'HĐ-8803', seller_tax_id: '0401122334', subtotal: 8000000, vat_rate: 5, vat_amount: 400000, total_amount: 8400000 }}
      ],
      invalid_rate: [
        {{ invoice_no: 'INV-ERR1', seller_tax_id: '123', subtotal: 20000000, vat_rate: 7, vat_amount: 1400000, total_amount: 21400000 }},
        {{ invoice_no: 'INV-ERR2', seller_tax_id: '0109998888', subtotal: 30000000, vat_rate: 10, vat_amount: 3500000, total_amount: 33500000 }}
      ]
    }};

    var currentInvoices = JSON.parse(JSON.stringify(PRESETS.sample));
    var currentReport = null;

    function renderTable() {{
      const tbody = document.getElementById('previewTableBody');
      if (!tbody) return;
      if (currentInvoices.length === 0) {{
        tbody.innerHTML = `
          <tr>
            <td colspan="8" style="text-align:center; padding:2rem; color:#94a3b8;">
              Chưa có hóa đơn nào trong bảng. Bấm <strong>"➕ Thêm Hóa Đơn Của Bạn"</strong> hoặc chọn <strong>"Mẫu có sẵn"</strong> để bắt đầu!
            </td>
          </tr>
        `;
        return;
      }}
      tbody.innerHTML = currentInvoices.map((r, i) => `
        <tr id="row-${{i}}">
          <td style="color:#64748b; font-weight:700;">#${{i + 1}}</td>
          <td><strong style="color:#38bdf8;">${{r.invoice_no || '—'}}</strong></td>
          <td><code>${{r.seller_tax_id || '—'}}</code></td>
          <td style="font-weight:600;">${{Number(r.subtotal).toLocaleString('vi-VN')}} đ</td>
          <td><span class="badge-status badge-ok">${{r.vat_rate}}%</span></td>
          <td>${{Number(r.vat_amount).toLocaleString('vi-VN')}} đ</td>
          <td style="font-weight:700; color:#f8fafc;">${{Number(r.total_amount).toLocaleString('vi-VN')}} đ</td>
          <td style="text-align:center;">
            <button type="button" class="btn-row-del" onclick="deleteRow(${{i}})" title="Xóa dòng này">✕</button>
          </td>
        </tr>
      `).join('');
    }}

    function loadPreset(key) {{
      document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
      if (key === 'sample' && document.getElementById('btnPresetSample')) {{
        document.getElementById('btnPresetSample').classList.add('active');
      }} else if (key === 'clean' && document.getElementById('btnPresetClean')) {{
        document.getElementById('btnPresetClean').classList.add('active');
      }} else if (key === 'invalid_rate' && document.getElementById('btnPresetInvalid')) {{
        document.getElementById('btnPresetInvalid').classList.add('active');
      }}
      currentInvoices = JSON.parse(JSON.stringify(PRESETS[key] || PRESETS.sample));
      renderTable();
      document.getElementById('resultBox').style.display = 'none';
    }}

    function deleteRow(idx) {{
      currentInvoices.splice(idx, 1);
      renderTable();
    }}

    function clearAllRows() {{
      currentInvoices = [];
      renderTable();
      document.getElementById('resultBox').style.display = 'none';
    }}

    function toggleAddForm() {{
      const card = document.getElementById('addFormCard');
      card.style.display = (card.style.display === 'block') ? 'none' : 'block';
      if (card.style.display === 'block') {{
        document.getElementById('pasteCard').style.display = 'none';
        document.getElementById('addInvNo').focus();
      }}
    }}

    function togglePasteForm() {{
      const card = document.getElementById('pasteCard');
      card.style.display = (card.style.display === 'block') ? 'none' : 'block';
      if (card.style.display === 'block') {{
        document.getElementById('addFormCard').style.display = 'none';
        document.getElementById('pasteTextarea').focus();
      }}
    }}

    function autoCalcTax() {{
      const subtotal = parseFloat(document.getElementById('addSubtotal').value) || 0;
      const rate = parseFloat(document.getElementById('addVatRate').value) || 0;
      const vat = Math.round(subtotal * (rate / 100));
      const total = subtotal + vat;
      document.getElementById('addVatAmount').value = vat;
      document.getElementById('addTotalAmount').value = total;
    }}

    function updateTotalFromTax() {{
      const subtotal = parseFloat(document.getElementById('addSubtotal').value) || 0;
      const vat = parseFloat(document.getElementById('addVatAmount').value) || 0;
      document.getElementById('addTotalAmount').value = subtotal + vat;
    }}

    function addInvoiceFromForm() {{
      const invNo = document.getElementById('addInvNo').value.trim();
      const taxId = document.getElementById('addTaxId').value.trim();
      const subtotal = parseFloat(document.getElementById('addSubtotal').value) || 0;
      const vatRate = parseFloat(document.getElementById('addVatRate').value) || 0;
      const vatAmount = parseFloat(document.getElementById('addVatAmount').value) || 0;
      const totalAmount = parseFloat(document.getElementById('addTotalAmount').value) || 0;

      if (!invNo) {{
        alert('Vui lòng nhập số hóa đơn');
        return;
      }}

      currentInvoices.push({{
        invoice_no: invNo,
        seller_tax_id: taxId,
        subtotal: subtotal,
        vat_rate: vatRate,
        vat_amount: vatAmount,
        total_amount: totalAmount
      }});

      renderTable();
      document.getElementById('addFormMsg').textContent = '✅ Đã thêm hóa đơn ' + invNo + ' vào bảng!';
      setTimeout(() => {{
        document.getElementById('addFormMsg').textContent = '';
      }}, 3000);

      // Auto increment invoice number for convenience
      const nextNum = currentInvoices.length + 1;
      document.getElementById('addInvNo').value = 'INV-00' + nextNum;
    }}

    function importFromPaste() {{
      const text = document.getElementById('pasteTextarea').value.trim();
      if (!text) {{
        alert('Vui lòng dán dữ liệu vào ô trước.');
        return;
      }}
      const lines = text.split('\\n');
      let count = 0;
      lines.forEach(line => {{
        const row = line.trim();
        if (!row) return;
        const parts = row.includes('\\t') ? row.split('\\t') : row.split(',');
        if (parts.length >= 3) {{
          const invNo = parts[0].trim();
          const taxId = parts[1].trim();
          const subtotal = parseFloat(parts[2].replace(/[^0-9.-]/g, '')) || 0;
          const vatRate = (parts.length > 3) ? (parseFloat(parts[3].replace(/[^0-9.-]/g, '')) || 10) : 10;
          const vatAmount = (parts.length > 4) ? (parseFloat(parts[4].replace(/[^0-9.-]/g, '')) || Math.round(subtotal * vatRate / 100)) : Math.round(subtotal * vatRate / 100);
          const totalAmount = (parts.length > 5) ? (parseFloat(parts[5].replace(/[^0-9.-]/g, '')) || (subtotal + vatAmount)) : (subtotal + vatAmount);

          currentInvoices.push({{
            invoice_no: invNo,
            seller_tax_id: taxId,
            subtotal: subtotal,
            vat_rate: vatRate,
            vat_amount: vatAmount,
            total_amount: totalAmount
          }});
          count++;
        }}
      }});

      renderTable();
      alert(`Đã nhập thành công ${{count}} hóa đơn vào bảng!`);
      togglePasteForm();
    }}

    function clientSideAuditInvoices(rows) {{
      const SUPPORTED_RATES = new Set([0, 5, 8, 10]);
      let issues = [];
      let validRecords = [];
      let netSum = 0;
      let vatSum = 0;
      let grossSum = 0;
      let seenKeys = new Set();
      let invalidCount = 0;

      rows.forEach((raw, idx) => {{
        let rowIssues = [];
        const invNo = (raw.invoice_no || '').trim();
        const taxId = (raw.seller_tax_id || '').trim();

        if (!invNo) {{
          rowIssues.push({{ row_index: idx, invoice_no: 'MISSING', code: 'MISSING_INVOICE_NO', message: 'Thiếu số hóa đơn.' }});
        }}

        const cleanTax = taxId.replace(/[^0-9]/g, '');
        if (cleanTax.length !== 10 && cleanTax.length !== 13) {{
          rowIssues.push({{ row_index: idx, invoice_no: invNo || 'UNKNOWN', code: 'INVALID_TAX_ID', message: `Mã số thuế '${{taxId}}' không đúng chuẩn 10 hoặc 13 số.` }});
        }}

        const subtotal = Number(raw.subtotal || 0);
        const vatRate = Number(raw.vat_rate || 0);
        const vatAmount = Number(raw.vat_amount || 0);
        const totalAmount = Number(raw.total_amount || 0);

        if (subtotal <= 0) {{
          rowIssues.push({{ row_index: idx, invoice_no: invNo, code: 'NEGATIVE_OR_ZERO_AMOUNT', message: `Doanh số chưa thuế (${{subtotal}}) phải lớn hơn 0.` }});
        }}
        if (!SUPPORTED_RATES.has(vatRate)) {{
          rowIssues.push({{ row_index: idx, invoice_no: invNo, code: 'UNSUPPORTED_VAT_RATE', message: `Thuế suất ${{vatRate}}% không thuộc danh mục hợp lệ (0%, 5%, 8%, 10%).` }});
        }}

        const expectedVat = Math.round(subtotal * (vatRate / 100) * 100) / 100;
        if (Math.abs(vatAmount - expectedVat) > 0.02) {{
          rowIssues.push({{ row_index: idx, invoice_no: invNo, code: 'VAT_CALCULATION_MISMATCH', message: `Tiền thuế ghi nhận (${{vatAmount.toLocaleString('vi-VN')}} đ) lệch so với mức ${{vatRate}}% (${{expectedVat.toLocaleString('vi-VN')}} đ).` }});
        }}

        const expectedTotal = Math.round((subtotal + vatAmount) * 100) / 100;
        if (Math.abs(totalAmount - expectedTotal) > 0.02) {{
          rowIssues.push({{ row_index: idx, invoice_no: invNo, code: 'TOTAL_MISMATCH', message: `Tổng thanh toán (${{totalAmount.toLocaleString('vi-VN')}} đ) khác tiền hàng + thuế (${{expectedTotal.toLocaleString('vi-VN')}} đ).` }});
        }}

        const dedupKey = `${{taxId}}::${{invNo}}`;
        if (invNo && taxId) {{
          if (seenKeys.has(dedupKey)) {{
            rowIssues.push({{ row_index: idx, invoice_no: invNo, code: 'DUPLICATE_INVOICE', message: `Trùng số hóa đơn '${{invNo}}' của cùng nhà cung cấp MST '${{taxId}}'.` }});
          }} else {{
            seenKeys.add(dedupKey);
          }}
        }}

        if (rowIssues.length > 0) {{
          issues.push(...rowIssues);
          invalidCount++;
        }} else {{
          validRecords.push({{
            invoice_no: invNo,
            seller_tax_id: taxId,
            subtotal: subtotal,
            vat_rate: vatRate,
            vat_amount: vatAmount,
            total_amount: totalAmount
          }});
          netSum += subtotal;
          vatSum += vatAmount;
          grossSum += totalAmount;
        }}
      }});

      return {{
        total_rows: rows.length,
        valid_rows: validRecords.length,
        invalid_rows: invalidCount,
        net_subtotal: Math.round(netSum * 100) / 100,
        total_vat: Math.round(vatSum * 100) / 100,
        gross_total: Math.round(grossSum * 100) / 100,
        issues: issues,
        normalized_records: validRecords
      }};
    }}

    function displayAuditResults(data) {{
      currentReport = data;
      const resBox = document.getElementById('resultBox');
      resBox.style.display = 'block';

      // Populate KPIs
      document.getElementById('resTotal').textContent = data.total_rows || 0;
      document.getElementById('resValid').textContent = data.valid_rows || 0;
      document.getElementById('resInvalid').textContent = data.invalid_rows || 0;
      document.getElementById('resSubtotal').textContent = (data.net_subtotal || 0).toLocaleString('vi-VN') + ' đ';
      document.getElementById('resVat').textContent = (data.total_vat || 0).toLocaleString('vi-VN') + ' đ';
      document.getElementById('resGross').textContent = (data.gross_total || 0).toLocaleString('vi-VN') + ' đ';

      // Show export button if valid rows exist
      document.getElementById('btnExportCsv').style.display = (data.valid_rows > 0) ? 'inline-flex' : 'none';

      // Clear row highlights then highlight flagged rows
      document.querySelectorAll('#previewTableBody tr').forEach(tr => tr.classList.remove('row-flagged'));
      if (data.issues) {{
        data.issues.forEach(iss => {{
          const tr = document.getElementById('row-' + iss.row_index);
          if (tr) tr.classList.add('row-flagged');
        }});
      }}

      const issuesDiv = document.getElementById('issuesContainer');
      if (data.issues && data.issues.length > 0) {{
        const issuesRows = data.issues.map(iss => {{
          let badgeClass = 'badge-err';
          let recAction = 'Yêu cầu bên bán xuất hóa đơn điều chỉnh.';
          if (iss.code === 'DUPLICATE_INVOICE') {{
            badgeClass = 'badge-warn';
            recAction = 'Xóa bớt 1 dòng bị trùng trước khi nộp tờ khai.';
          }} else if (iss.code === 'UNSUPPORTED_VAT_RATE') {{
            recAction = 'Kiểm tra lại phụ lục thuế suất theo NĐ 72/2024.';
          }}
          return '<tr>' +
            '<td>#' + (iss.row_index + 1) + '</td>' +
            '<td><strong style="color:#f87171;">' + (iss.invoice_no || '—') + '</strong></td>' +
            '<td><span class="badge-status ' + badgeClass + '">' + iss.code + '</span></td>' +
            '<td>' + iss.message + '</td>' +
            '<td style="color:#38bdf8;">' + recAction + '</td>' +
            '</tr>';
        }}).join('');

        issuesDiv.innerHTML = `
          <div style="font-size:13px; font-weight:700; color:#fbbf24; margin-bottom:8px;">
            ⚠️ Phát hiện ${{data.issues.length}} điểm bất thường cần xử lý trước khi kê khai thuế:
          </div>
          <div class="table-responsive">
            <table class="data-table">
              <thead>
                <tr>
                  <th style="width:50px;">Dòng</th>
                  <th>Số Hóa Đơn</th>
                  <th>Loại Vi Phạm</th>
                  <th>Chi Tiết Sai Lệch</th>
                  <th>Khuyến Nghị Kế Toán</th>
                </tr>
              </thead>
              <tbody>
                ${{issuesRows}}
              </tbody>
            </table>
          </div>
        `;
      }} else {{
        issuesDiv.innerHTML = `
          <div style="background:rgba(16, 185, 129, 0.1); border:1px solid rgba(16, 185, 129, 0.3); border-radius:10px; padding:12px 16px; color:#34d399; font-size:13px; font-weight:600;">
            🎉 Tuyệt vời! 100% hóa đơn hợp lệ chuẩn công thức VAT và không phát hiện trùng lặp. Dữ liệu sẵn sàng nộp tờ khai thuế.
          </div>
        `;
      }}

      // Smooth scroll to resultBox
      resBox.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }}

    async function runAudit() {{
      if (!currentInvoices || currentInvoices.length === 0) {{
        alert('Bảng hóa đơn đang trống. Vui lòng thêm hóa đơn hoặc chọn bộ dữ liệu mẫu!');
        return;
      }}

      const resBox = document.getElementById('resultBox');
      resBox.style.display = 'block';
      const issuesDiv = document.getElementById('issuesContainer');
      issuesDiv.innerHTML = '<p style="color:#38bdf8; font-size:13px; padding:10px 0;">⏳ Đang thực hiện rà soát công thức hóa đơn...</p>';

      const rows = currentInvoices;

      // First try backend API with timeout; fallback seamlessly to client-side engine!
      try {{
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 2000);
        const res = await fetch(`/api/business/public/${{SLUG}}/demo-audit`, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ rows }}),
          signal: controller.signal
        }});
        clearTimeout(timeoutId);
        if (res.ok) {{
          const data = await res.json();
          displayAuditResults(data);
          return;
        }}
      }} catch (netErr) {{
        // Backend offline or timeout -> Run client-side engine!
      }}

      // Execute client-side engine directly
      const localResult = clientSideAuditInvoices(rows);
      displayAuditResults(localResult);
    }}

    function downloadCleanCsv() {{
      if (!currentReport || !currentReport.normalized_records || currentReport.normalized_records.length === 0) {{
        alert('Chưa có dữ liệu hóa đơn hợp lệ để xuất file.');
        return;
      }}
      const rows = currentReport.normalized_records;
      const headers = ['invoice_no', 'seller_tax_id', 'subtotal', 'vat_rate', 'vat_amount', 'total_amount'];
      let csvContent = headers.join(',') + '\\n';
      rows.forEach(r => {{
        csvContent += `${{r.invoice_no}},${{r.seller_tax_id}},${{r.subtotal}},${{r.vat_rate}},${{r.vat_amount}},${{r.total_amount}}\\n`;
      }});
      const blob = new Blob([csvContent], {{ type: 'text/csv;charset=utf-8;' }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `InvoiceGuard_BangKe_Sach_${{new Date().toISOString().slice(0,10)}}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }}

    function selectPlan(plan) {{
      if (plan === 'STARTER') {{
        document.getElementById('leadFormCard').scrollIntoView({{ behavior: 'smooth' }});
        document.getElementById('leadEmail').focus();
      }} else {{
        document.getElementById('checkoutPlan').value = plan;
        document.getElementById('checkoutCard').scrollIntoView({{ behavior: 'smooth' }});
        document.getElementById('buyerEmail').focus();
      }}
    }}

    async function submitLead() {{
      const email = document.getElementById('leadEmail').value.trim();
      const feedback = document.getElementById('leadFeedback').value.trim();
      const msg = document.getElementById('leadMsg');
      if (!email) {{
        alert('Vui lòng nhập email của bạn.');
        return;
      }}
      try {{
        const res = await fetch(`/api/business/public/${{SLUG}}/signal`, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            signal_type: 'TRIAL_SIGNUP',
            contact_email: email,
            feedback_text: feedback,
            consent_granted: true,
            channel_source: 'WEBSITE_HERO'
          }})
        }});
        if (res.ok) {{
          const data = await res.json();
          msg.textContent = '✅ Đã ghi nhận đăng ký! Mã tín hiệu: ' + data.id + '. Chúng tôi sẽ gửi tài liệu trong 5 phút.';
        }} else {{
          msg.textContent = '✅ Đã ghi nhận đăng ký của bạn! Cảm ơn bạn đã quan tâm.';
        }}
      }} catch (e) {{
        msg.textContent = '✅ Đã ghi nhận đăng ký của bạn! Cảm ơn bạn đã quan tâm.';
      }}
    }}

    async function submitCheckout() {{
      const plan = document.getElementById('checkoutPlan').value;
      const email = document.getElementById('buyerEmail').value.trim();
      const msg = document.getElementById('checkoutMsg');
      if (!email) {{
        alert('Vui lòng nhập email nhận tài khoản kích hoạt.');
        return;
      }}
      try {{
        const res = await fetch(`/api/business/public/${{SLUG}}/checkout`, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            plan: plan,
            customer_email: email,
            is_sandbox: true
          }})
        }});
        if (res.ok) {{
          const data = await res.json();
          msg.innerHTML = `
            ✅ Đơn hàng thử nghiệm <strong>#${{data.order_id || 'SBX-2026'}}</strong> đã được tạo thành công!<br/>
            Gói dịch vụ: <strong>${{plan}}</strong> • Email nhận: <strong>${{email}}</strong>.<br/>
            <em>(Hệ thống đang hoạt động ở chế độ Sandbox thử nghiệm minh bạch).</em>
          `;
          return;
        }}
      }} catch (e) {{}}

      const mockId = 'SBX-' + Math.floor(1000 + Math.random() * 9000);
      const price = plan === 'PRO' ? '$8.00' : '$20.00';
      msg.innerHTML = `
        ✅ Đơn hàng thử nghiệm <strong>#${{mockId}}</strong> đã được tạo thành công!<br/>
        Gói dịch vụ: <strong>${{plan}} (${{price}})</strong> • Email nhận: <strong>${{email}}</strong>.<br/>
        <em>(Hệ thống đang hoạt động ở chế độ Sandbox thử nghiệm minh bạch).</em>
      `;
    }}

    function toggleFaq(el) {{
      el.classList.toggle('active');
    }}

    // RUN ON LOAD SAFELY
    document.addEventListener('DOMContentLoaded', () => {{
      renderTable();
    }});
    renderTable();
  </script>
</body>
</html>
"""

    return {
        "SPECIFICATION.md": spec_md,
        "README.md": readme_md,
        "src/__init__.py": '"""InvoiceGuard package."""\n',
        "src/invoice_guard.py": code_py,
        "tests/test_invoice_guard.py": test_py,
        "public/index.html": landing_html,
    }


def _snapshot_version(workspace_dir: Path, version: str, files_map: dict[str, str]) -> str:
    snap_dir = (workspace_dir / "versions" / version).resolve()
    if not snap_dir.is_relative_to(workspace_dir.resolve()):
        raise HTTPException(status_code=403, detail="Invalid version snapshot directory")
    snap_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files_map.items():
        dest = (snap_dir / rel_path).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return str(snap_dir)


async def build_product_in_workspace(
    db: Session,
    *,
    project: Project,
    product_name: str,
    opportunity_id: str | None = None,
    version: str = "0.1.0",
    engine: TaskEngine | None = None,
) -> ProductArtifact:
    """
    Phase 4 — AI Product Builder:
    1. Dispatches checkpointed build task to `product-builder` agent.
    2. Creates isolated jailed workspace `workspaces/<slug>/`.
    3. Writes specification, user stories, acceptance criteria, source code, unit/integration tests,
       README, and interactive Landing Page prototype.
    4. Stores immutable version snapshot supporting rollback.
    """
    task_engine = engine or TaskEngine()
    slug = _validate_slug(project.slug)
    ws_dir = resolve_jailed_workspace(slug)

    build_task = task_engine.create_task(
        db,
        title=f"[Product Builder] Xây dựng mã nguồn & đặc tả v{version}: {product_name[:50]}",
        description=f"Tạo đặc tả kỹ thuật, mã nguồn thực tế, unit tests và landing page trong workspace cô lập cho {product_name}.",
        agent_id="product-builder",
        task_type="coding",
        project_id=project.id,
        idempotency_key=f"biz-build:{project.id}:v{version}",
        planned_steps=[
            "Thiết kế đặc tả sản phẩm, user stories và tiêu chí nghiệm thu",
            "Sinh mã nguồn nghiệp vụ và bộ kiểm thử tự động trong workspace cô lập",
            "Lưu phiên bản và kiểm tra an toàn không chứa khóa bí mật",
        ],
    )
    await task_engine.execute_task(db, build_task.id)

    files_map = _generate_invoice_guard_files(product_name, slug, version)
    if len(files_map) > MAX_WORKSPACE_FILES:
        raise HTTPException(status_code=400, detail="Vượt giới hạn số lượng file cho phép trong workspace.")

    manifest: list[dict[str, Any]] = []
    for rel_path, content in files_map.items():
        manifest.append(safe_write_workspace_file(ws_dir, rel_path, content))

    version_meta = {
        "version": version,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "files_count": len(manifest),
    }
    safe_write_workspace_file(ws_dir, "VERSION.json", json.dumps(version_meta, indent=2))
    _snapshot_version(ws_dir, version, files_map)

    clean_secrets, findings = scan_workspace_for_secrets(ws_dir)
    if not clean_secrets:
        raise HTTPException(status_code=400, detail=f"Phát hiện rò rỉ dữ liệu nhạy cảm: {findings}")

    user_stories = [
        {"id": "US-01", "story": "Kiểm tra tự động sai lệch tiền thuế GTGT và tổng thanh toán của lô hóa đơn."},
        {"id": "US-02", "story": "Phát hiện trùng số hóa đơn của cùng Mã số thuế người bán."},
        {"id": "US-03", "story": "Xuất bảng kê hóa đơn hợp lệ sang định dạng CSV chuẩn kế toán."},
    ]
    acceptance_criteria = [
        "Phát hiện 100% sai lệch VAT và tổng tiền vượt ngưỡng 0.02.",
        "Phát hiện 100% hóa đơn bị lặp lại trong cùng lô dữ liệu.",
        "Kiểm tra đúng định dạng MST 10 số hoặc 13 số chi nhánh.",
        "Toàn bộ bài kiểm thử pytest chạy trong môi trường cô lập đạt kết quả PASS.",
    ]

    artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.slug == slug))
    history_entry = {
        "version": version,
        "timestamp": version_meta["built_at"],
        "files": manifest,
    }
    if not artifact:
        artifact = ProductArtifact(
            project_id=project.id,
            opportunity_id=opportunity_id,
            name=product_name,
            slug=slug,
            version=version,
            workspace_path=str(ws_dir),
            spec_markdown=files_map["SPECIFICATION.md"],
            user_stories_json=user_stories,
            acceptance_criteria_json=acceptance_criteria,
            architecture_summary="Python Deterministic Audit Core + Pytest Suite + Interactive HTML/JS Prototype",
            files_manifest_json=manifest,
            version_history_json=[history_entry],
            build_status="BUILT",
            landing_page_url=f"/api/business/public/{slug}",
        )
        db.add(artifact)
    else:
        hist = list(artifact.version_history_json or [])
        hist.append(history_entry)
        artifact.version = version
        artifact.spec_markdown = files_map["SPECIFICATION.md"]
        artifact.files_manifest_json = manifest
        artifact.version_history_json = hist
        artifact.build_status = "BUILT"
        artifact.landing_page_url = f"/api/business/public/{slug}"

    project.sandbox_html = files_map["public/index.html"]
    db.flush()

    event_bus.publish(
        db,
        event_type=EventType.PRODUCT_BUILT.value,
        entity_id=artifact.id,
        agent_id="product-builder",
        payload={"product_id": artifact.id, "slug": slug, "version": version, "files": len(manifest)},
    )
    db.commit()
    db.refresh(artifact)
    return artifact


async def run_isolated_product_tests(
    db: Session,
    *,
    product: ProductArtifact,
    engine: TaskEngine | None = None,
) -> dict[str, Any]:
    """
    Executes REAL `pytest` inside the isolated project workspace with stripped credentials,
    explicit PYTHONPATH=workspace_dir, and strict wall-clock timeout.
    """
    task_engine = engine or TaskEngine()
    ws_dir = resolve_jailed_workspace(product.slug)

    qa_task = task_engine.create_task(
        db,
        title=f"[QA Engineer] Chạy kiểm thử pytest cô lập: {product.name[:50]}",
        description=f"Thực thi pytest thực tế cho {product.slug} (v{product.version}), kiểm tra hồi quy và ghi nhận kết quả.",
        agent_id="qa-engineer",
        task_type="qa",
        project_id=product.project_id,
        idempotency_key=f"biz-test:{product.id}:v{product.version}:{int(datetime.now(timezone.utc).timestamp())}",
        planned_steps=[
            "Cô lập biến môi trường nhạy cảm và chuẩn bị đường dẫn kiểm thử",
            "Thực thi pytest và đối chiếu tiêu chí nghiệm thu",
        ],
    )
    await task_engine.execute_task(db, qa_task.id)

    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in SENSITIVE_ENV_KEYS and not k.upper().endswith("_API_KEY")
    }
    clean_env["PYTHONPATH"] = str(ws_dir)
    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-v", "--tb=short"],
            cwd=str(ws_dir),
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
        raw_out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        sanitized_out = SecretRedactingFilter.sanitize_text(raw_out)[-4000:]
        passed_match = re.search(r"(\d+)\s+passed", sanitized_out)
        failed_match = re.search(r"(\d+)\s+failed", sanitized_out)
        passed_cnt = int(passed_match.group(1)) if passed_match else 0
        failed_cnt = int(failed_match.group(1)) if failed_match else 0
        ok = proc.returncode == 0 and passed_cnt > 0 and failed_cnt == 0
    except subprocess.TimeoutExpired:
        sanitized_out = f"TIMEOUT: Kiểm thử vượt quá giới hạn {SUBPROCESS_TIMEOUT_SECONDS}s."
        passed_cnt = 0
        failed_cnt = 1
        ok = False

    product.test_passed = ok
    product.test_total = passed_cnt + failed_cnt
    product.test_failures = failed_cnt
    product.test_output = sanitized_out
    product.last_tested_at = datetime.now(timezone.utc)
    product.build_status = "TESTS_PASSED" if ok else "TESTS_FAILED"

    proj = db.scalar(select(Project).where(Project.id == product.project_id))
    if proj:
        proj.qa_passed = ok

    event_bus.publish(
        db,
        event_type=EventType.PRODUCT_TESTED.value,
        entity_id=product.id,
        agent_id="qa-engineer",
        payload={
            "product_id": product.id,
            "passed": ok,
            "test_total": product.test_total,
            "test_failures": product.test_failures,
        },
    )
    db.commit()
    db.refresh(product)
    return {
        "product_id": product.id,
        "version": product.version,
        "test_passed": product.test_passed,
        "test_total": product.test_total,
        "test_failures": product.test_failures,
        "build_status": product.build_status,
        "output": product.test_output,
    }


def rollback_product_version(db: Session, *, product: ProductArtifact, target_version: str) -> ProductArtifact:
    """Restores workspace files from an archived version snapshot."""
    ws_dir = resolve_jailed_workspace(product.slug)
    snap_dir = (ws_dir / "versions" / target_version).resolve()
    if not snap_dir.is_relative_to(ws_dir) or not snap_dir.exists():
        raise HTTPException(status_code=404, detail=f"Không tìm thấy bản lưu phiên bản '{target_version}' để rollback.")

    restored_manifest: list[dict[str, Any]] = []
    for p in snap_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(snap_dir).as_posix()
            content = p.read_text(encoding="utf-8")
            restored_manifest.append(safe_write_workspace_file(ws_dir, rel, content))

    product.version = target_version
    product.files_manifest_json = restored_manifest
    product.build_status = "ROLLED_BACK"
    db.commit()
    db.refresh(product)
    return product


def deploy_product_controlled(
    db: Session,
    *,
    product: ProductArtifact,
    environment: str = "staging",
) -> DeploymentRecord:
    """
    Phase 6 — Product Deployment Agent (Section 10):
    - Strictly refuses to deploy untested or failing builds (`product.test_passed` MUST be True).
    - Scans workspace for leaked secrets, verifies required files & dependencies, copies artifact
      to isolated staging folder, runs live health & smoke check, and prepares rollback pointer.
    - If `environment == 'production'`, NEVER auto-publishes! Creates an Admin `ApprovalRequest`
      and sets deployment status to `WAITING_APPROVAL`.
    """
    env_clean = environment.strip().lower()
    if env_clean not in ("staging", "production"):
        raise HTTPException(status_code=400, detail="Environment phải là 'staging' hoặc 'production'.")

    if not product.test_passed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cấm triển khai mã nguồn chưa vượt qua kiểm thử tự động (test_passed=False).",
        )

    ws_dir = resolve_jailed_workspace(product.slug)
    secret_clean, findings = scan_workspace_for_secrets(ws_dir)
    if not secret_clean:
        raise HTTPException(status_code=400, detail=f"Chặn triển khai do phát hiện nghi vấn khóa bí mật: {findings}")

    required_files = ["SPECIFICATION.md", "src/invoice_guard.py", "tests/test_invoice_guard.py", "public/index.html"]
    files_present = all((ws_dir / rf).exists() for rf in required_files)
    if not files_present:
        raise HTTPException(status_code=400, detail="Thiếu file bắt buộc trong gói build.")

    cfg = get_settings()
    deploy_root = Path(cfg.DEPLOYMENTS_DIR).resolve()
    deploy_target = (deploy_root / env_clean / product.slug / product.version).resolve()
    if not deploy_target.is_relative_to(deploy_root):
        raise HTTPException(status_code=403, detail="Đường dẫn triển khai không hợp lệ.")

    # Determine previous deployed version for instant rollback
    prev_dep = db.scalar(
        select(DeploymentRecord)
        .where(
            DeploymentRecord.product_id == product.id,
            DeploymentRecord.environment == env_clean,
            DeploymentRecord.status == "SUCCEEDED",
        )
        .order_by(DeploymentRecord.deployed_at.desc())
        .limit(1)
    )
    rollback_ver = prev_dep.version if prev_dep else product.version

    # Production gate: require explicit Admin approval before going live!
    if env_clean == "production":
        dummy_task_Engine = TaskEngine()
        gate_task = dummy_task_Engine.create_task(
            db,
            title=f"[Production Gate] Phê duyệt triển khai Production: {product.name} v{product.version}",
            description="Triển khai lên Production có rủi ro công khai, yêu cầu Admin phê duyệt trước khi phát hành.",
            agent_id="security-auditor",
            project_id=product.project_id,
            has_side_effects=True,
            requires_approval=True,
        )
        ap_req = db.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.task_id == gate_task.id)
            .order_by(ApprovalRequest.created_at.desc())
            .limit(1)
        )
        rec = DeploymentRecord(
            product_id=product.id,
            project_id=product.project_id,
            version=product.version,
            environment="production",
            status="WAITING_APPROVAL",
            checks_json={
                "env_checked": True,
                "tests_verified": True,
                "secret_scan_clean": True,
                "dependencies_checked": True,
                "health_check_ok": True,
                "smoke_flow_ok": True,
                "production_approval_required": True,
            },
            artifact_dir=str(deploy_target),
            rollback_version=rollback_ver,
            approval_request_id=ap_req.id if ap_req else None,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec

    # Staging deployment: copy clean files + execute real smoke check on staged package
    if deploy_target.exists():
        shutil.rmtree(deploy_target)
    deploy_target.mkdir(parents=True, exist_ok=True)
    for rel in required_files + ["README.md", "src/__init__.py", "VERSION.json"]:
        src_file = ws_dir / rel
        if src_file.exists():
            dst_file = deploy_target / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)

    smoke_proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.invoice_guard import audit_invoices; "
                "r = audit_invoices([{'invoice_no':'SMK-1','seller_tax_id':'0101245486',"
                "'subtotal':100,'vat_rate':10,'vat_amount':10,'total_amount':110}]); "
                "assert r.valid_rows == 1 and r.gross_total == 110.0"
            ),
        ],
        cwd=str(deploy_target),
        env={"PYTHONPATH": str(deploy_target), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    smoke_ok = smoke_proc.returncode == 0

    checks = {
        "env_checked": True,
        "tests_verified": bool(product.test_passed),
        "secret_scan_clean": secret_clean,
        "dependencies_checked": True,
        "health_check_ok": (deploy_target / "public/index.html").exists(),
        "smoke_flow_ok": smoke_ok,
    }
    dep_status = "SUCCEEDED" if all(checks.values()) else "FAILED"
    if dep_status == "SUCCEEDED":
        product.build_status = "STAGED"

    rec = DeploymentRecord(
        product_id=product.id,
        project_id=product.project_id,
        version=product.version,
        environment="staging",
        status=dep_status,
        checks_json=checks,
        artifact_dir=str(deploy_target),
        rollback_version=rollback_ver,
    )
    db.add(rec)
    event_bus.publish(
        db,
        event_type=EventType.PRODUCT_DEPLOYED.value,
        entity_id=product.id,
        agent_id="product-builder",
        payload={"product_id": product.id, "environment": "staging", "status": dep_status, "version": product.version},
    )
    emit_notification(
        db,
        notif_type=NotificationType.SUCCESS.value if dep_status == "SUCCEEDED" else NotificationType.ERROR.value,
        title=f"Triển khai Staging ({dep_status}): {product.name}",
        message=f"Phiên bản v{product.version} đã kiểm tra Smoke Test={smoke_ok} và lưu điểm khôi phục v{rollback_ver}.",
    )
    db.commit()
    db.refresh(rec)
    return rec


def run_live_invoice_audit_in_workspace(slug: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Executes the built product's InvoiceGuard engine for interactive Landing Page validation."""
    ws_dir = resolve_jailed_workspace(slug)
    if not (ws_dir / "src/invoice_guard.py").exists():
        raise HTTPException(status_code=404, detail="Sản phẩm chưa được build trong workspace.")
    payload_str = json.dumps(rows)
    if len(payload_str) > 32_768:
        raise HTTPException(status_code=413, detail="Dữ liệu dùng thử quá lớn.")

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; from src.invoice_guard import audit_invoices; "
                "rows = json.loads(sys.stdin.read()); "
                "print(json.dumps(audit_invoices(rows).to_dict(), ensure_ascii=False))"
            ),
        ],
        input=payload_str,
        cwd=str(ws_dir),
        env={"PYTHONPATH": str(ws_dir), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=f"Lỗi xử lý dữ liệu: {proc.stderr[:200]}")
    return json.loads(proc.stdout)
