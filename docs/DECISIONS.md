# 设计决策记录(DECISIONS)

按时间倒序追加。每条注明状态:设计/已实现/部分实现。

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
- **D-06 集成测试直连本地 PG,session 开始时 TRUNCATE 业务表**:测试库与开发库共用(本地专用),保证可重复执行。已实现。
- **D-07 上传仅做扩展名白名单校验,内容校验留给 P1.2 prepare 阶段**:上传路径不做格式解析,保持轻量。已实现。
- **D-08 重复提交 = 同 meeting + 同 SHA-256 → 409 返回已有 recording/job**:不创建重复逻辑任务。已实现。
- **D-09 上传权限 = 对该会谈有访问权的任意用户**:不区分角色,操作审计由 created_by 记录。已实现。
- **D-10 无权与不存在统一 404**:不区分两者,防止存在性探测。已实现。

## 环境备注(Phase 0)

- Docker Hub 直连超时,经 docker.m.daocloud.io 拉取;中文目录名需 compose -p mes;
  alembic.ini 必须保持 ASCII(GBK 读取);工具链统一 PYTHONUTF8=1。
