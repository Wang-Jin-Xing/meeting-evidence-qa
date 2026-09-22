# 交接说明(HANDOFF)

面向接手的人:怎么跑起来、当前做到哪、哪些结论能说什么不能说、下一步该干什么。
所有"已完成"都以仓库内可运行的测试/脚本为证,不以文档声明为证。

## 1. 一分钟启动

```powershell
docker compose -p mes up -d                       # PG16 + pgvector
Copy-Item .env.example .env                       # 必填 TEST_DATABASE_URL(独立测试库)
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe pytest -q              # 集成测试(独立测试库)
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8100
# 另开终端:
.\.venv\Scripts\python.exe -m app.worker
# 浏览器: http://127.0.0.1:8100/
```

端口 8000/8080 在本机已被其他项目占用,故示例统一用 **8100**。

## 2. 账号与演示数据初始化

```powershell
.\.venv\Scripts\python.exe demo/seed_demo.py --reset
# 真实 LLM 组合:
$env:LLM_PROVIDER="deepseek"; .\.venv\Scripts\python.exe demo/seed_demo.py --reset
```

脚本会:清空业务表 → 建 3 个机构与演示账号 → 建 2 场会谈 → 上传
`demo/media/*.wav`(2 分钟静音占位)→ 只跑切片阶段 → 导入人工转写 →
用配置指定的 LLM 生成纪要 → 建索引 → 逐个问题打印答案与证据引用,
并打印两类越权访问的拒绝结果。

| 账号 | 机构 | 能看到 |
|---|---|---|
| `demo_admin_a` / `demo_staff_a` | 演示-市第一人民医院 | 正常案例(org 可见) |
| `demo_admin_c` / `demo_staff_c` | 演示-康泰儿童医院 | 边界案例(restricted;staff 非白名单 → 404) |
| `demo_outsider` | 演示-外部医疗机构 | 什么都看不到(404,不区分无权/不存在) |

密码默认 `demo-pass-123`,可用环境变量 `DEMO_PASSWORD` 覆盖。
演示数据不需要手工录入:界面里"人工转写导入"框粘贴 `demo/seed_demo.py` 中的
台词即可(行数须等于音频切片数,每 10 秒 1 行)。

## 3. 必须保持的不变量(改代码前先看这几条)

1. **证据链**:原始录音 → 版本化转写片段(`transcript_segment`,落库后不可变)
   → 纪要/回答引用 `segment_id`。纠错是**新增版本** + `segment_mapping`,
   永不改写旧版本片段,否则历史引用失效。
2. **双发布指针**:`active_transcript_version_id`(转写已发布)与
   `active_search_version_id`(索引就绪)分开;检索只认后者,
   且同时要求 `is_active=true` → 半成品永不可见。
3. **业务提交与阶段状态同事务**,事务内先锁 job 行、用数据库
   `clock_timestamp()` 校验 epoch 与租约有效期;模型调用一律在事务外。
   旧 epoch 的 worker 不得提交(但可能仍在产生外部调用——这是能力边界)。
4. **外部调用四态留痕**:调用意图(model_run 前置 unknown)/ 我方发出
   (Provider 入口调用记录)/ 远端结果(succeeded / failed / 保留 unknown)/
   业务提交。**不保证只调用一次或不重复计费**。
5. **成本三态**:`cost_basis` = estimated / provider_reported / unknown;
   `cost_estimate` 为 NULL 是"未知"不是 0。预算在每次调用发出前原子预留,
   结果未知的调用按预估保守计为已用。mock 调用恒为 unknown 且不占预算。
6. **权限在 SQL 层**:检索候选同时受"URL 会谈 id + org_id 等值 + 可见会谈子查询"
   三重限制;无权与不存在统一 404。机构归属只从 JWT → 数据库解析,
   请求体里带 `org_id` 直接 422。
7. **模型选择不静默降级**:未知 Provider 名 / 缺凭证 / 向量维度不符 → 直接报错。
   新增真实适配器时在 `app/ai/registry.py` 注册并声明 `requires`。

## 4. 当前真实模型组合与解锁条件

| 环节 | 现状 | 解锁条件 |
|---|---|---|
| LLM | **真实** DeepSeek `deepseek-chat`(密钥来自环境变量 `DEEPSEEK_API_KEY`) | 已可用 |
| Embedding | mock(字符 bigram 哈希,1024 维) | 任一 OpenAI 兼容 embeddings 服务商可用余额;`BAAI/bge-m3` 维度 1024 与向量列一致,可直接接 |
| ASR | mock + 人工转写导入 | 服务商可用余额;需实现 `ASRProvider` 适配器(按 10 秒分片调用) |

2026-09-23 实测:SiliconFlow 的 ASR 与 Embedding 接口返回
`402 account balance insufficient`(账户余额不足),故本轮未接入。
**在真实 Embedding 接入前,任何"检索效果提升"的结论都不成立**;
当前能说的是:纪要与问答的语义判断由真实模型完成,权限/引用/恢复/记账链路正确。

## 5. 已完成(有测试或脚本证据)

- P0 环境与迁移、双健康检查;P1 业务表/JWT/会谈与录音 API/四阶段 worker/
  结构化纪要/会谈内证据问答;P2 租约回收接管、故障注入系统测试;
  P2 收尾(入口调用记录、结果未知分类、调用超时与阶段期限、退避与调度守卫)
- P3 首版:Provider 工厂、费用三态与调用前预算预留、人工转写导入、
  评测框架(dev/test 按会谈划分 + 越权探针 + 自描述报告)、最小界面
- 测试:独立测试库上 `pytest -q` 全绿(数量见 `docs/` 与提交说明);
  含真实子进程 worker 的故障注入与并发接管测试
- 评测:`python eval/run_eval.py` 可跑;报告含 git 工作区脏标记与内容指纹、
  实际写入库名、模型/提示词版本、检索参数、数据集 sha256、费用汇总、每题时延;
  mock 基线下 D-32 的型号错配反例确实以误报形式出现(如实记录)

## 6. 未完成 / 明确不做

- 真实 ASR、真实 Embedding 与真实基线数字(等服务商可用)
- 纪要人工确认/编辑流转、原始音频下载与回放、转写版本列表/片段查看、
  历史纪要版本内容、问答审计查询等入口(清单见 `docs/PERMISSIONS.md`)
- 检索策略优化(阈值/top-k/重排/混合检索):基线未建立前不动
- 多智能体、微调、知识图谱、复杂管理后台:不在范围内

## 7. 已知坑(踩过,别再踩)

1. `TEST_DATABASE_URL` 缺失时测试**直接退出**(不会清应用库);测试库带会话独占锁,
   并发跑第二个 pytest 会拒绝启动。
2. `alembic.ini` 只能 ASCII(GBK 读取);中文说明写在迁移文件里。
3. 中文目录名导致 compose 需要 `-p mes`;Windows 工具链统一 `PYTHONUTF8=1`。
4. `app/core/db.py` 在导入时就创建 engine,任何"切换数据库配置"的代码
   必须在导入它之前执行(`tests/conftest.py` 顶部即为此设计)。
5. 调用超时用独立线程兜底,Python 无法强杀线程:超时只保证任务不再阻塞,
   不保证远端已停止;真实 Provider 必须自带网络超时。
6. `transcript_version.source_type` 等枚举列在库里是 VARCHAR 且**无 CHECK 约束**,
   取值只靠应用层枚举;要库级约束需单独迁移。
7. worker 子进程若把 stderr 丢弃,流水线异常只会表现为"等待状态超时";
   测试与 `demo/` 脚本都已把 stderr 落盘(`.pytest_worker_logs/`)。
8. 真实 LLM 输出不总满足纪要空值策略:适配器只**向更保守方向**归一
   (缺 owner/due_date → `needs_confirmation=true`),绝不补造事实;
   越界证据 id 不静默剔除,仍按协议违规处理。

## 8. 交接物清单

| 内容 | 位置 |
|---|---|
| 代码与文档 | Git 仓库(提交见下);`git bundle` 或私有远端 |
| 演示录音 | `demo/media/case_normal.wav`、`case_boundary.wav`(静音占位,2 分钟) |
| 可重建数据脚本 | `demo/seed_demo.py`(`--reset` 幂等重建) |
| 数据库导出 | `demo/db_export/meeting_evidence_demo.sql`(含演示数据;`pg_dump`) |
| 上传原件 | `storage/uploads/`(不入库,`.gitignore` 排除;需与导出配套保存) |
| 评测报告 | `eval/reports/*.json`(运行时产物,`.gitignore` 排除;可重跑生成) |
| 密钥 | 只在 `.env` / 环境变量,**不进 Git**(`.env.example` 是模板) |

恢复演示数据(不依赖导出文件):直接跑 `demo/seed_demo.py --reset`。
需要精确复现当时状态时:还原 `demo/db_export/*.sql` + 把 `storage/uploads/`
放回原位(路径记在 `recording.file_path`)。
