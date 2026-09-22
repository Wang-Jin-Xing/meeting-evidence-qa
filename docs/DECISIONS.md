# 设计决策记录(DECISIONS)

按时间倒序追加。每条注明状态:设计/已实现/部分实现。

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
