# 快速开始

构建、测试和服务运行统一在 WSL 执行。本机用于编辑代码和 Git 操作。

## 安装与模型配置

需要 Python 3.11+、Git，建议安装 ripgrep。运行完整平台还需要 Java 17+、Maven、
Node.js 22、Docker Compose 和 make，配置方法见 [部署指南](full-stack-development.md)。

```bash
git clone --branch full-stack-integration https://github.com/Artoriazzz/RepoAgent.git
cd RepoAgent
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

编辑 `.env` 中的模型接口与 API key。CLI 不自动加载 `.env`，在仓库根目录执行：

```bash
set -a
source .env
set +a
```

配置示例：

```bash
export OPENAI_API_KEY="your-api-key"
export OPENAI_BASE_URL="https://open.bigmodel.cn/api/paas/v4"
export OPENAI_MODEL="glm-5.3-flash"
```

## 修复本地仓库

```bash
repo-agent run \
  --repo /path/to/repository --coordinator \
  --task "定位异常根因，保留相关合法输入的行为，完成最小修复" \
  --output-dir .repo_agent_runs/my-fix
```

## 从 Issue 或 CI 进入

GitHub API 请求可按需配置 `GITHUB_TOKEN`：

```bash
repo-agent fix-issue \
  --repo /path/to/repository \
  --issue-url https://github.com/owner/repo/issues/123 --coordinator

repo-agent repair-ci \
  --repo /path/to/repository \
  --github-repo owner/repo --run-id 123456 --coordinator
```

## 角色模型与预算

```bash
repo-agent run \
  --repo /path/to/repository --coordinator \
  --task "修复公开问题描述中的行为" \
  --model glm-5.3-flash \
  --coordinator-reasoning-effort high \
  --implement-reasoning-effort low --reasoning-effort low \
  --max-steps 16
```

`--implement-model` 单独指定实现模型。供应商对思考参数的支持存在差异，
协议适配位于 `repo_agent/core/llm.py`。

## 经验检索入口

单独检查公开 Issue 的查询提取：

```bash
repo-agent extract-memory-query \
  --issue-file /path/to/public-issue.md --model glm-5.3-flash \
  --output-dir .repo_agent_runs/query
```

- 普通 CLI 使用 `MemoryStore` 的词法召回；`--memory-query-model` 只开启独立查询提取。
- 向量入口 `scripts/vector_memory_agent.py` 使用冻结经验记录与历史向量，
  在线提取查询、计算 embedding、每类选 Top-1。该入口需要准备对应实验配置，
  包括 embedding Python、模型路径、经验记录和索引；现有 WSL 批处理脚本含环境专属路径。
- 后端 Worker 通过业务 API 召回固定 revision，配置 `REPO_AGENT_MEMORY_MODE`；
  Qdrant 与 embedding 需要分别启用，详见 [经验设计](../backend/docs/memory-rag-design.md)。

查询提取可通过 `REPO_AGENT_MEMORY_QUERY_BASE_URL`、`REPO_AGENT_MEMORY_QUERY_API_KEY`
指定独立服务。JSON 格式无效时允许一次保留完整 Issue 的格式修正；CLI 提取器仍失败时
返回公开问题并标记原因，实验向量入口将其视为流程错误，不使用题号兜底。

## 运行流程与入口

README 的横向图表示“经验召回 → 首次修复 → 验证 → 失败归因与纠正 →
预算判断 → 再次协调”的统一协作设计。本次文档调整未改动运行时状态机。

当前入口边界：

| 入口 | 召回与学习 | 反馈纠正与调度 |
| --- | --- | --- |
| 普通 CLI `run --coordinator` | 根据配置召回 | 不自动启动官方 Harness 完整闭环 |
| CLI `experience-build` | 关闭历史召回，成功后可学习 | 支持一次反馈纠正，需显式配置验证命令 |
| CLI `benchmark` | 只读召回 | 关闭失败反馈纠正与记忆写入 |
| SWE-bench 学习批处理 | 关闭历史召回，独立评测成功后学习 | 最多两次纠正；范围不足时可尝试 Coordinator replan |
| 后端 Worker `evolve` | 召回经验，成功后提交学习提案 | 阶段流水线支持一次失败归因与纠正 |

**当前实现尚未为所有入口提供“任意纠正失败后自动回到 Coordinator”的通用循环。**
实际调度遵循各入口的授权检查和预算；不能通过修改 README 启用新的运行路径。

反馈使用公开验证还是 Harness 取决于入口与配置。普通运行不会自动安装评测器或构建镜像。
正式评测需单独审计 hints、测试反馈、同题经验与预测选择过程。

## 平台启动与验证

```bash
make install
make frontend-install
# 加载 .env 后执行；共享 Worker Token 需替换为随机密钥。
make infra
```

三个 WSL 终端分别加载根目录 `.env`，运行 `make backend`、`make worker`、`make frontend`。
默认后端 8080，前端 5173。实际修复前需创建用户、仓库和 Issue，并准备 Worker 镜像目录。
完整操作见 [部署指南](full-stack-development.md)。

```bash
make test-python
make test-backend
make frontend-build
make infra-down
```

`infra-down` 保留命名数据卷。日志发布前检查密钥、业务数据和源码权限。
