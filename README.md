# AI BUSINESS SIMULATOR 2.5D
### Autonomous AI Company Management System

Hệ thống điều hành doanh nghiệp số vận hành bằng đa tác tử AI (**Multi-Agent AI Company**) thông qua giao diện thế giới game quản trị **2.5D Isometric** kết nối trực tiếp với **Backend thực tế** (FastAPI + SQLAlchemy + WebSocket + Encrypted Credential Vault + Free-First Model Router).

---

## 1. Điểm Nổi Bật Của Hệ Thống

- **Thế giới Game 2.5D Isometric thời gian thực (`Company World`):**
  - 7 khu vực chức năng: **Command Center**, **AI Research Lab**, **AI Development Lab**, **Marketing Studio**, **API Control Room**, **Finance Office**, **Memory Archive**.
  - 8 nhân vật AI Agent có hoạt ảnh di chuyển, làm việc, chờ nhiệm vụ, cảnh báo lỗi; hỗ trợ kéo bản đồ (pan), phóng to/thu nhỏ (zoom), chọn phòng và chọn từng nhân vật để hội thoại trực tiếp.
- **API Provider Manager & Tự động chuyển Model (`FREE_FIRST` / `FREE_ONLY`):**
  - Hỗ trợ adapter nhà cung cấp thật (**Google Gemini API**, **OpenAI-Compatible / Groq / OpenRouter / Ollama**) cùng **Configurable Mock Provider** để diễn tập và kiểm thử lỗi 429, hết quota, sai API key, timeout mà không tốn token thật.
  - Tự động phát hiện lỗi hạn mức, chuyển sang model dự phòng đủ năng lực, phát sự kiện `PROVIDER_SWITCHED` lên thế giới game và khôi phục thực thi từ `Checkpoint` mà không lặp lại bước có tác dụng phụ (`side-effect idempotency`).
- **Bảo mật Khóa & Tách biệt Tài khoản Google:**
  - Khóa API được mã hóa đối xứng bằng `Fernet` (`CredentialVault`) với khóa mã hóa nằm ngoài cơ sở dữ liệu; che một phần khóa trên giao diện và tự động lọc khỏi log/event.
  - Phân biệt rõ **Google OAuth 2.0** (chỉ xác thực danh tính người dùng) với **AI Provider API Key** và **Service OAuth Credential**. Không thu thập mật khẩu Gmail hay đọc hộp thư.
- **An toàn Vận hành & Kiểm soát Quyền:**
  - 3 cấp độ tự động hóa (`LEVEL_1_OBSERVE`, `LEVEL_2_SANDBOX`, `LEVEL_3_CONTROLLED_PROD`), hàng đợi phê duyệt (`Approval Workflow`), nhật ký kiểm toán (`Audit Log`), phân quyền RBAC (`admin`, `operator`, `viewer`) và nút **EMERGENCY STOP**.

---

## 2. Cấu Trúc Thư Mục

```text
ai-business-simulator/
├── backend/                  # FastAPI + SQLAlchemy 2.0 + Alembic + Provider Router + Task Engine
│   ├── alembic/              # Cấu hình & migration cơ sở dữ liệu
│   ├── app/
│   │   ├── agents/           # 8 AI Agent chuyên biệt & hội thoại bền vững
│   │   ├── analytics/        # Báo cáo hệ thống, API token/chi phí & tài chính minh bạch
│   │   ├── api/              # REST API endpoints & WebSocket /ws/events
│   │   ├── core/             # Config, logging chống lộ secret, database session
│   │   ├── events/           # Event Bus thời gian thực & truy vấn đồng bộ lại
│   │   ├── models/           # 18+ bảng SQLAlchemy lưu trữ dài hạn
│   │   ├── providers/        # AIProvider, Gemini, OpenAI-Compatible, Mock, Router, QuotaTracker
│   │   ├── schemas/          # Pydantic v2 schemas
│   │   ├── security/         # CredentialVault (Fernet), JWT RBAC, Rate Limiter
│   │   ├── services/         # Seed khởi tạo & Business MVP Sandbox Workflow
│   │   └── tasks/            # Task Engine, Checkpoint trung lập & Idempotency
│   └── pyproject.toml
├── frontend/                 # React + TypeScript + Vite + Isometric 2.5D Engine + Tailwind + Recharts
│   ├── src/
│   │   ├── api/              # HTTP client & WebSocket reconnect/replay client
│   │   ├── components/       # Agent Drawer, Room Inspector, Notification Toast, Modals
│   │   ├── game/             # Isometric 2.5D World Renderer (7 rooms + 8 animated agents)
│   │   ├── pages/            # CompanyWorld, ApiControlRoom, OperationsDashboard
│   │   ├── stores/           # Zustand store đồng bộ backend
│   │   └── types/            # TypeScript contracts khớp backend
│   └── package.json
├── tests/                    # Bộ kiểm thử tự động Backend (>=18 kịch bản bắt buộc)
├── docs/                     # Hướng dẫn Backup/Khôi phục & Tài liệu Bảo mật/Giới hạn
├── .env.example              # Mẫu biến môi trường an toàn (không chứa secret)
├── docker-compose.yml        # Cấu hình PostgreSQL + Backend + Frontend cho triển khai lâu dài
├── ARCHITECTURE.md           # Tài liệu kiến trúc chi tiết
├── IMPLEMENTATION_PLAN.md    # Kế hoạch triển khai theo 6 Milestone
└── PROGRESS.md               # Nhật ký tiến độ và bằng chứng kiểm thử
```

---

## 3. Hướng Dẫn Chạy Nhanh Trên Máy Cá Nhân (Local Development)

### Yêu cầu
- **Python quản lý qua `uv`** (đã cài sẵn `uv` trên máy — tự động tải Python 3.12).
- **Node.js >= 20** (trên Windows PowerShell gọi qua `npm.cmd` nếu `ExecutionPolicy` chặn `.ps1`).

### Bước 1: Chuẩn bị file cấu hình `.env`
```powershell
Copy-Item "d:\ai-business-simulator\.env.example" "d:\ai-business-simulator\.env"
```

### Bước 2: Khởi động Backend (Cổng `8000`)
```powershell
# Từ thư mục d:\ai-business-simulator\backend
uv sync --dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
- API Docs (Swagger UI): `http://127.0.0.1:8000/docs`
- Health Check: `http://127.0.0.1:8000/api/health`

### Bước 3: Khởi động Frontend 2.5D (Cổng `5173`)
```powershell
# Từ thư mục d:\ai-business-simulator\frontend
npm.cmd install
npm.cmd run dev
```
Mở trình duyệt tại: **`http://localhost:5173`**

### Tài khoản Local Mặc định (cho môi trường phát triển)
- **Admin:** `admin@simulator.local` / `Admin@123456!` (Toàn quyền cấu hình API Key, Router, Emergency Stop, Phê duyệt)
- **Operator:** `operator@simulator.local` / `Operator@123456!` (Giao task, hội thoại với Agent, tạm dừng/tiếp tục task)
- **Viewer:** `viewer@simulator.local` / `Viewer@123456!` (Chỉ quan sát bản đồ 2.5D và báo cáo, bị chặn quyền quản trị)

---

## 4. Chạy Kiểm Thử Tự Động

### Kiểm thử Backend (18+ kịch bản bắt buộc dùng Mock Provider, không tốn token thật)
```powershell
# Từ thư mục d:\ai-business-simulator\backend
uv run pytest ../tests -v
```

### Kiểm thử & Build Frontend
```powershell
# Từ thư mục d:\ai-business-simulator\frontend
npm.cmd test -- --run
npm.cmd run build
```

---

## 5. Kết Nối API Thật (Gemini / Groq / OpenRouter)

1. Đăng nhập tài khoản **Admin** trên giao diện web.
2. Mở màn hình **API Control Room** (hoặc nhấp vào phòng **API Control Room** trên bản đồ 2.5D).
3. Chọn nhà cung cấp thật đã tích hợp sẵn (`Google Gemini` hoặc `OpenRouter / Groq OpenAI-Compatible`), nhập API Key được cấp phép vào mục **Add Credential**.
4. Nhấn **Test Connection** để kiểm tra kết nối thật.
5. Chọn chế độ định tuyến `FREE_FIRST` hoặc `FREE_ONLY` để ưu tiên các model miễn phí và tự động chuyển đổi khi hết quota.
