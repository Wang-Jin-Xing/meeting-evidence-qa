# 医疗机构与厂商会谈的智能纪要与证据问答系统

上传会谈录音 → 后台转写 → 结构化纪要(人工确认) → 基于权限的证据问答。
虚构业务与模拟数据,用于求职展示的完整工程项目。

## 技术栈

- 后端:Python 3.11 + FastAPI + SQLAlchemy 2 + Alembic
- 数据库:PostgreSQL 16 + pgvector(Docker,无 SQLite 降级路径)
- 后台任务:独立 worker 进程 + 数据库任务表 + 租约(fencing)机制
- 界面:`app/web/` 原生 HTML/CSS/JS 静态页(无构建步骤,由 FastAPI 挂载在 `/`)
- 模型接入:Provider 接口隔离 ASR/LLM/Embedding,按配置选择且**不静默回退 mock**;
  默认 mock,真实密钥只走环境变量

## 启动步骤

**虚拟环境位置**:仓库根目录下的 `.venv`。下文命令一律写全路径,不依赖
"激活后才是某个 python"的隐式状态:

- PowerShell:`.\.venv\Scripts\python.exe`
- Git Bash / CMD:`.venv\Scripts\python.exe`
- 若你的 venv 建在别处,把下面的 `.venv` 整体替换即可(本项目未使用激活脚本)。

**配置只有一个来源:仓库根的 `.env`**(被 .gitignore 排除)。
API、worker、评测与演示脚本都读同一份 `.env`,因此**新开终端不需要再 `$env:` 设置**;
临时实验才用环境变量覆盖(优先级高于 .env)。

```powershell
# 1. 数据库(中文目录名需显式指定项目名 -p mes)
docker compose -p mes up -d
docker inspect --format '{{.State.Health.Status}}' mes-pg   # 应输出 healthy

# 2. 虚拟环境与依赖(用系统 python 创建,之后一律用 venv 内的解释器)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. 配置
Copy-Item .env.example .env
#    必查:TEST_DATABASE_URL 指向独立测试库(库名不得与 DATABASE_URL 相同,
#    否则 pytest 直接退出,不清任何库);
#    模型组合与阈值见 .env.example 注释(默认已填本机验证过的真实组合)。
#    密钥(如 DEEPSEEK_API_KEY)放用户级环境变量或 .env,不进 Git。

# 4. 迁移应用库
.\.venv\Scripts\python.exe -m alembic upgrade head

# 5. 集成测试(独立测试库;首次会自动建库并迁移)
.\.venv\Scripts\python.exe -m pytest -q

# 6. 一键起 API + 独立 worker,并打印两侧实际生效的模型组合(不输出密钥)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev_up.ps1 -Port 8100

# 7. 打开界面
start http://127.0.0.1:8100/
```

`scripts/dev_up.ps1` 会:停掉本项目的旧 API/worker → 分别起两个进程 →
等待 readiness → 打印 worker 启动横幅与登录后 `/meta` 的生效值,
用于核对"网页上传→持久化任务→独立 worker→结果展示"两侧配置一致。

## 演示:数据重建与真实模型组合

```powershell
# 重建两组虚构演示会谈(正常案例 / 边界案例)+ 演示账号,并逐个问题打印引用结果
.\.venv\Scripts\python.exe demo/seed_demo.py --reset                 # 全 mock 组合
$env:LLM_PROVIDER="deepseek"; .\.venv\Scripts\python.exe demo/seed_demo.py --reset   # 真实 LLM 组合
```

演示账号(密码可用环境变量 `DEMO_PASSWORD` 覆盖,默认见 `demo/seed_demo.py`):

| 账号 | 机构 | 用途 |
|---|---|---|
| `demo_admin_a` / `demo_staff_a` | 演示-市第一人民医院 | 正常案例(负责人/日期/待办明确) |
| `demo_admin_c` / `demo_staff_c` | 演示-康泰儿童医院 | 边界案例(无明确日期 + 型号混淆,`restricted`) |
| `demo_outsider` | 演示-外部医疗机构 | 越权演示:详情与提问均 404 |

### 当前真实模型组合(如实说明,2026-09-23)

| 环节 | 生效实现 | 说明 |
|---|---|---|
| LLM(纪要 + 问答) | **真实** DeepSeek `deepseek-chat` | 返回 tokens 为服务商口径;金额未配单价表 → 成本记 `unknown` |
| Embedding(检索) | **mock**(字符 bigram) | SiliconFlow `BAAI/bge-m3` 返回 402 账户余额不足,真实检索未接入 |
| ASR(转写) | **mock** + 人工转写导入 | SiliconFlow `SenseVoiceSmall` 同样 402;演示以人工转写为权威文本来源 |

### 真实录音端到端(本地 ASR + 本地 Embedding + DeepSeek)

```powershell
$env:ASR_PROVIDER="whisper"; $env:EMBED_PROVIDER="bge-m3"; $env:LLM_PROVIDER="deepseek"
$env:QA_EVIDENCE_THRESHOLD="0.66"; $env:HF_ENDPOINT="https://hf-mirror.com"
.\.venv\Scripts\python.exe demo/ingest_real_recording.py --audio "C:/.../会议录音.wav"     --questions demo/questions_real_meeting.json
```

脚本会:上传**原始文件**(24-bit/48k/立体声已实测支持)→ 进程内跑四阶段
(真实 whisper,预算 300s)→ 逐问问答 → 把 ASR 原始输出(含错误,不修正)存
`demo/corpus/<名>.asr.json`,报告存 `demo/verification/real_audio_<名>.json`
(含音频参数、各阶段耗时与设备、引用与相似度、成本口径)。
加 `--corrected 校对稿.json` 会另建一个隔离会谈发布校对版本做双链路对比,
主展示会谈保持原始 ASR 不被替换。

首轮实测(单样本,不代表整体准确率):识别错误如"布齐/北迅时间/没有报告"
原样保留;实体问句 3/5 命中,2 问因识别错误导致证据不足(详见 D-55)。

启用真实 Embedding(本地,无需任何密钥与余额):把 `.env` 中
`EMBED_PROVIDER=bge-m3`、`LLM_PROVIDER=deepseek`、`QA_EVIDENCE_THRESHOLD=0.66`、
`HF_ENDPOINT=https://hf-mirror.com` 配好(huggingface.co 直连在本机超时),然后:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # 含 torch-cpu / sentence-transformers
.\.venv\Scripts\python.exe demo/verify_real_embedding.py        # 维度核实 + 索引重建 + 五项验证
```

阈值为什么必须换:mock bigram 上"无关句"相似度约 0.06,而 bge-m3 上
**无关句就有 0.39~0.55**、金标句 0.71~0.82;沿用 0.15 等于所有候选全部通过。
`0.66` 是在 **dev 会谈**上按"最小金标相似度 − 0.05"取的,标定脚本与分布
记录在 `demo/verification/real_embedding.json`(不用 test 集调参,D-48)。

因此:**纪要、问答与检索向量都是真实模型;但样本只有 2 场会谈,**
验证结论只说明那些具体断言成立,不外推为整体准确率。
`demo/media/*.wav` 是 2 分钟静音占位录音(时长决定切片数即时间轴);
真实录音可直接替换后重新上传。

## 环境要求

- Python 3.11+
- Docker Desktop(需 daemon 运行中)
- Git
- 可选:真实 LLM 的 `DEEPSEEK_API_KEY`(仅经环境变量注入,不提交)


## 健康检查(两类检查刻意分离)

| 端点 | 用途 | 依赖 |
|---|---|---|
| `GET /health` | 进程存活(liveness),永不触碰数据库 | 无 |
| `GET /health/ready` | 就绪(readiness),实际执行 `SELECT 1` | PostgreSQL 运行中 |

## 当前状态(如实记录)

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

P3 首版(可演示、可复现、可交接):
- 配置驱动的 Provider 工厂(D-43):未注册名 / 缺凭证 / 向量维度不符分别显式报错,
  **不静默回退 mock**;`GET /meta` 返回实际生效组合,界面右上角如实标注
- 真实 LLM 闭环(D-45):DeepSeek 生成结构化纪要与证据问答回答;
  边界案例上真实模型能拒绝把 Y500 的报价归给 Y300(mock 则会误答,已实测对比)
- 人工转写导入(D-44):`POST /meetings/{id}/transcript` → 无版本时建
  `human_transcription` 首版,有版本时追加 `human_correction` + 逐条映射;
  时间位置仍只来自音频切片,行数与切片数不符即拒绝
- 费用三态与调用前预算预留(D-42/D-38):`cost_basis` =
  estimated / provider_reported / unknown,`cost_estimate` 为 NULL 表示未知而非 0;
  预算在**每次外部调用发出前**原子预留,长任务不会在领取后突破预算;
  问答与离线导入同样记账,没有免费旁路
- 评测框架(D-40/D-41):数据集按会谈划分 dev/test,金标含型号错配/否定/
  日期归属/无答案/越权五类;`--transcript human_corrected` 可先隔离 ASR 误差;
  报告记录 git rev + **工作区脏标记与内容指纹**、实际写入的数据库、
  模型/提示词版本、检索参数、数据集 sha256、费用汇总与每题时延
  - 运行:`python eval/run_eval.py [--split dev|test|all] [--top-k N] [--threshold F]`
- 最小界面(`app/web/`,无构建步骤):登录、会谈列表与新建、录音上传与四阶段进度、
  人工转写导入、纪要与证据引用、问答与证据展示;不含管理后台
- 独立测试库(D-39):`TEST_DATABASE_URL` 必填并以 `current_database()` 复核;
  测试库会话独占锁(并发跑第二个会话直接拒绝启动)

尚未实现 / 不成立:
- 真实 ASR 未启用(云端余额 402;本地 faster-whisper 已接入但实测中文专名
  错译明显,见 D-50);真实 Embedding 已用本地 bge-m3 接入
- 更大规模的评测集与真实基线数字(当前 dev/test 各 1 场会谈,只够流程验证)
- 纪要人工确认/编辑流转、原始音频下载与回放、转写版本列表/片段单独查看、
  历史纪要版本内容查看、问答审计查询等入口(见 docs/PERMISSIONS.md 未实现清单)
- 评测集规模(dev/test 各 1 场会谈)只够流程冒烟,不足以支撑效果结论

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
6. **测试库不可并发**:两个 pytest 会话会互相 TRUNCATE,故加了会话独占锁;
   第二个会话会直接拒绝启动(等前一个结束,或另配一个测试库名)。
7. **枚举列无数据库 CHECK 约束**:迁移以 `native_enum=False` 建列时未生成 CHECK,
   取值仅由应用层枚举保证(如 `transcript_version.source_type`);
   后续如要库级约束需单独迁移。
8. **外部调用不保证只执行一次**:超时/断连记为结果未知(unknown)并按预估
   保守计入预算,不宣称避免重复调用或重复计费。
9. **演示录音为静音占位**:音频内容不影响链路(时间轴来自切片边界),
   转写文本由人工导入提供;真实录音需在真实 ASR 接入后才有意义。

