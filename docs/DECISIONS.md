# 设计决策记录(DECISIONS)

按时间倒序追加。每条注明状态:设计/已实现/部分实现。

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
