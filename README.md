# 医疗机构与厂商会谈的智能纪要与证据问答系统

上传会谈录音 → 后台转写 → 结构化纪要(人工确认) → 基于权限的证据问答。
业务场景与演示语料是虚构的,但**工程链路是真的**:独立 worker 进程、租约接管、版本化证据链、
真实模型(faster-whisper / DeepSeek / bge-m3,本机本地推理 + 真实外部调用)与一条真实会议录音
都已跑通,并留下可复现工件。用于求职展示。

**三类模型都在产品路径上,不是只有一类**:本地 ASR(faster-whisper)+ 本地 Embedding
(bge-m3)+ 远程 LLM(DeepSeek),`model_run` 表截至 2026-09-25 累计 **368 次真实调用**
(逐项拆解与自数命令见下文「当前真实模型组合」;该数字**会随调用增长**,以命令实数为准)。
测试与评测**默认走 mock** 是为了不花钱、可复现,别把它误读成"项目没接模型"。

## 文档地图

| 想知道 | 看哪里 |
|---|---|
| 怎么起、怎么测、怎么演示 | 本文(启动步骤 / 演示 / 健康检查) |
| 架构、数据流、任务状态机、权限判定图 | `docs/ARCHITECTURE.md` |
| 每个接口的权限规则与尚未实现的入口 | `docs/PERMISSIONS.md` |
| 为什么这样设计、踩过哪些坑(编号 D-xx) | `docs/DECISIONS.md` |
| 3~5 分钟演示脚本(含现场故障预案) | `docs/DEMO_SCRIPT.md` |
| 接手这个项目先要知道什么 | `docs/HANDOFF.md` |
| 能力清单、关键取舍、真实实验数字、失败案例 | `PROJECT_SUMMARY.md` |
| 面试问答与可当场展示的证据 | `INTERVIEW.md`、`docs/RESUME.md` |
| 评测数据集与运行方式 | `eval/`(`python eval/run_eval.py`) |
| 当前基线:五类分开的质量/耗时/成本口径与失败案例 | `docs/EVAL_BASELINE.md` |

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

端口:由**命令行**决定,`scripts/dev_up.ps1 -Port 8100` 默认 **8100**(它把值传给
`uvicorn --host/--port`;`.env` 里不再放端口,因为没有任何代码读取它,原先的
`APP_PORT` 字段是无效配置,已删除)。若 8100 已被本机其他应用占用,换一个空闲端口即可,
用 `curl http://127.0.0.1:<port>/health/ready` 确认起的是本项目(响应里
`database: up`,首页标题为"会谈纪要与证据问答")。API 与 worker 是**两个独立进程**,
模型组合与阈值一律以仓库根 `.env` 为准(终端里显式 `$env:` 设的同名变量优先级更高,
只用于一次性覆盖,见下节),新开终端无需任何设置。

在**管道化的父进程**里调用(CI、脚本捕获输出)时,请给一个不挂起的 stdin,
否则脚本已打印完结果、调用方仍会一直等 EOF:API/worker 是常驻进程,会带着继承来的
管道句柄活着。交互式 PowerShell 无此问题;自动化侧用
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev_up.ps1 < /dev/null`,
或改为后台启动后自行轮询 `/health/ready`。

## 演示:数据重建与真实模型组合

```powershell
# 重建两组虚构会谈(正常案例 / 边界案例)+ 演示账号,并逐个问题打印引用结果。
# 模型组合默认取自 .env(本机已验证的真实组合),无需设环境变量。
.\.venv\Scripts\python.exe demo/seed_demo.py --reset
#   ⚠ --reset 是 TRUNCATE 全部业务表。库里若存在**非演示**会谈(例如真实录音链路、
#   入口验收、A/B 对照那几条不可重造的验收数据),它会直接拒绝并列出条数与样例;
#   确实要清空:先备份,再加 --force。要在不动那些数据的前提下演示,
#   直接用已有会谈即可(见 docs/DEMO_SCRIPT.md 最后一节)。

# 仅当要复现"全 mock"版本时才临时覆盖;$env: 只对**当前这个终端会话**里的
# 后续命令生效,新开终端不继承 —— 因此长期配置一律写 .env,不要靠 $env: 记状态。
# 阈值必须跟着 Embedding 模型走:mock bigram 的校准值是 0.15,沿用 .env 的 0.66
# 会让 mock 检索全部落在阈值以下,看起来"没有引用"其实是配置错配(见 D-48)。
$env:ASR_PROVIDER="mock"; $env:LLM_PROVIDER="mock"; $env:EMBED_PROVIDER="mock"
$env:QA_EVIDENCE_THRESHOLD="0.15"
.\.venv\Scripts\python.exe demo/seed_demo.py --reset
Remove-Item Env:ASR_PROVIDER,Env:LLM_PROVIDER,Env:EMBED_PROVIDER,Env:QA_EVIDENCE_THRESHOLD
```

演示账号(密码可用环境变量 `DEMO_PASSWORD` 覆盖,默认见 `demo/seed_demo.py`):

| 账号 | 机构 | 用途 |
|---|---|---|
| `demo_admin_a` / `demo_staff_a` | 演示-市第一人民医院 | 正常案例(负责人/日期/待办明确) |
| `demo_admin_c` / `demo_staff_c` | 演示-康泰儿童医院 | 边界案例(无明确日期 + 型号混淆,`restricted`) |
| `demo_outsider` | 演示-外部医疗机构 | 越权演示:详情与提问均 404 |

### 当前真实模型组合(2026-09-25 实测,三个环节都是真模型)

| 环节 | 生效实现 | 位置 | 实测调用 |
|---|---|---|---|
| ASR(转写) | faster-whisper small/int8 | **本机本地推理** | 55 次 |
| Embedding(检索) | BAAI/bge-m3,1024 维 | **本机本地推理** | 182 次(问答 156 + 建索引 26) |
| LLM(纪要 + 问答) | DeepSeek `deepseek-chat` | 远程 API | 131 次(问答 105 + 纪要 26) |

"实测调用"是 `model_run` 表里 `is_mock=false` 的记录条数,自数一次即可:

```bash
docker exec mes-pg psql -U mes -d meeting_evidence -c \
  "select provider, model_name, stage, count(*) from model_run \
   where is_mock=false group by 1,2,3 order by 4 desc;"
```

**关于 mock**:`pytest -q` 与 `eval/run_eval.py` **默认走 mock**
(跑测试与评测不花钱、可复现),所以仓库里 mock 的出现频率远高于真模型 ——
mock 是**测试/评测的默认**,不是产品路径的默认。运行中的服务读仓库根 `.env`,
`/meta` 会回显实际生效的 Provider。

两点如实保留:① LLM 未配单价表 → `cost_basis=unknown`(金额 NULL,**不按 0 记账**);
② `deepseek-chat` 是官方公告 2026-07-24 停用的**旧别名**,当前仍可成功调用,
但已不在官方价目表内(见 `docs/DECISIONS.md` D-71)。

### 真实录音端到端(本地 ASR + 本地 Embedding + DeepSeek)

```powershell
# 模型组合与阈值已在 .env 中配好(whisper / deepseek / bge-m3 / 0.66),
# 无需在终端设任何 $env: 变量;权重走 hf-mirror。
.\.venv\Scripts\python.exe demo/ingest_real_recording.py --audio "C:/.../会议录音.wav"     --questions demo/questions_real_meeting.json
```

脚本会:上传**原始文件**(24-bit/48k/立体声已实测支持)→ 进程内跑四阶段
(真实 whisper,预算 300s)→ 逐问问答 → 把 ASR 原始输出(含错误,不修正)存
`demo/corpus/<名>.asr.json`,报告存 `demo/verification/real_audio_<名>.json`
(含音频参数、各阶段耗时与设备、引用与相似度、成本口径)。
加 `--corrected 校对稿.json` 会另建一个隔离会谈发布校对版本做双链路对比,
主展示会谈保持原始 ASR 不被替换。**跑之前要先停常驻 worker**
(`scripts/dev_up.ps1` 起的两个进程):脚本自己领取任务,被抢到会直接报错退出。

双链路实测(`--corrected demo/corpus/会议录音.corrected.json`,会谈 #7 vs #8,
模型/提示词/问题/阈值 0.66 全固定;逐处校对依据见 `会议录音.校对说明.md`):
校对把 5 题的最高相似度**全部**抬升(+0.031~+0.127),并把 A 侧答案里的错字
("张公傅责")修成正确文本("张工负责")—— 这一层是 **ASR 错误的影响**。
但"什么时间之前要补齐""保修时间确定了吗"两问校对后**仍**判证据不足,
最高相似度 0.5681 / **0.6566** 都低于 0.66,而排序第一名恰是应引用的那片:
这两问失败的原因要改判为**阈值相对短疑问句偏高**,不再是 ASR(详见 D-59)。
本轮按指示**不调阈值**,连同"泛化问句召回为 0"一起留作下一阶段待解案例。
以上均为单条录音、5 个问题,不构成整体准确率。

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
  人工转写导入、纪要与证据引用、**引用按切片回放**、问答与证据展示、
  纪要**按版本确认**与"编辑为新版";不含管理后台
- 独立测试库(D-39):`TEST_DATABASE_URL` 必填并以 `current_database()` 复核;
  测试库会话独占锁(并发跑第二个会话直接拒绝启动)
- 证据回放(D-61):`GET /recordings/{id}/audio` 带权限与单区间 Range;
  24/32-bit 原件按需降位成 16-bit **派生副本**(原件只读,副本帧数/采样率/声道与原件校验一致),
  `X-Audio-*` 响应头区分"送出的字节"与"原件";`storage/` 不在任何静态挂载下
- 纪要人工流转(D-62):编辑追加 `human_edit` 版本(记操作者/时间/来源版本),
  确认写在版本行上(`confirmed_by/confirmed_at`),编辑后需重新确认,
  基于陈旧版本的编辑返回 409 而非静默覆盖

当前状态补充(测试数量以最近一次运行为准):`pytest -q` → **141 passed / 3 skipped**;
3 条 skipped 是真实 Provider 集成测试,默认跳过是为了不让每次全量测试都加载 2.3GB 权重。
**开关必须限定在那个文件上**:`MEET_TESTS_USE_REAL_PROVIDERS=1 pytest tests/test_real_embedding.py -q`
→ 实测 **3 passed**。**不要加在整套上** —— 那会让 `tests/conftest.py` 不再把 Provider 钉回
mock,整套改用 `.env` 的真实组合与 0.66 阈值,为 mock 口径造的夹具会大面积**假失败**
(2026-09-25 实测:整套加开关 → 32 failed / 96 passed / 15 errors)。详见 `docs/HANDOFF.md` §10。

尚未实现 / 不成立:
- 云端真实 ASR/Embedding 服务商不可用(SiliconFlow 实测 402 余额不足;DeepSeek 无该类接口,
  已探测确认),**已改用本地真实模型跑通全链路**,不再是"真实 ASR 未启用"
- 短疑问句在 0.66 阈值下召回不足(校对后最高相似度 0.6566,差 0.0034)、
  泛化问句候选为 0 —— 是当前最大的已知质量问题,按指示未调阈值,见 D-59
- 转写版本列表/片段单独查看、问答审计(query_log)查询入口、全局跨会谈问答、
  音频下载入口(回放接口已可取字节)尚未实现,见 docs/PERMISSIONS.md
- 评测集规模(dev/test 各 1 场会谈、共 12 题)与真实链路(1 条录音、5 问)
  只够流程验证与逐题归因,**不足以支撑效果或准确率结论**

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
9. **两套演示数据的音频性质不同**:`demo/seed_demo.py` 造的 case_normal /
   case_boundary 是**静音占位**(时间轴来自切片边界,文本由人工导入提供,
   不是"识别出来的");真实语音只在 `会议录音.wav` 那条链路上
   (`demo/ingest_real_recording.py` 或网页上传),其文本是 faster-whisper 实际输出,
   **含未修正的识别错误**。界面上的"转写来源"标签按数据库事实区分这两者。
10. **`model_run.tokens_in` 只在远程 LLM 行上有 token 含义**:本地 ASR/Embedding
   行自 D-58 起写 NULL(此前误把音频字节数、批内条数写进该列)。**历史行不回填**,
   所以跨行汇总必须先按 `device`/`model_name` 排除本地行,否则会把"288 万字节"
   当成"288 万 token"。

