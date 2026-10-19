# 交接说明(HANDOFF)

面向接手的人:怎么跑起来、当前做到哪、哪些结论能说什么不能说、下一步该干什么。
所有"已完成"都以仓库内可运行的测试/脚本为证,不以文档声明为证。

## 0. 项目来源与知识产权口径(先读这条)

- 业务形态来自作者在一家医疗信息化公司的一段短期实习;
  **仓库内代码为作者独立实现,未使用该公司代码**,经其确认。
- 语料全部为**虚构数据**;唯一的真实录音由作者本人提供,**不入仓库、不入 SQL 导出、
  不部署公网**(`storage/` 已被 .gitignore 排除,`git ls-files storage/` 为 0)。
- 因此本项目可作为个人作品公开投递与演示。
- **当前这份是脱敏公开版**:真实录音的转写工件已从全部历史移除,公司名与本地
  绝对路径已替换,自查项与命中数见 `docs/PUBLICATION.md`。
  完整版(含校对稿与原始 A/B 报告)只在作者本地,数值与本仓库一致。

### 0b. 脱敏公开版(2026-09-24 已构建)

- 同一项目有**两个仓库**:母本(含真实语料工件,继续开发只在这里)与公开版
  `〔公开版路径〕`(推 GitHub 只从它推)。若你手上这份是公开版,母本里那些真实
  语料工件**不在**这里 —— 清单与原因见 `docs/PUBLICATION.md`。
- 公开版不是手工删出来的,是**投影**:`git-filter-repo --invert-paths` 从全部历史删掉
  5 个真实语料工件(`demo/corpus/会议录音.{asr,corrected}.json`、
  `会议录音.{待确认,校对说明}.md`、`demo/verification/real_audio_会议录音.json`),
  再用 `--replace-text` 全历史替换真实整句转录、公司名、含用户名的绝对路径。
  规则文件与脚本都在 `D:\desens\`。
- 替换作用于**全部历史**,不是"最新提交里删掉";投影产物 `git fsck --unreachable` 为 0。
  母本另有备份:`〔母本路径〕_完整历史备份_含真实语料.bundle` + 附注标签 `pre-public-freeze`。
- 公开版比母本多一个**发布层**,由脚本重放生成:`real_audio_AB_summary.json`
  (逐题阈值前相似度、Δ、耗时、用量、成本口径,文本字段占位)、`docs/PUBLICATION.md`、
  以及把指向已删工件的引用改指摘要(规则见 `D:\desens\fix_refs.py`)。
- 推送前有闸门 `D:\desens\gate_scan.py`:母本转写正文的任一字段值出现在公开版即拒绝。
  它做过反向验证:塞进一句真实转录 → 退出码 1;删掉 → 通过。
  公开版 `pytest -q` → 141 passed / 3 skipped,与母本一致。
- 未决:① 是否加 LICENSE(不加即默认版权,考虑来源口径反而更稳);
  ② 投影提交沿用仓库提交者身份(邮箱会随提交公开,`git log` 里就能看到,所以不写在这里),
  要不要换成 GitHub 的 noreply 邮箱(要换得在首次推送前再投影一次)。

### 0c. 以后改了代码怎么同步到公开版

```powershell
# 只同步 + 过闸门,不推:
powershell -NoProfile -ExecutionPolicy Bypass -File D:\desens\publish_sync.ps1
# 确认无误后再加 -Push:
powershell -NoProfile -ExecutionPolicy Bypass -File D:\desens\publish_sync.ps1 -Push
```

它做六步:重新投影母本 → 把投影的文件树盖到公开版(不动它的 `.git`/`.env`/`storage`)→
重放发布层 → 过闸门 → **追加**一个"同步母本 <sha>"提交 → 加 `-Push` 才推送。
公开版历史只追加、不重写,所以永远不需要 force push。若你在公开版手工改过东西,
`fix_refs.py` 会因为"原文命中 0 次"停下 —— 把那份改动挪回母本再同步,别在公开版里直接编辑。

**纪律**:母本里凡是真实录音衍生出的新工件,一律落在 `demo/corpus/` 或
`demo/verification/real_audio_*.json` 这两个已被投影规则覆盖的位置;放到别处就得先往
`D:\desens\drop-paths.txt` 补一行,否则闸门以外的路径可能漏出去。

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

**与常驻 worker 并存时的行为**(实测,不是 bug):seed 只跑切片阶段后导入人工转写,
若此时有 worker 在跑,它稍后会领取同一个任务并补跑纪要/索引阶段——
对同一转写版本、用同一套配置的模型,因此会多出第二个纪要版本(界面显示最新版),
不会覆盖人工转写、也不会产生 mock 内容污染。想让演示库严格只有一个纪要版本,
先停 worker 再跑 seed。

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

| 环节 | 现状 | 备注 |
|---|---|---|
| LLM | **真实** DeepSeek `deepseek-chat`(密钥来自环境变量 `DEEPSEEK_API_KEY`) | 2026-09-25 实测仍有成功调用(14:27Z,2 次);tokens 为服务商口径,金额未配单价表 → `unknown`。**注意**:该别名官方公告 2026-07-24 停用,且已不在当前官方价目表内,见 **D-71** |
| Embedding | **真实** 本地 `BAAI/bge-m3`(`EMBED_PROVIDER=bge-m3`) | 权重经 hf-mirror 下载;实测 1024 维 = 向量列;换模型必须重校准阈值(D-48) |
| ASR | **真实** 本地 faster-whisper small/int8(`ASR_PROVIDER=whisper`) | 已在一段真实录音(立体声/24-bit/48k/28.86s)上端到端验证;识别错误原样保留(D-55);静音占位演示会谈仍标注为占位 |

2026-09-23 实测:SiliconFlow 的 ASR 与 Embedding 接口返回
`402 account balance insufficient`(账户余额不足),故本轮未接入。
**真实 Embedding 已接入(本地 bge-m3)**,但样本只有 2 场会谈:
`demo/verify_real_embedding.py` 的 5 项断言全部通过、dev 的 4 个金标问题
top1 均命中证据句(相似度 0.71~0.82,无答案探针最高 0.55)——
这些是**具体断言成立**,不是整体准确率或召回率。

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
- P3 真实链路:**本地真实模型全链路已跑通** —— faster-whisper small/int8 转写真实录音
  (24-bit/48k/立体声)、DeepSeek 纪要与问答、本地 bge-m3 索引(维度 1024 与列一致),
  阈值按 bge-m3 重校准为 0.66(D-48);识别错误原样入库不修补(D-55)
- P3 双链路对比:以人工确认的 6 处校对为金标跑 A(原始 ASR)/B(校对)隔离对比,
  5 问最高相似度全部抬升,两问仍不足的原因**改判为阈值**而非 ASR(D-59);
  数值在 `demo/verification/real_audio_AB_summary.json`(脱敏摘要)
- 应用入口验收:网页上传 → 持久化任务 → **独立 worker 进程**四阶段 → 界面展示,
  含重复上传幂等实测;工件 `demo/verification/entry_acceptance_job6.json`
- 证据回放:`GET /recordings/{id}/audio`(鉴权 + Range;24-bit 走 16-bit 派生副本、
  原件只读、副本帧数校验),界面按引用切片范围播放并到点自停(D-61)
- 纪要人工流转:编辑追加 human_edit 版本(AI 原版不改写)、确认写在版本行上、
  编辑后需重新确认、陈旧基版本 409 防静默覆盖(D-62)

## 6. 未完成 / 明确不做

- 云端真实 ASR/Embedding 服务商(SiliconFlow 实测 402 余额不足、DeepSeek 无该接口):
  已用**本地真实模型**替代,不再等云;剩余缺口是"更多真实样本"而不是"服务商可用"
- 尚未实现:转写版本列表/片段单独查看、问答审计(query_log)查询入口、
  全局跨会谈问答、原始音频的界面内下载入口(回放接口已可取字节)
- 检索策略优化:短疑问句在 0.66 下召回不足是**当前最大的已知质量问题**(差 0.0034,
  见 D-59),但按指示未在正式配置上调阈值;泛化问句召回为 0 同样留作待解案例
- 多智能体、微调、知识图谱、复杂管理后台:不在范围内

## 7. 已知坑(踩过,别再踩)
- **`.env` 的值不进 `os.environ`**:pydantic 只填 Settings 对象,凡子依赖
  (huggingface_hub 等)只认环境变量的配置,必须在代码里显式 `setdefault` 应用,
  否则静默回落(D-56/D-60)。
- **`HF_HOME` 不设会把系统盘写满**:权重 4.8GB 默认落 `C:\Users\...\.cache`,
  本机因此只剩 8GB(同期 Docker 引擎退出两次,**日志无磁盘满记录,不是磁盘造成的**);
  `scripts/dev_up.ps1` 之外也要保证 `.env` 有值。
- **`model_run.tokens_in` 只对远程 LLM 有 token 含义**:本地 ASR/Embedding 行自 D-58
  起为 NULL,历史行仍是字节/条数,跨行汇总前先排除本地行,否则数字完全没意义。
- **改了判定规则必须回头重跑基线**:型号归属闸的弱版本(型号出现在任一引用里即可)
  在冻结 dev 的 mock 基线上误报依旧是 1,因为同一段语料里另一句确实提到了该型号。
  只有重跑才发现,见 D-65。
- **Docker 后端会自己退出,别误判原因**:本会话内遇到两次(`mes-pg` 变 Exited 255,测试连不上库)。
  查过日志:**没有 ENOSPC/磁盘满的记录**,而是 `Docker Desktop.exe.log` 里 9/11 起反复出现的
  `backend process exited: exit status 0x40010004`(本机既有模式,不是本项目造成的)。
  我曾把这条写成"被 C 盘压力压掉线",那是未证实的因果,已更正。
  恢复顺序:`docker start mes-pg` → 等 healthy → `scripts/dev_up.ps1`。
- **`<audio>` 发不出 Authorization 头**:别为此把 JWT 塞进 URL(会进浏览器历史与
  访问日志);界面用带鉴权的 fetch 取 blob 播放。
- **供应商模型别名会"过了停用日期还继续服务",且已从官方价目表里消失**(D-71):
  本项目调 `deepseek-chat`,官方 2026-04-24 公告其 **2026-07-24 停用**,但
  2026-09-25 实测仍能成功调用;而当前官方价格页只列 `deepseek-flash` /
  `deepseek-v4-pro`,**没有 `deepseek-chat`**。后果:单价表**填不了** ——
  填任何数字都是猜(违反纪律 1),`cost_basis` 只能停在 `unknown`。
  别因为"今天还能用"就以为它稳定,也别为了把 `unknown` 变好看而编个价。
- **前端轮询不能对"已结束"的任务再起轮询**(D-70):`pollJob()` 会立即执行一次 `tick()`,
  而 `tick()` 在终态分支里又调 `renderRecordings()`,后者又对**所有**录音调 `pollJob()` ——
  于是无界自我刷新,表现为详情页持续跳动(实测 300ms 500+ 次请求)。
  凡"轮询 + 完成后刷新"的写法都要防这条自触发路径;判据只留一份(`TERMINAL_JOB`),
  别在"停止轮询"和"是否重新起轮询"两处各写一遍条件。

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
| 真实 Embedding 验证工件 | `demo/verification/real_embedding.json`(维度、索引重建不变量、dev 相似度分布、五项验证、model_run 台账) |
| 真实音频链路工件 | `demo/verification/real_audio_AB_summary.json`(A=原始 ASR / B=人工校对 双链路逐题对比 + **阈值前相似度**,文本字段已脱敏)+ `demo/questions_real_meeting.json`。原始报告与 `demo/corpus/会议录音.*`(ASR 原始输出 / 金标校对稿 / 逐处依据)含真实会议内容,公开版已从全部历史删除,见 `docs/PUBLICATION.md` |
| 应用库备份 | `backups/meeting_evidence_before_real_audio.sql`(加真实录音数据前) |
| 应用入口验收工件 | `demo/verification/entry_acceptance_job6.json`(网页上传→持久化任务→**独立 worker** 四阶段→界面展示;两侧生效模型、阶段完成间隔、model_run 台账、界面原文引用与不可测项口径) |
| 冷启动验收工件 | `demo/verification/cold_start_acceptance.json`(进程与容器全停→拉起全流程、Range/416/来源头实测、纪要两版本与引用 recording_id 复核) |
| 语音合成脚本(ASR 备用) | `demo/make_demo_audio.py`(本机 TTS 合成 12 句 × 10s;本轮不用于演示) |
| 交付文档 | `PROJECT_SUMMARY.md`、`INTERVIEW.md`、`docs/RESUME.md`、`docs/ARCHITECTURE.md`(四张图)、`docs/DEMO_SCRIPT.md` |
| 密钥 | 只在 `.env` / 环境变量,**不进 Git**(`.env.example` 是模板) |

恢复演示数据(不依赖导出文件):直接跑 `demo/seed_demo.py --reset`。
需要精确复现当时状态时:

```powershell
docker exec mes-pg psql -U mes -d postgres -c "CREATE DATABASE meeting_evidence_restore OWNER mes"
docker exec -i mes-pg psql -U mes -d meeting_evidence_restore -q < demo/db_export/meeting_evidence_demo.sql
```

导出已实测可还原(2 会谈 / 2 录音 / 2 转写版本 / 24 向量块 / 2 纪要版本 /
19 条模型调用记录,中文与向量均完整);`recording.file_path` 指向的原始音频
在 `storage/uploads/`,需与导出配套放回。

**该导出不含真实录音那几条会谈**(#4 主展示 / #5 入口验收 / #6 入口验收复跑),
也没有把真实录音放进仓库:一旦提交就等于把原始语音与其中的日期、金额写进版本库
(公开版连它的转写工件一起删,见 `docs/PUBLICATION.md`)。复现这几条要原件在手
(sha256 前 16 位 `207e189720932974`,8311724 字节,立体声 / 24-bit / 48kHz),
再跑 `python demo/ingest_real_recording.py`(或按 `entry_acceptance_job6.json`
里记的入口路径在网页上传)。工件里只留转写文本、相似度与台账数字。

## 9. 实测评测记录(2026-09-23,可复跑)

`eval/reports_sample/` 三份报告(同一 dev/test 数据集,各 6 题 + 权限探针):

| 组合 | 无答案题正确拒答 | 无答案误报 | 证据命中 | 回答命中 | 平均时延 | 权限探针 |
|---|---|---|---|---|---|---|
| mock LLM + mock 检索(**2026-09-23,归属闸之前**) | dev 0.5 / test 0.5 | 各 1 例 | 1.0 | 1.0 | ~60ms | 5/5 |
| **DeepSeek LLM** + mock 检索(2026-09-23) | dev 1.0 / test 1.0 | 0 例 | dev 0.75 / test 1.0 | dev 0.75 / test 1.0 | 650–830ms | 5/5 |
| mock LLM + mock 检索(**2026-09-25,归属闸 + D-69 之后**) | **dev 1.0 / test 1.0** | **0 例** | 1.0 | 1.0 | dev 51.5ms / test 41.6ms | dev 3/3、test 2/2 |

**更正留痕(2026-09-25)**:第一行是**归属闸之前**的基线。重跑冻结集发现 test 的
`test_neg_model_mismatch` 在归属闸上线后**仍然误报**(0.5)—— 因为 D-65 的闸在"金额/数量"
类问题上是空转的:问题点名的型号代号**自带数字**,"Y300" 就满足了金额类线索的 `\d` 判据
(见 D-69)。修复后 dev/test 误报都归零、可答题命中率不变。改前/改后报告留档在
`eval/reports_sample/mock_baseline_devtest_before_d69_20260925.json` /
`..._after_d69.json`。

能说的:换真实 LLM 后 dev/test 的型号错配/日期归属误报都归零,代价是时延上升一个量级;
但**这两例后来在 mock 路径下也各自被归属闸消掉了**(dev 那例 D-65、test 那例 D-69) ——
原文"真实 LLM 消除了 mock 的两例误报"只是当时的表述,不要读成"只有真实 LLM 能消"。
**不能说的**:检索质量结论——Embedding 仍是 mock bigram,`evidence_hit` 只反映
字面重叠检索是否把金标片段捞上来。

两处测量口径局限(不是模型缺陷,别误读):
1. `answer_hit` 按金标线索**字面包含**判定;真实模型改写成"两年"而非"2年"
   会被记未命中(dev_q2 即此例,故 dev 出现 0.75)。
2. 真实 LLM 输出非严格确定(同一输入两次跑,纪要条目数 9 与 11 都出现过);
   受层1校验约束,结构合法性稳定,条目粒度不稳定。评测数字要跨次比较时
   必须固定 prompt_version 并看区间,不看单次。

费用口径:真实调用若未配单价表(`LLM_PRICE_PER_1K_IN/OUT`),`cost_basis=unknown`
且 `cost_estimate` 为 NULL,报告里体现为 `unpriced_real_calls=15` ——
这是"不知道花没花钱",不是"没花钱";该机构一旦设置预算即被视为不可核算而停止调用。

## 10. 接手后的第一次核验(2026-09-25)

**接手规范**:先跑下面几条,确认现状与文档一致再动任何文件;不一致就先把差异报告给用户,不要直接改。

| 检查 | 命令 | 2026-09-25 实测 |
|---|---|---|
| 推送前闸门 | `python D:\desens\gate_scan.py` | ✅ 两道通过、exit 0(纳入母本真实语料整句 11 条) |
| 版本现状 | `git -C 〔母本路径〕 log --oneline -5` | ✅ 与公开版同源(公开版 HEAD = "同步母本 <母本 HEAD>") |
| 全量测试 | `pytest -q` | ✅ **141 passed / 3 skipped**(150.9s) |
| 真 Provider 集成 | `MEET_TESTS_USE_REAL_PROVIDERS=1 pytest tests/test_real_embedding.py -q` | ✅ **3 passed**(29.0s);⚠️ 开关**只能限定在该文件上**,见下 |

依赖外部状态:上面两条测试都要先起 `mes-pg`(`docker start mes-pg` → 等 healthy)。
Docker 起不来时它们跑不了 —— **那是"没跑",不是"失败"**,不要混为一谈。

**2026-09-25 补跑(Docker 起来之后)**:

- 全量测试:`pytest -q` → **141 passed / 3 skipped**(150.9s;改动后再跑 130.9s,无回归)
- 评测基线:`ASR_PROVIDER=mock LLM_PROVIDER=mock EMBED_PROVIDER=mock
  QA_EVIDENCE_THRESHOLD=0.15 python eval/run_eval.py` → 冻结 dev/test 各 6 题 + 权限探针
  全绿(数字见 §9 更正留痕)。**注意:裸跑 `python eval/run_eval.py` 会直接退出码 1** ——
  脚本断言 `ASR_PROVIDER=mock`(语料靠 mock ASR 注入),`.env` 配成真 whisper 时必须先覆盖。
- 顺带查出并修掉一个真缺陷(**D-69**):型号归属闸在"金额/数量"类问题上是**空转**的 ——
  问题点名的型号代号自带数字("Y300" 就满足 `\d`),于是闸恒不触发;冻结 test 的
  `test_neg_model_mismatch`(「Y300的报价是多少?」)误报 0.5 就是它。修后误报 **1 → 0**、
  可答题命中率不变(无误伤)。
- 起服务让用户看界面时,用户报告"人工转写那一页一直跳来跳去" → 查出并修掉前端一个
  **无界自我刷新**的真缺陷(**D-70**)。用户描述的位置是详情页第 2 张卡,但根因在第 1 张卡
  (录音进度)与第 3 张卡(纪要)被反复重建、把同行的卡推上推下。
  离线量化:改前 300ms 内 **501 次**请求(触发上限保护),改后**已结束的任务 0 次**、
  运行中的任务 6 次后停止。`pytest -q` 无回归。

仍未复验的:`demo/seed_demo.py`、各能力主张的端到端复跑(需起服务写应用库)。

### 更正留痕:`MEET_TESTS_USE_REAL_PROVIDERS` 的写法(原文有歧义,照着做会踩坑)

原文(`README.md` / `docs/EVAL_BASELINE.md` / `PROJECT_SUMMARY.md` / `docs/RESUME.md`)
写的是「显式设 `MEET_TESTS_USE_REAL_PROVIDERS=1` 时实测 3 passed」——**读起来像是在整套上加开关**。
实际不是:`tests/conftest.py` 只在**没设**该开关时才把 Provider 钉回 mock;一旦设了,
**整套测试**都会改用 `.env` 里的真实组合(whisper / deepseek / bge-m3)与 0.66 阈值,
而那些夹具是按 mock 口径(阈值 0.15)造的 → 大面积**假失败**(conftest 自己的注释就写着
「轻则假失败(阈值口径不同)」)。

2026-09-25 实测对照:

| 命令 | 结果 |
|---|---|
| `pytest -q`(默认) | **141 passed / 3 skipped**(150.9s) |
| `MEET_TESTS_USE_REAL_PROVIDERS=1 pytest tests/test_real_embedding.py -q` | **3 passed**(29.0s) |
| `MEET_TESTS_USE_REAL_PROVIDERS=1 pytest -q`(整套) | **32 failed / 96 passed / 1 skipped / 15 errors** ← 假失败,**不是**产品缺陷 |

抽查一例证明是假失败:`tests/test_qa.py::test_query_log_written` 报 `assert row[5] is True`
(即 `has_sufficient_evidence`);日志里能看到真实权重确实被加载
(`Loading weights: 100%|391/391`),检索候选也取到了,只是 0.66 阈值把 mock 口径的夹具
判成"证据不足"。**这个开关是给单文件用的,不是给整套用的。**


### 更正留痕:提交数口径(此前多处写死且互不一致)

此前三处硬编码的提交数**彼此不符、也都与实测不符**,已更正 —— 按纪律单独留痕,
不悄悄删:

| 出处 | 原写 | 更正前实测(2026-09-25) |
|---|---|---|
| `PROJECT_SUMMARY.md` | 「`git log` 29 次提交」 | 母本 45 / 公开版 43 |
| 对外材料母本(仓库之外) | 「仓库 39 次提交」 | 母本 45 / 公开版 43 |
| 旧版交给接手的文档 | 「母本 43 次提交」 | 母本 45 / 公开版 43 |

两仓全部提交的作者均为本人(与"均为本人提交"一致)。

**规则:凡引用提交数,先 `git rev-list --count HEAD` 实测再写,不要沿用旧文** ——
条数每提交一次就变,写死的数字必然过期,而"每个数字都能当场复跑"是本项目的卖点。
`PROJECT_SUMMARY.md` 已改为不写死条数、由读者自数。

**对外材料提醒**:联系方式的母本放在仓库之外,**永远不入任何 git 仓库**;
对外文件导出前先与母本对齐,避免发出过期版本。
