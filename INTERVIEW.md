# 面试追问手册(INTERVIEW)

每题都指到**具体代码位置**,便于当场翻给面试官看。行号以当前 HEAD 为准。
最后两节是"我知道自己弱在哪"——面试官一定会问。

---

## A. 证据链与版本

**Q:为什么纪要里点开的引用一定是原文,而不是模型复述?**
`/meetings/{id}/ask` 的引用不是模型生成的文本,而是检索命中的片段行本身:
`app/services/qa_service.py:196` 把候选的 `text/start_ms/end_ms/similarity` 直接组装成
`CitationOut`,模型只负责生成 `answer` 字段。所以引用与原文不可能不一致;
不一致的是回答,那属于生成侧问题(已实测到一例:回答省略了引用里的否定限定语)。

**Q:转写被人工改过,怎么保证不覆盖原始输出?**
版本只追加。`app/services/transcript_correction.py:221 import_corrected_transcript`
新建 `transcript_version(source_type=human_correction, correction_of_version_id=…)`
并写 `segment_mapping`,原 `asr` 版本与片段行不动;
入口脚本里还有断言"校对必须新增版本,不得覆盖 ASR 原版"。

**Q:人有两版转写,系统"当前用哪一版"记在哪?为什么要两个指针?**
`app/models/recording.py:42-50`:`active_transcript_version_id`(给人看)与
`active_search_version_id`(检索唯一依据)。索引重建有窗口期,若只靠
`embedding_chunk.is_active` 单标志,会出现"一半新一半旧"的检索结果;
`retrieve_candidates` 里再加一条 `Recording.active_search_version_id == chunk.version_id`
(`app/services/qa_service.py:66-73`),冗余标志误标也检索不到未发布版本。

**Q:纪要被人工改过,AI 的判断还看得到吗?**
看得到,而且是两回事:`minutes_version` 每次编辑追加 `human_edit` 并记
`parent_version_id`(`app/services/minutes_workflow.py:edit_minutes`);
"确认"不是会谈级开关,而是**版本行上的** `confirmed_by/confirmed_at`
(迁移 `b7e4d2a91c58`)。新版本一律回到 `pending_review`——否则等于替人背书他没看过的内容。

---

## B. 检索与问答

**Q:向量检索怎么做的?top-k 之外还筛什么?**
一次查询向量 → pgvector 余弦距离排序取 top-k(`app/services/qa_service.py:38`),
然后**在应用层按相似度阈值二次筛**(`:117`,`1 - distance >= qa_evidence_threshold`)。
响应里同时给 `raw_retrieved_count`(阈值前)与 `relevant_candidate_count`(过阈值),
就是为了把"没检索到"和"检索到但不够相关"分开——这两个数不一样时,问题在阈值不在索引。

**Q:阈值怎么定的?为什么不是 0.5?**
按模型标定,不是猜的:mock bigram 相关 ~0.45 / 无关 ~0.06 → 0.15;
换 bge-m3 后**不相关句就有 0.39~0.55**,金标 0.71~0.82,取 dev 上
`min(金标相似度) - 0.05 = 0.66`(标定脚本 `demo/verify_real_embedding.py`)。
默认值仍是 0.15 服务 mock 测试,真值经 `QA_EVIDENCE_THRESHOLD` 注入 `.env`。

**Q:证据不足时怎么办?**
直接返回"证据不足",无回答、无引用,不拼接候选冒充答案(`insufficient()` 分支)。
产品含义:医疗/采购场景里,幻觉比沉默贵得多。

**Q:已知最大的检索问题是什么?**
短疑问句。实测"保修时间确定了吗?"人工校对后最高相似度 **0.6566**,
差 0.0034 被 0.66 挡掉,而排名第一的正是应引用那片——所以这一类的瓶颈是阈值/问句风格,
不是 ASR 也不是排序。还没解决,是当前第一优先级。

---

## C. 租约、恢复与幂等

**Q:worker 挂了任务怎么办?**
`app/worker/lease.py:32 claim_job` 领取时 `lease_epoch+1` 并写 `lease_expires_at`;
`app/worker/lease.py:114 leased_transaction` 让**每一次阶段提交**都带
`WHERE lease_epoch = :epoch AND (未过期)`,用 `clock_timestamp()` 判定。
于是被接管的旧 worker 提交会 `LeaseLostError`,不会双写。
接管与故障注入有真实子进程测试(`tests/test_recovery.py`)。

**Q:阶段重跑会不会重复调用模型、重复计费?**
阶段是幂等的:每阶段开头先对账(本 job 是否已产出该结果),已提交的分片直接复用
(`run_transcribe`/`run_minutes`/`run_index` 开头都有对账查询)。
但**不宣称一定不重复计费**:超时/断连时外部世界可能已经执行并扣费,
这类结果记为 `unknown` 并**按预估保守计入预算**(`app/services/call_accounting.py:142 billed_call`)。
这是我能接受的最诚实写法。

**Q:重复上传怎么处理?**
`recording` 上有 `UNIQUE(meeting_id, checksum)`;命中抛 `DuplicateRecordingError` →
409 带已有 `recording_id/job_id`,不新建任务。已在真实录音上从网页重复提交两次,
`processing_job / recording / model_run / job_stage / transcript_version` 计数全部不变。

---

## D. 权限

**Q:多租户怎么隔离?前端能选机构吗?**
不能。机构归属只由 `get_current_user` 从 JWT 解析;界面没有机构下拉。
隔离不是"查出来再过滤",而是把条件写进 SQL:
`app/services/access.py` 的 `visible_meeting_condition`(单资源)+
`visible_meeting_ids_subquery`(集合),检索路径还额外要求显式
`EmbeddingChunk.meeting_id == URL 参数` 且 `org_id` 等值(`qa_service.py:74-77`)。
冗余标志(`is_active`、`org_id`)都不单独作为授权依据。

**Q:为什么无权是 404 而不是 403?**
403 会告诉攻击者"这个 id 存在"。所有越权路径统一 404,不区分。

**Q:音频文件怎么防直接下载?**
`storage/` 不在任何静态挂载下(`app/main.py` 只挂 `app/web`),
唯一入口 `GET /recordings/{id}/audio`(`app/api/recordings.py:46`)带同一套可见性判定,
响应 `Cache-Control: private, no-store`。测试直接断言按 uuid 文件名取 `/uploads/…` 和 `/…` 都是 404。

---

## E. 评测与可复现

**Q:怎么证明效果?**
分三层,并且**不敢混**:① 程序与权限(138 passed / 3 skipped);
② 链路正确性(真实录音端到端、入口验收工件);
③ 效果测量(`eval/run_eval.py` 分指标 + 真实链路 5 问 A/B)。
样本量分别写在报告里,12 题评测集和 5 问真实链路都太小,**不外推成准确率**。

**Q:报告怎么保证可复现?**
每份报告记:git rev、**工作区是否脏 + 内容指纹**(只记 rev 复现不了未提交代码)、
实际写入的数据库名、Provider 与模型名、提示词版本、检索参数、数据集 sha256、每题时延与成本口径。

**Q:test 集有没有被用来调参?**
没有。阈值标定只用 dev(D-48 明确纪律);若将来任何一次用 test 决定过方案,
就必须在报告里记污染并另设留出集——这条已经写进 `docs/DECISIONS.md` 的口径。

---

## F. 我知道自己弱在哪(面试官会问)

1. **短疑问句召回**:固定阈值把有答案的问题挡了(差 0.0034)。没解决。
2. **泛化问句**候选为 0,阈值与问句风格强相关,缺少多轮调参的证据。
3. **单样本**:真实录音只有一条,三次转写还出现过标点抖动。
4. **生成侧会丢限定语**,同一问题两次回答不一致;我只记录了,没修。
5. **回放把整条录音读进内存**,大文件要改流式或签名短链。
6. **阶段自身耗时不可得**:`job_stage.started_at` 没落库,只能给阶段完成间隔。

## G. 如果只有 30 秒,我会这样说

"我做了一个会谈证据系统:录音转写和纪要都能点开到**原始片段**,支持按切片回放;
后台是独立的 worker 进程,带租约和 fencing,进程被杀任务会被人接管且不会双写;
所有模型调用都进台账,成本分'估算/服务商返回/未知'三态,未知按预估保守计费而不是填 0。
我用一条真实录音跑了 A/B:人工校对确实把 5 个问题的相似度全部抬升、并修掉了答案里的错字;
但有 2 个问题校对后仍然'证据不足',查下来是阈值差 0.0034,不是识别错误——
这个改判是我这轮最有价值的产出,因为它把'该修模型'变成了'该修检索策略'。"
