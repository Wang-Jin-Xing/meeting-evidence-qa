# 架构与流程图

配套文字说明见 `README.md`;权限逐接口清单见 `docs/PERMISSIONS.md`;
设计取舍与"为什么这样做"见 `docs/DECISIONS.md`。图中每个节点都能在代码里找到对应文件。

## 1. 进程与组件

```mermaid
flowchart LR
  subgraph browser["浏览器(静态页,无构建)"]
    UI["app/web/index.html + app.js"]
  end

  subgraph api["API 进程 (uvicorn app.main:app)"]
    R1["app/api/* 路由"]
    S1["app/services/* 业务与权限"]
    P1["app/ai/registry → Provider 组合"]
  end

  subgraph worker["独立 worker 进程 (python -m app.worker)"]
    L["lease 领取/续租/fencing"]
    ST["四阶段 stages.py"]
  end

  PG[("PostgreSQL 16 + pgvector<br/>任务表/版本/向量/台账")]
  FS[("storage/uploads 原件<br/>storage/derived 播放副本")]
  EXT["模型:本地 whisper / bge-m3<br/>远程 DeepSeek / mock"]

  UI -- "HTTP + JWT" --> R1 --> S1 --> PG
  R1 --> FS
  S1 --> P1 --> EXT
  ST --> P1
  worker -- "claim / 提交(带 epoch 校验)" --> PG
  api -. "只落盘与建任务,不做模型调用" .-> worker
  UI -- "带鉴权 fetch 取音频字节" --> R1
```

要点:
- **API 与 worker 是两个进程**,只通过数据库里的任务表通信;上传请求内不调用任何模型。
- Provider 组合由仓库根 `.env` 决定,API / worker / 脚本读同一份;不可用时**报错而不静默回退 mock**。
- 音频文件不在任何静态挂载之下,只能经 `GET /recordings/{id}/audio` 取。

## 2. 数据流(一次会谈从上传到可问答)

```mermaid
flowchart TD
  U["上传原件(流式落盘 + SHA-256)"] --> REC["recording(不可变)"]
  U --> JOB["processing_job(queued)+ 4×job_stage(pending)"]
  JOB --> PR["prepare:探测格式、切 10s 片<br/>transcribe_slice(pending)"]
  PR --> TR["transcribe:逐片 ASR<br/>→ transcript_version v1 + transcript_segment"]
  TR --> PM["发布转写:active_transcript_version_id = v1"]
  PM --> MI["minutes:LLM 产出结构化条目<br/>minutes_validator 校验(引用/空值策略)<br/>→ minutes_version v1 (ai_draft, pending_review)"]
  MI --> IX["index:批量向量化<br/>embedding_chunk(is_active=false)"]
  IX --> PB["原子发布:active_search_version_id = v1<br/>+ 旧块置 is_active=false"]
  PB --> ASK["POST /ask:权限注入检索 → 阈值筛选 → LLM 回答"]
  ASK --> CIT["回答 + 引用(segment 原文/时间/相似度)"]
  CIT --> LOG["query_log + model_run 台账"]

  HC["人工转写导入 / 校对<br/>human_transcription|human_correction"] --> NEWV["**新增**版本 + segment_mapping<br/>不改写 ASR 原版"] --> PM
  ME["纪要人工修订 human_edit"] --> MV["新增 minutes_version<br/>(parent 指向被编辑版本)"] --> CONF["按版本确认<br/>confirmed_by / confirmed_at"]
```

要点:
- **双发布指针**:`active_transcript_version_id`(给人看的当前转写)与
  `active_search_version_id`(检索唯一依据)。索引未完成的版本哪怕误标 `is_active`
  也检索不到,因为指针不匹配。
- 版本与片段**只追加不覆盖**;校对、重转写、纪要编辑都是新版本 + 血缘字段。
- 每次外部模型调用一行 `model_run`(两阶段留痕:先插 unknown,再回写结果)。

## 3. 任务与租约状态机

```mermaid
stateDiagram-v2
  [*] --> queued: 上传受理(202)
  queued --> running: worker claim(epoch+1, 写 lease_expires_at)
  running --> running: 阶段推进 prepare→transcribe→minutes→index
  running --> succeeded: 四阶段全部提交
  running --> queued: 阶段失败 → 退避 next_run_at<br/>attempt_count+1
  queued --> failed: 超过重试上限
  running --> running: 租约过期被他人接管<br/>原持有者提交时 epoch 不符 → LeaseLostError
  note right of running
    所有写入都带 epoch 与 clock_timestamp() 守卫:
    被接管的旧 worker 不能把结果写回去(防"双写")
    模型调用超时/断连 = 结果未知:status 保留 unknown,
    按预估保守计入预算,不宣称"没扣过费"
  end note
```

## 4. 权限与可见性判定(每个读接口同一套)

```mermaid
flowchart TD
  REQ["请求 + JWT → user(org_id, role)"] --> VIS{"会谈可见性"}
  VIS -- "org" --> A["本机构 且 (org 或 本人创建)"]
  VIS -- "restricted" --> B["白名单 meeting_access 或 本人创建 或 org_admin"]
  A --> OK["可见"]
  B --> OK
  B -. 不在名单 .-> NF["404(与'不存在'同形,不给探测信号)"]
  A -. 跨机构 .-> NF

  OK --> Q1["检索/聚合类接口:条件写进 SQL<br/>org_id 等值 + meeting_id ∈ 可见会谈子查询<br/>+ 显式 meeting_id 限定 + 发布指针匹配"]
  OK --> Q2["单资源接口:join 到 meeting 再套同一可见性条件"]
  Q1 --> RES["结果只含该用户可见的数据"]
  Q2 --> RES
```

不变量(有测试锁定):
- 机构归属**只由服务端从 JWT 解析**,界面不提供选机构入口;
- 冗余的 `is_active` / `org_id` 标志都不能单独作为授权依据,必须与指针和可见性子查询同时成立;
- 音频字节走同一可见性判定,`restricted` 同机构未授权也 404。
