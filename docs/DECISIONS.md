# 设计决策记录(DECISIONS)

按时间倒序追加。每条注明状态:设计/已实现/部分实现。

## P3(2026-09-23,进行中)

- **D-42 费用三态与"预算不可核算"**:`model_run.cost_basis` ∈
  {estimated, provider_reported, unknown};`cost_estimate` 为 NULL 表示**未知而非 0**。
  mock 调用恒为 unknown(is_mock=true,本地计算不产生真实费用,也不得冒充真实成本)。
  推论:机构设置了预算却存在成本未知的**真实**调用时,预算无法核算 →
  claim 与调用双重拒绝(宁可停住,不按 0 放行)。结果未知(超时/断连)的调用
  按预估保守计为已用,与 D-28"不宣称避免重复计费"一致。已实现。
- **D-38(修订 D-36)预算在每次外部调用前预留**:只在领取时比较历史费用,
  拦不住一个领取后连续调用数百次的长任务。`app/services/budget.py` 在发出调用前
  做原子预留(受守卫约束的 `UPDATE organization SET reserved_cents = reserved_cents + est`
  且 `SUM(cost_estimate)+reserved+est <= budget`,rowcount=0 即 BudgetExceededError →
  **不调用 Provider**);返回后结算。并发由 organization 行锁串行。
  边界:预估不足时实际成本可能小幅超出预留(以 model_run 记账为准);
  问答(HTTP 内)与离线导入同样走这条路径,不再有免费调用旁路。已实现。
- **D-43 Provider 工厂:配置驱动且不静默回退 mock**:`ASR_PROVIDER/LLM_PROVIDER/
  EMBED_PROVIDER` 决定实现;未注册名 / 缺凭证 / Embedding 维度与向量列不符
  分别抛 Unknown / Credential / Dimension 三类错误(维度校验落实 D-03)。
  `describe_providers()` 供报告与界面显示**实际生效**模型,禁止硬编码 mock。
  已实现。
- **D-44 人工转写导入路径(ASR 不可用时的权威来源)**:
  `POST /meetings/{id}/transcript` → 无转写版本时建 `human_transcription` 首版,
  已有版本时建 `human_correction` 追加版 + 逐条 `segment_mapping`;
  **时间轴仍来自音频切片**(行数须等于切片数,不符即拒绝,绝不由文本推断位置);
  随后按配置生成纪要(层1校验)并重建索引、原子发布。原版本片段不可变,
  历史引用仍可解析。边界:该入口是数据通路,不等于纪要人工确认/编辑工作流
  (仍未实现);`transcript_version.source_type` 在库中是 VARCHAR 且**无 CHECK 约束**
  (迁移遗留),取值仅由应用层枚举保证。已实现。
- **D-45 真实模型组合冻结(2026-09-23)**:实测 DeepSeek `deepseek-chat` 可用
  (返回 usage.tokens,成本按单价表估算,未配单价则 unknown);
  SiliconFlow 的 `FunAudioLLM/SenseVoiceSmall`(ASR)与 `BAAI/bge-m3`(Embedding,
  维度 1024 恰与向量列一致)均返回 **402 账户余额不足**,故真实 ASR 与真实检索
  本轮不可用。冻结组合 = **真实 LLM(DeepSeek)+ mock Embedding + 人工转写来源**;
  不再做模型选型实验。界面与评测报告必须逐项标注该来源与是否 mock,
  检索质量结论在真实 Embedding 接入前一律不成立。部分实现(检索仍 mock)。
- **D-46 演示界面为无构建步骤的静态页**:`app/web/`(原生 HTML/CSS/JS)由 FastAPI
  挂载在 `/`,只做既有 API 的呈现,不含管理后台、不含业务判断(权限与模型选择
  全在后端)。理由:交付窗口内可维护、可交接,零前端工具链。已实现。
- **D-47 模型输出归一只向更保守方向修正**:真实 LLM 的纪要条目若缺
  owner/due_date,适配器强制 `needs_confirmation=true`(不代为补造),
  未知 type/certainty 降级为 discussion/discussed;越界证据 id 不静默剔除,
  仍由服务层引用校验判为协议违规。校验不通过即 502 显式失败。已实现。

## P3 初始评测框架(2026-09-23)

- **D-40 评测框架(初始,报告自描述)**:`eval/dataset.json` 以 `split_by=meeting`
  按会谈划分 dev/test(冻结靠 `dataset_version` + 报告内 sha256),语料经
  `MEET_MOCK_ASR_CORPUS_FILE` 注入 mock ASR(每句对应一个 10s 切片),
  经真实子进程 worker 完成四阶段后逐问调 API 评分。
  指标刻意分开:`evidence_hit_rate`(引用命中金标片段)/ `answer_hit_rate`
  (回答含金标线索)/ `missed_rate`(有答案却判不足)/
  `no_answer_correct_rate` 与 `false_positive_rate`(无答案题的拒答与误报),
  并按类别汇总时把"可回答题"与"无答案题"分列(同名指标不得混算)。
  **报告必须可自证身份**:`meta.git` 含 rev/branch/**dirty**/改动清单/
  `content_fingerprint`(覆盖未提交内容与未跟踪文件),`meta.database` 记录实际
  写入的库——只有 git_rev 时,一份跑了未提交代码的报告无法复现。
  已实现。
- **D-41 mock 基线的解释边界**:当前全部 Provider 为 mock,评测分数只证明
  "数据链路与评分脚本正确",**不得**作为检索或事实判断效果的证据;
  D-32 已知反例(型号错配→把 X200 的日期当 X300 的答案)在 mock 基线中
  确实以 false_positive 形式出现,这正是待真实模型改进的测量对象。
  调参纪律:`--top-k` / `--threshold` 一次只覆盖一个旋钮,且不得据 test 集反复调参。
  **样本量边界**:dev/test 目前各 1 场会谈 / 6 题,只能作流程冒烟,
  真实基线前必须扩充并冻结。已记录。

## 工程约束:测试库隔离(2026-09-23)

- **D-39(修订 D-06)测试必须运行在独立测试库,清表前先核实实际库名**:
  原 D-06 让集成测试直连本地开发库并在 session 开始 TRUNCATE 全部业务表——
  一旦开发库里有真实数据或他人正在使用,该约定就是破坏性操作。现改为:
  ① `TEST_DATABASE_URL` 必须显式配置(env 或 .env),**缺失即整场退出,不执行
  任何 TRUNCATE**(不从 `database_url` 推导或回退);② 与应用库的
  (host, port, database) 三元组相同、或库名相同(即便主机不同)一律拒绝;
  ③ 不信任 URL 字符串,连接后以 `current_database()` 复核,并校验编码为 UTF8;
  ④ 测试库不存在则自动建库,迁移只应用到测试库;⑤ 守卫在 conftest 顶部、
  `app.core.db` 导入之前执行——engine 在该模块导入时按当时配置创建,顺序错了
  就会连到应用库;⑥ worker 子进程经继承的 `DATABASE_URL` 走同一测试库,并由
  `test_worker_subprocess_inherits_test_database` 实证(否则故障注入会写脏真实库)。
  配套:`no_stale_pending_jobs` 由 conftest autouse 全局提供(原在两个测试模块
  各自定义),并移除依赖未安装的 pytest-order 的 `@pytest.mark.order` 标记,
  使单文件运行与任意执行顺序结果一致。已实现。

## P2 收尾(2026-09-23)

- **D-33 Provider 入口调用记录(命名口径纠偏)**:新增 `MEET_MOCK_REQUEST_LOG`
  在三个 mock Provider 方法**最开头**(先于延迟与故障注入)追加 JSON 行。
  定性必须准确:这是**本地 Provider 入口调用记录**,能证明"Provider 被调用了几次、
  入参规模多大",**不能证明真实服务商收到请求**。`model_run` 的前置 unknown 行只证明
  调用意图;两者独立并列,才区分出"意图 / 我方发出 / 远端结果 / 业务提交"四层。
  真实 Provider 接入后需以服务商返回的请求 ID 与账单核对(待验证,不宣称已具备)。
  已实现。
- **D-34 调用结果四态分类**:`ProviderResultUnknownError`(超时/断连——请求已发出、
  远端结果不可知)保留 model_run=unknown 并原样上抛;**明确执行失败**才记 failed
  (`_fail_model_run`)。测试分别注入并断言 `unknown ≥ 1 / failed = 0` 与
  `failed = 1 / unknown = 0` 两种形态,不混用。已实现。
- **D-35 三层时间约束(心跳存活≠任务有进展)**:① 单次外部调用超时
  `provider_call_timeout_seconds`(默认 60s,`_call_provider` 用独立线程 +
  `fut.result(timeout)` 兜底);② 单阶段执行期限 `stage_deadline_seconds`(默认 300s,
  期限内所有调用共享,超期抛 `StageDeadlineError` 且**不再发出新调用**);
  ③ 租约 TTL(既有)。实际单调用超时 = min(①, 阶段剩余期限)。
  **能力边界(如实声明)**:Python 线程无法强杀,超时后的挂起调用线程会泄漏直到自行
  结束,故"超时"只保证任务不再阻塞、不保证远端已停;真实 Provider 必须自带网络超时,
  本层为最后防线。已实现。
- **D-36 重试退避与调度守卫前移到 claim**:失败回队写
  `next_run_at = now + min(attempt * base, cap)`(base=0 禁用,保持既有时序);
  `claim_job` 增加三重候选过滤:`attempt_count < worker_max_attempts`、
  `next_run_at <= clock_timestamp()`、组织累计费用未达 `organization.budget_cents`
  (NULL=不限)。使"重试上限/退避/预算"真正参与调度,而非仅记录。
  **已知不足**:只在领取时比较历史费用,无法约束单个长任务的总开销;调用级预留
  检查在后续阶段补(见 P3 的 D-38)。已实现(仅领取侧)。
- **D-37 恢复复用的证据口径**:恢复类测试必须同时核对三样东西——
  DB 状态、产物数量、调用留痕;并且区分**"结果没有重复"**(每 input_hash 恰好一条
  succeeded)与**"外部调用没有重复"**(入口调用日志的物理请求数)。
  索引中断恢复用例据此断言 `model_run {succeeded:2, failed:3}` + 请求批大小
  `[3,2,2,2,2]`(已提交分片被对账复用、不重发),而非只断言"最终成功"。已实现。

## P2(2026-09-23)

- **D-27(修 P1.2 遗留)MEET_WORKER_* 环境变量从未生效**:Settings 无 env_prefix,字段只认 `WORKER_LEASE_TTL_SECONDS` 等无前缀名,测试注入的 `MEET_WORKER_*` 一直被忽略(P1.2 测试碰巧不依赖 TTL 过期所以全绿)。修复:worker_* 三字段加 AliasChoices 双名兼容。P2 恢复测试正是靠短 TTL 注入发现此缺陷。已实现(诊断脚本实证:kill 后租约 117s 不过期 → 修复后 2s 过期)。
- **D-28 模型调用两阶段记录(unknown 前置)**:调用前 INSERT model_run(status=unknown),返回后 UPDATE succeeded / 异常时 failed。进程死于窗口内 → unknown 行残留 = "结果未知的调用"审计证据;接管者重发(缓存只认 succeeded,未命中必然重调),同 input_hash 出现 unknown+succeeded 两行——**外部调用物理重复,如实留痕,不宣称避免重复调用或计费**(真实服务商接入时靠 idempotency_key 请求级去重,mock 无此语义)。配套:model_run.idempotency_key 唯一约束改为普通索引(行=一次物理调用的事实记录,同键可多行;幂等键语义收窄为服务商请求级去重键)。已实现(场景测试验证)。
- **D-29(修 P1.2 缺陷)run_index 切换范围**:is_active 翻转原为 meeting 级,同会谈多录音时后完成者会踢掉先完成录音的索引;修复为限定本录音旧版本(经 version→recording 子查询)。回归测试:同会谈两条录音(15s+25s)各自发布后均 active=5、内容均可检索。已实现。
- **D-30 恢复边界声明**:① 心跳线程保证活 worker 租约不过期,租约过期回收仅对死亡/卡死进程有效;② 被杀进程无法复活,"旧 worker 恢复后写入被拒"以进程内持旧 epoch 模拟(等价网络分区恢复),子进程级验证到"kill → 过期 → 接管(epoch+1) → 产物唯一"为止;③ 无重试退避(维持 D-16);④ 测试间残留任务防护:recovery 测试前置 fixture 将遗留 queued/running 置终态,防 worker 抢走残留导致注入错位。已实现。
- **D-31 检索日志口径分离**:响应与 query_log 均区分 raw_retrieved_count(阈值前原始检索数)/ relevant_candidate_count(过阈值候选数),retrieved_chunk_ids 保留阈值前 ID 列表;避免数量与 ID 列表口径混淆。已实现(迁移 c8a1f27d90b4 加列)。
- **D-32 评测反例集(起始)**:mock 充分性启发式的已知误判纳入评测反例——① 日期归属:候选含"X200 注册证 5月20日补齐"时,问"X300 的注册证什么时候补齐"会被误判充分(字面规则无法验证日期归属);② bigram 碰撞噪声:短问题与无关长句可碰撞至 ~0.16(> 阈值 0.15),如"培训周期多长?"vs 注册证句实测 0.157。真实 LLM + 人工复核是待验证方案,不视为问题已解决;评测阶段须以 dev/test 集区分并覆盖上述反例。已记录(评测未实现)。

## P1.3(2026-09-23)

- **D-24(修订 D-18/D-20,评审反馈)候选 SQL 会谈显式限定 + 发布状态双保险**:
  ① 候选必须显式 `meeting_id = URL 会谈`,"属于可见会谈集合"不替代该条件(测试:同机构同用户可见的另一会谈,相同内容索引零泄漏);② 检索同时匹配 `is_active=true` 与 `chunk.version_id = recording.active_search_version_id`(指针为权威,冗余标志误标不可见;测试:未发布版本 chunk 误标 active,与问题向量相似度=1 仍不进候选)。已实现。
- **D-25(修订 D-20)充分性两层判定,has_sufficient_evidence 为 mock 启发式**:
  第一层=服务层相关性阈值(0.15,仅筛选相关候选);第二层=mock LLM 焦点线索启发式(问负责人→候选须含"负责";问时间点→须含日期模式;问时长→须含数字+年/个月/天/周),线索缺失返回 text=None → 明确证据不足,不拼接候选冒充答案。**不对外宣称完成真实充分性校验**:字面规则无法验证线索与问题对象的对应(如把 X200 的日期当 X300 的答案),语义充分性留给真实 LLM + 人工确认。已实现。
- **D-26 retrieved_count 口径=过相关性阈值的候选数**(非阈值前检索数):0=无相关候选(无索引/不相关/未发布版本被排除),>0 且证据不足=相关但焦点线索缺失;query_log.retrieved_chunk_ids 另记阈值前检索候选(审计完整检索行为)。已实现。
- **D-18 问答范围=会谈内**:POST /meetings/{id}/ask。权限=会谈访问检查(404 不区分无权/不存在)+ 候选 SQL 双注入(embedding_chunk.org_id 等值 + meeting_id ∈ 可见会谈子查询),不信任单一冗余键;引用无需跨会谈上下文。已实现(双机构同内容索引隔离测试验证:向量完全相同时对方片段不进候选)。
- **D-19 问答同步调用模型**:问题向量化与 LLM 回答在请求内完成;P1.1 的"请求内零模型调用"仅约束上传受理路径。mock 为本地确定性计算;真实 Provider 接入时再评估流式/异步。已实现。
- **D-20 证据充分性阈值 0.15**(config qa_evidence_threshold):候选余弦相似度 ≥ 阈值才可作为证据;过滤后为空 → 明确返回证据不足(answer=None、无引用、has_sufficient_evidence=false),无可用索引同样归入证据不足。阈值按 mock bigram 实测校准(相关问题 ~0.45 / 无关 ~0.06),真实 Provider 需重校准。已被 D-24/D-25 修订扩展。
- **D-21 引用必须来自权限过滤后的候选**:LLM 越权引用即协议违规(500 拒绝返回),不静默剔除(剔除会导致回答文本与引用集不一致)。mock QA 为抽取式:回答文本只由候选原文拼接,禁止编造。已实现。
- **D-22 纪要查看**:GET /meetings/{id}/minutes 返回最新版本完整内容 + 版本摘要;条目证据按 segment_id 解析为内嵌片段详情(原文/说话人/时间位置),解析范围限本会谈录音(防跨会谈探测);片段落库不可变保证 human_correction/re_asr 后历史引用仍可解析。已实现。
- **D-23 问答全量审计**:每次问答(含证据不足)写 query_log(user/问题/候选 chunk/回答/引用/充分性/is_mock)。已实现。
- **测试修正**:test_recordings 的"零模型调用"断言由全表改为限定本 job——全表查询依赖测试执行顺序(test_qa 的 worker 产物先落库即污染),非放宽断言。已实现。

## P1.2(2026-09-23)

- **D-11 use_alter FK 的坑**:alembic autogenerate 的 create_table 不会真正创建 use_alter=True 的内联 FK,P1.1 迁移后 recording→transcript_version 两个外键实际缺失,由 bd57398cd833 迁移补建(已验证 DB 中恰好一套)。已实现。
- **D-12 结果缓存按业务范围隔离**:model_run 缓存查询限定同 job(修订约定"不能只按相同内容合并不同业务请求");跨任务复用留给真实 Provider 接入时按机构粒度评估。已实现。
- **D-13 分片 input_hash 含分片序号**:相同字节在不同位置不共享缓存(真实 ASR 依赖上下文)。测试暴露:静音 WAV 各片字节相同曾导致片 1 命中片 0 缓存。已实现。
- **D-14 阶段异常统一兜底**:Provider 抛出的任何异常(非仅 StageError)都走失败/重试路径,不让异常杀死 worker。已实现(测试注入验证)。
- **D-15 mock LLM 输出经层1校验后才入库**:校验失败=阶段失败;空值策略仅约束 decision/todo(topic/discussion 不适用)。已实现。
- **D-16 能力边界声明**:阶段内重试(含失败后跨进程重跑)已实现并测试;租约过期回收路径代码已存在,但"杀死 worker 后的完整崩溃恢复"系统测试属 P2,不宣称已验证;无重试退避间隔(真实 Provider 接入前再评估)。
- **D-17 租约校验用 clock_timestamp()::timestamp**(数据库实时钟,naive 与列一致),区别于事务开始时刻的 now()。已实现。

## P1.1(2026-09-23)

- **D-01 会谈不维护状态字段**:处理进度由录音的 processing_job 派生,避免双状态源不同步。已实现。
- **D-02 时间戳统一 UTC naive(DateTime 无时区)**:PG 容器默认 UTC;前端展示时转换。已实现。
- **D-03 向量维度固定 1024**:迁移时定死;真实 Provider 若维度不同需要新迁移(记录于迁移文件)。已实现。
- **D-04 枚举用非原生 VARCHAR+CHECK(native_enum=False)**:避免 PG 原生枚举改值的迁移成本。已实现。
- **D-05 密码哈希用 stdlib scrypt,JWT 用 PyJWT/HS256**:无 bcrypt Windows 兼容问题;密钥走环境变量。已实现。
- **D-06 集成测试直连本地 PG,session 开始时 TRUNCATE 业务表**:测试库与开发库共用(本地专用),保证可重复执行。**已被 D-39 修订**:不再共用,TRUNCATE 只允许发生在显式配置的独立测试库上。
- **D-07 上传仅做扩展名白名单校验,内容校验留给 P1.2 prepare 阶段**:上传路径不做格式解析,保持轻量。已实现。
- **D-08 重复提交 = 同 meeting + 同 SHA-256 → 409 返回已有 recording/job**:不创建重复逻辑任务。已实现。
- **D-09 上传权限 = 对该会谈有访问权的任意用户**:不区分角色,操作审计由 created_by 记录。已实现。
- **D-10 无权与不存在统一 404**:不区分两者,防止存在性探测。已实现。

## 环境备注(Phase 0)

- Docker Hub 直连超时,经 docker.m.daocloud.io 拉取;中文目录名需 compose -p mes;
  alembic.ini 必须保持 ASCII(GBK 读取);工具链统一 PYTHONUTF8=1。
