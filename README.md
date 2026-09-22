# 医疗机构与厂商会谈的智能纪要与证据问答系统

上传会谈录音 → 后台转写 → 结构化纪要(人工确认) → 基于权限的证据问答。
虚构业务与模拟数据,用于求职展示的完整工程项目。

## 技术栈

- 后端:Python 3.11 + FastAPI + SQLAlchemy 2 + Alembic
- 数据库:PostgreSQL 16 + pgvector(Docker,无 SQLite 降级路径)
- 后台任务:独立 worker 进程 + 数据库任务表 + 租约(fencing)机制
- 前端:React + Vite(Phase 1 起)
- 模型接入:Provider 接口隔离 ASR/LLM/Embedding,默认 mock 实现,密钥走环境变量

## 环境要求

- Python 3.11+
- Docker Desktop(需 daemon 运行中)
- Git

## 启动步骤

```powershell
# 1. 数据库(中文目录名需显式指定项目名 -p mes)
docker compose -p mes up -d
# 等待健康检查通过
docker inspect --format '{{.State.Health.Status}}' mes-pg   # 应输出 healthy

# 2. Python 虚拟环境与依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. 配置(可选,默认值即可本地运行;真实密钥只放 .env,已被 .gitignore 排除)
Copy-Item .env.example .env

# 4. 数据库迁移
.\.venv\Scripts\python.exe -m alembic upgrade head

# 5. 冒烟测试(含真实连接 PG 的集成测试)
.\.venv\Scripts\python.exe -m pytest -v

# 6. 启动 API
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 健康检查(两类检查刻意分离)

| 端点 | 用途 | 依赖 |
|---|---|---|
| `GET /health` | 进程存活(liveness),永不触碰数据库 | 无 |
| `GET /health/ready` | 就绪(readiness),实际执行 `SELECT 1` | PostgreSQL 运行中 |

## 当前状态(Phase 0,如实记录)

已完成并验证:
- 仓库骨架、依赖锁定(requirements.txt)
- docker-compose 定义 PG16+pgvector,容器健康检查通过
- Alembic 初始迁移(启用 pgvector 扩展)已应用
- `/health` 与 `/health/ready` 端点,5 项冒烟测试全部通过
- uvicorn 真实进程启动并验证两端点响应

尚未实现(后续阶段):
- 业务表、API、worker、前端、评测(见 docs/DESIGN.md 规划)

## 已知限制与环境备注

1. **Docker Hub 直连超时**:本项目环境需经镜像加速拉取,已验证
   `docker pull docker.m.daocloud.io/pgvector/pgvector:pg16` 可用后 tag 回原名。
2. **中文目录名**:docker compose 无法推断项目名,所有 compose 命令需带 `-p mes`。
3. **alembic.ini 必须保持 ASCII**:alembic 以 locale 编码(GBK)读取该文件,
   中文注释会导致 UnicodeDecodeError;中文说明写在 env.py / 迁移文件中。
4. Windows 控制台运行 Python 工具链时如遇编码问题,先执行 `$env:PYTHONUTF8=1`。
