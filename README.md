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

# 3. 配置(必须项:测试库;其余默认值即可本地运行,真实密钥只放 .env,已被 .gitignore 排除)
Copy-Item .env.example .env
#    编辑 .env,确认 TEST_DATABASE_URL 指向独立测试库(库名不得与 DATABASE_URL 相同)。
#    未配置 TEST_DATABASE_URL 时 pytest 直接退出,不会清空任何数据库。

# 4. 数据库迁移(应用库)
.\.venv\Scripts\python.exe -m alembic upgrade head

# 5. 冒烟测试(真实连接 PostgreSQL 的集成测试)
#    测试自行完成:建测试库(若不存在)→ 迁移测试库 → 校验 current_database()
#    → 仅对测试库 TRUNCATE;worker 子进程继承同一测试库连接。
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

P1.3(已完成并验证,51/51 测试通过):
- 检索问答 POST /meetings/{id}/ask:pgvector 余弦相似度检索;候选 SQL 显式限定
  URL 会谈 meeting_id + 权限双注入(org_id 等值 + 可见会谈子查询,不信任单一
  冗余键)+ 发布状态双保险(is_active 且 version=所属录音 active_search_version,
  误标 active 的未发布版本不可检索)
- 证据充分性两层判定(均为 mock 启发式,不宣称真实充分性校验):
  相关性阈值(0.15)+ 焦点线索规则(负责人/时间点/时长线索缺失即不足);
  证据不足时明确返回(无回答、无引用),不拼接候选冒充答案
- 引用内嵌片段原文/说话人/时间位置,且必须来自权限过滤后的候选(越权引用 500);
  mock 回答为抽取式(只由候选原文拼接);每次问答写 query_log 审计(含证据不足)
- 纪要查看 GET /meetings/{id}/minutes:最新版本完整内容 + 版本摘要;
  条目证据按 segment_id 解析为内嵌片段详情(限本会谈录音,防跨会谈探测)
- 测试 17 项(含同机构会谈隔离、误标 active 版本不可检索、相关但缺焦点线索
  → 证据不足);修正 test_recordings"零模型调用"断言为限定本 job(原全表
  断言依赖测试顺序)

P2(已完成并验证,58/58 测试通过,含 7 项故障注入系统测试):
- 恢复与故障验证(真实 PG + worker 子进程注入,每项同时核对 DB 状态、
  产物数量、mock 调用记录):
  分片部分成功后杀进程→接管只处理未完成片段;模型已响应未提交→记录
  unknown 并重发(不宣称避免重复调用/计费);阶段产物已提交→恢复复用
  (ASR 零重调、版本不重建);旧 epoch 续租/提交被拒;索引部分中断→
  旧版本仍可检索、完成后原子切换且历史引用仍可解析;双 worker 竞争
  回收→单接管者、产物唯一
- 模型调用两阶段记录(unknown 前置):结果未知调用如实留痕;
  model_run 行=物理调用事实,idempotency_key 唯一约束改普通索引
- 修复两处 P1.2 缺陷:MEET_WORKER_* 环境变量从未生效(无 env_prefix);
  run_index 切换范围 meeting 级→本录音旧版本(多录音会谈互不踢除)
- 检索日志口径分离:raw_retrieved_count / relevant_candidate_count
- 权限矩阵:docs/PERMISSIONS.md(已实现入口+未实现入口如实列出)

P2 收尾(已完成并验证,见 docs/DECISIONS.md D-33~D-37):
- Provider 入口调用记录(`MEET_MOCK_REQUEST_LOG`,本地 JSONL):与 model_run
  完全独立的第四层留痕,用于核对**物理请求发出次数**。定性如实:它能证明
  mock 被调用,不能证明真实服务商收到请求(真实接入需按请求 ID/账单核对)
- 调用结果四态:调用意图(model_run 前置 unknown)/ 结果未知(超时、断连,
  保留 unknown)/ 明确失败(failed)/ 成功(succeeded),不混淆
- 三层时间约束:单调用超时(默认 60s)+ 单阶段期限(默认 300s,超期不再发出
  新调用)+ 租约 TTL;心跳存活不再等于任务有进展
- 重试退避(`next_run_at`,按 attempt 递增封顶)与调度守卫前移到 claim:
  重试上限、退避时刻、组织费用预算三项参与候选过滤
- 恢复复用断言加强:索引中断恢复用例同时核对 model_run 状态计数与
  入口调用批大小(已提交分片复用、不重发)

尚未实现:
- 检索/纪要质量评测(dev/test 区分,须覆盖 D-32 反例集:日期归属误判、
  bigram 碰撞噪声)
- 前端(Phase 1 起)、纪要人工确认/编辑流转、原始音频下载、转写版本/片段查看、
  历史纪要版本内容等入口(见 docs/PERMISSIONS.md 未实现清单)
- 真实模型 Provider 适配器(当前全部 mock,效果指标不代表真实能力)

## 已知限制与环境备注

1. **Docker Hub 直连超时**:本项目环境需经镜像加速拉取,已验证
   `docker pull docker.m.daocloud.io/pgvector/pgvector:pg16` 可用后 tag 回原名。
2. **中文目录名**:docker compose 无法推断项目名,所有 compose 命令需带 `-p mes`。
3. **alembic.ini 必须保持 ASCII**:alembic 以 locale 编码(GBK)读取该文件,
   中文注释会导致 UnicodeDecodeError;中文说明写在 env.py / 迁移文件中。
4. Windows 控制台运行 Python 工具链时如遇编码问题,先执行 `$env:PYTHONUTF8=1`。
5. **测试库与应用库分离**:`TEST_DATABASE_URL` 必填,测试启动时会以
   `current_database()` 复核并拒绝在与应用库同名的库上运行(见 D-39)。
   应用库(`meeting_evidence`)不再被测试清表,其中历史测试数据需自行清理。
