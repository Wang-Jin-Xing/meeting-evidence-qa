# 权限矩阵(按当前实现整理,如实声明)

权限总原则:身份由 JWT → 数据库解析(User.org_id / role),不信任请求参数;
无权与不存在统一 404(防存在性探测);机构归属一律取自后端身份。

## 已实现入口

| 操作 | 入口 | 权限规则 |
|---|---|---|
| 登录 / 身份 | `POST /auth/login`、`GET /auth/me` | 用户名密码 → JWT;org/role 取自数据库 |
| 会谈列表 | `GET /meetings` | 本机构 + 可见性条件(SQL 内过滤:org 可见 / 本人创建 / 白名单 / org_admin) |
| 会谈详情 | `GET /meetings/{id}` | 同上,无权/不存在统一 404 |
| 创建会谈 | `POST /meetings` | 任意认证用户;org_id/created_by 取自身份;白名单用户须属本机构 |
| 录音上传 | `POST /meetings/{id}/recordings` | 对该会谈有访问权的任意用户(不区分角色);同会谈同内容 409 |
| 任务状态 | `GET /jobs/{id}` | 经录音→会谈 join 可见性条件;404 不区分 |
| 音频回放 | `GET /recordings/{id}/audio` | 经录音→会谈 join 可见性条件(org 等值 + 可见会谈子查询),无权/不存在统一 404(不给录音 ID 探测留信号);`restricted` 需白名单,同机构未授权也 404;支持单区间 Range(206/416);响应 `Cache-Control: private, no-store`;**storage/ 不在任何静态挂载下,音频只能经本接口取** |
| 检索问答 | `POST /meetings/{id}/ask` | 三重条件:① 会谈访问检查(404);② 候选 SQL 显式 `meeting_id = URL 会谈`;③ 候选 SQL 权限注入(org_id 等值 + 可见会谈子查询);另匹配发布指针(is_active 且 version = active_search_version),未发布版本不可检索;预算耗尽/不可核算 → 503(不回退 mock) |
| 纪要查看 | `GET /meetings/{id}/minutes` | 会谈访问检查;条目证据解析限定本会谈录音的版本链(防跨会谈探测);最新纪要版本的证据引用旧版本片段仍可解析(片段不可变) |
| 人工转写导入 | `POST /meetings/{id}/transcript` | 会谈访问检查(404);`recording_id` 指定时须属本会谈(否则同样 404,不区分);导入者记入 `transcript_version.created_by`;模型调用按调用者机构记账与预算约束 |
| 运行元信息 | `GET /meta` | 仅认证用户可读;返回**实际生效**的 Provider 组合与是否 mock(配置不可用时 503,不伪装) |

## 未实现入口(明确列出,不冒充已有)

| 能力 | 状态 |
|---|---|
| 原始音频下载 / 回放 | **已实现为回放接口**(`GET /recordings/{id}/audio`,同一接口即下载入口):24/32-bit 原件由服务端降位成 16-bit **派生副本**再送出,原件始终只读;`X-Audio-Source` / `X-Audio-Original-Bits` / `X-Audio-Served-Bits` 如实标注实际送出的字节 |
| 转写版本列表 / 片段单独查看 | 无 API(片段原文仅内嵌于问答引用与纪要证据响应) |
| 历史纪要版本内容查看 | 仅返回最新版本完整内容 + 全部版本摘要;旧版本内容及其引用解析无入口 |
| 人工纠错 / 重新转写触发 | 数据模型支持(human_correction/re_asr 版本、segment_mapping),流程 API 未实现 |
| 纪要人工确认 / 编辑流转 | 未实现(纪要状态恒为 pending_review,无流转入口) |
| 问答历史 / query_log 审计查看 | 无 API(仅落库) |
| 全局(跨会谈)问答 | 未实现(当前为会谈内问答,见 D-18) |

## 边界说明

- 问答响应的 `has_sufficient_evidence` 为 mock 启发式(相关性阈值 + 焦点线索
  规则),不构成真实充分性校验(D-25);
- mock 检索向量为字符 bigram 哈希,存在碰撞噪声(实测短问题与无关长句可
  达 ~0.16 相似度),阈值 0.15 的判定含此噪声,真实 Provider 需重校准;
- 历史引用的可解析性依赖片段落库不可变约束;任何删除/改写片段的后续设计
  都会破坏该保证,需显式评审。
