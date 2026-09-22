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

## 当前状态(Phase 1 进行中,如实记录)

Phase 0(已完成并验证):
- 仓库骨架、PG16+pgvector 容器、Alembic 初始迁移、双健康检查端点、5 项冒烟测试

P1.1(已完成并验证,23/23 测试通过):
- 全量业务表迁移(14 张:机构/用户/会谈/白名单/录音/任务/阶段/模型调用/转写版本/片段/映射/向量块/纪要版本/问答日志)
- JWT 登录(/auth/login、/auth/me);机构归属一律由后端身份解析
- 会谈创建/列表/详情(visibility=org|restricted,白名单限本机构用户)
- 录音上传:流式落盘+SHA-256 查重(409)、创建 queued 任务与 4 个 pending 阶段,请求内零模型调用
- 任务状态查询 GET /jobs/{id}(访问范围跟随会谈)
- 跨机构隔离:会谈/任务/上传均 404;无权与不存在不区分

P1.2(已完成并验证,e2e 全部经真实子进程 worker):
- 租约机制:原子领取(epoch+1,Skip Locked)、心跳续租(仅当前 epoch 且未过期)、
  短事务提交守卫(锁行 + clock_timestamp 校验,旧 epoch/过期租约拒绝提交)
- 四阶段流水线:prepare(WAV 切片,边界即时间来源)→ transcribe(分片级断点以 DB 为准,
  成功片复用)→ minutes(规则 mock + 层1校验)→ index(分批 is_active=false,全部完成后原子切换)
- 双版本指针:active_transcript_version 转写发布;active_search_version 索引就绪,
  检索只依据后者,半成品不可见
- mock Provider 确定性输出 + 故障注入/延迟(环境变量);model_run 全量记录(is_mock 标识,
  结果缓存按 job 隔离,幂等键不含 attempt)
- 验收测试:独立进程完成流水线、租约三态拒绝、并发领取唯一、分片重试不重复调用、
  中途失败不暴露半成品、重复执行无重复产物

尚未实现:
- P1.3 检索问答 API、前端、评测;P2 崩溃恢复系统测试(回收路径代码已有)、重试退避

## 已知限制与环境备注

1. **Docker Hub 直连超时**:本项目环境需经镜像加速拉取,已验证
   `docker pull docker.m.daocloud.io/pgvector/pgvector:pg16` 可用后 tag 回原名。
2. **中文目录名**:docker compose 无法推断项目名,所有 compose 命令需带 `-p mes`。
3. **alembic.ini 必须保持 ASCII**:alembic 以 locale 编码(GBK)读取该文件,
   中文注释会导致 UnicodeDecodeError;中文说明写在 env.py / 迁移文件中。
4. Windows 控制台运行 Python 工具链时如遇编码问题,先执行 `$env:PYTHONUTF8=1`。
