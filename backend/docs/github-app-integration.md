# GitHub App 与后端接口

本文档描述当前已实现的后端接口，以及创建 GitHub App 时应填写的地址。OAuth 登录、installation token 交换和 Draft PR 发布尚未实现，因此不要为它们填写虚构的回调地址。

## 公网基础地址

令 `PUBLIC_BASE_URL` 为 GitHub 能访问的 HTTPS 地址，例如：

```text
https://api.example.com
```

`100.72.78.127` 是私有 Tailnet/内网地址，`http://localhost:8080` 也只对本机可见，二者都不能直接作为 GitHub Webhook URL。开发时可通过反向代理或临时 HTTPS tunnel 把后端的 `8080` 端口暴露为 `PUBLIC_BASE_URL`；生产环境应使用稳定域名和 TLS。

## GitHub App 注册填写项

| GitHub App 字段 | 当前填写值 |
| --- | --- |
| Homepage URL | `${PUBLIC_BASE_URL}/actuator/health` |
| Webhook active | 开启 |
| Webhook URL | `${PUBLIC_BASE_URL}/api/integrations/github/webhooks` |
| Webhook secret | 新生成的随机值，同时配置为后端 `GITHUB_WEBHOOK_SECRET` |
| User authorization callback URL | 暂时留空，OAuth callback 尚未实现 |
| Setup URL | 暂时留空，installation setup callback 尚未实现 |
| Request user authorization during installation | 暂时关闭 |

当前建议只订阅 `Issues` Webhook，并授予最小的 repository 权限：

| 权限 | 值 | 用途 |
| --- | --- | --- |
| Metadata | Read-only | GitHub App 的基础仓库识别权限 |
| Issues | Read-only | 接收并同步 Issue 事件 |

后续实现 GitHub App installation token、分支推送和 Draft PR 时，再申请 `Contents: Read and write`、`Pull requests: Read and write`、`Actions: Read-only`。只有允许 Agent 修改 `.github/workflows` 时才申请 `Workflows: Read and write`。

## Webhook 接口

```http
POST /api/integrations/github/webhooks
```

完整地址：

```text
${PUBLIC_BASE_URL}/api/integrations/github/webhooks
```

GitHub 自动发送以下请求头，后端均会校验或使用：

| Header | 用途 |
| --- | --- |
| `X-Hub-Signature-256` | 使用 HMAC-SHA256 校验原始请求体，密钥为 `GITHUB_WEBHOOK_SECRET` |
| `X-GitHub-Delivery` | 投递唯一标识，用于数据库去重 |
| `X-GitHub-Event` | 事件类型；当前仅处理 `issues` |

返回 `204 No Content` 表示接受投递。已绑定的仓库会同步 Issue 的编号、标题、正文及开闭状态；重复投递和当前不支持的事件会被安全忽略。尚未在平台绑定的仓库也会被忽略，不会自动创建记录。

后端环境变量：

```bash
export GITHUB_WEBHOOK_SECRET='use-a-long-random-secret'
```

本地验证可用同一 secret 计算 `sha256=<hex-hmac>` 并发送请求。不要关闭签名校验，也不要把 secret 写入仓库。

## 业务 API

当前公开业务接口仍使用开发期身份头 `X-User-Id`。OAuth 接入后，该头会由登录 Session 或 Bearer Token 替代。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/api/users` | 无 | 创建开发期平台用户 |
| `POST` | `/api/repositories` | Owner `X-User-Id` | 绑定 GitHub 仓库 |
| `POST` | `/api/repositories/{repositoryId}/members` | Owner | 添加 Owner/Developer 成员 |
| `POST` | `/api/repositories/{repositoryId}/issues` | Member | 手动创建 Issue 记录 |
| `POST` | `/api/issues/{issueId}/repair-tasks` | Owner | 创建幂等修复任务，需 `Idempotency-Key` |
| `GET` | `/api/repair-tasks/{taskId}` | Member | 查询任务和 patch/验证摘要 |
| `POST` | `/api/repair-tasks/{taskId}/cancel` | Owner | 取消未结束任务 |

示例：

```http
POST /api/issues/{issueId}/repair-tasks
X-User-Id: <owner-user-uuid>
Idempotency-Key: issue-42-first-run
Content-Type: application/json

{"baseSha":"<git-commit-sha>"}
```

任务状态依次为：

```text
CREATED -> QUEUED -> ANALYZING -> FIXING -> VALIDATING -> PATCH_READY -> PR_READY
```

`PATCH_READY` 表示 RepoAgent 已在本地隔离 worktree 生成非空 patch；只有后续 GitHub App 流程完成分支推送和 Draft PR 创建后，才能进入 `PR_READY`。

## Worker 内部接口

这两条接口只供 Python `repo-agent-worker` 调用，不应暴露给公网。它们要求：

```http
X-RepoAgent-Worker-Token: <REPAIR_WORKER_TOKEN>
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/internal/repair-tasks/{taskId}/context` | 获取仓库、Issue、基线 SHA 和目标分支快照 |
| `POST` | `/internal/repair-tasks/{taskId}/updates` | 上报顺序状态和最终 patch/验证/PR 结果 |

状态上报示例：

```json
{
  "status": "PATCH_READY",
  "patchSummary": "Local patch generated for: src/parser.py. Artifact: model.patch",
  "validationSummary": "RepoAgent completed; independent CI is still required.",
  "prUrl": null
}
```

后端拒绝跳过状态、未授权访问和终态任务的后续更新。
