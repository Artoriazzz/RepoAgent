# 全栈集成分支开发指南

分支：`full-stack-integration`。Python 包、CLI 和 Worker 保留在仓库根目录，
Spring Boot 放在 `backend/`，Vue 前端放在 `backend/frontend/`。

## 导入来源

后端来自 `Artoriazzz/backend` 的本地工作区，基础提交为
`9243c7e`，同时包含导入时尚未提交的任务流水线、RAG、监控前端与数据库迁移代码。
本次按源码快照导入，后端原仓库及其工作区保持不变。
后续集成分支的修改在当前仓库维护；原独立仓库不会自动同步。

导入排除了 `.git`、`target`、`node_modules`、`dist`、本地 `.env`、日志和 TypeScript 编译产物。
两边 Git 历史独立保留，本分支记录导入后的统一版本。

## WSL 启动前准备

构建、测试和服务运行统一在 WSL 执行，本机用于编辑和 Git 操作。
WSL 需要 Python 3.11+、Java 17+、Maven、Node.js 22、npm、Docker Compose，以及 `make`。
Docker 依赖会使用 3306、6379、5672、15672、6333、6334；业务服务使用 8080，前端使用 5173。
请先确认端口空闲，并准备独立的开发数据库；已有数据库请先备份。

```bash
git switch full-stack-integration
python -m venv .venv
make install
make frontend-install
cp .env.example .env
```

编辑 `.env`：填写模型接口与 API key，将 `REPAIR_WORKER_TOKEN` 改为随机密钥。
`REPO_AGENT_WORKER_TOKEN` 引用同一变量，后端与 Worker 使用相同的值。

**各个终端都从仓库根目录加载配置**：

```bash
set -a
source .env
set +a
```

示例中的 `${PWD}` 和变量引用由 shell 在 source 时展开。CLI 本身不自动读取 `.env`。
直接设置环境变量时，也需要保证两端 Worker Token 一致，以及
`REPAIR_ARTIFACT_ROOT` 指向 Python Worker 输出目录下的 `runs/`。

## 依次启动

```bash
# 基础设施：MySQL、Redis、RabbitMQ、Qdrant。
make infra

# 终端一：加载 .env 后运行。
make backend

# 终端二：加载 .env 后运行。
make worker

# 终端三：运行监控前端。
make frontend
```

- 后端健康检查：`http://localhost:8080/actuator/health`。
- 前端：`http://localhost:5173`，默认代理到 `http://127.0.0.1:8080`。
- RabbitMQ 管理界面：`http://localhost:15672`，本地示例账号为 `repoagent/repoagent`。
- Qdrant 在 Compose 中启动；启用后端向量检索仍需配置 `MEMORY_QDRANT_ENABLED`
  及 embedding 相关选项，启动容器本身不会自动启用 RAG。

前端可通过 `backend/frontend/.env.local` 中的 `VITE_BACKEND_TARGET` 覆盖代理目标。
使用页面前需要按 [业务接口说明](../backend/README.md) 创建用户、仓库和 Issue。
当前 `X-User-Id` 为开发鉴权入口；此示例不具备完整的生产认证与网络安全配置。

## 仓库镜像与评测环境

普通 GitHub 修复任务需要预先准备镜像：
`${REPO_AGENT_WORK_ROOT}/mirrors/<repository-id>`，其中 repository-id 来自后端 API。
可在该路径使用 `git clone --mirror <authorized-repository-url> <repository-id>`。
Worker 据此创建任务专属 worktree。模型配置、镜像授权、分支推送权限需分别准备。

SWE-bench 还需要单独配置数据集、仓库 mirror、Harness Python 和 Docker 镜像。
这些实验数据保持外置，不随单仓库集成导入。详见 [后端说明](../backend/README.md)
与根 README 的运行模式边界。

## 验证与停止

```bash
make test-python
make test-backend
make frontend-build
# 或一次执行：make test

make infra-down
```

`infra-down` 保留命名数据卷。此分支的 CI 分别检查 Python、Java 和前端构建。
上述单元测试与构建不代表已完成真实 GitHub 任务、模型 API、队列和 Harness 的端到端验证。

## 本次集成验证

构建验证在 WSL 独立目录中进行，保留现有部署与实验目录。
分支 CI 同时配置了 Python、Java 17/Maven 和前端检查。

- Python：515 项测试通过。
- Java 17 / Maven：44 项测试通过，BUILD SUCCESS；使用 WSL 已有 Maven 和依赖缓存。
- 前端：离线安装依赖后，TypeScript 检查与 Vite 生产构建通过。
- Docker Compose：配置校验通过；未启动新的基础设施实例。
- 验证目录：`/home/artoria/workspace/RepoAgent/fullstack-check-20261027-ruywpo`，
  Java 结果保存在该目录的 `java-test.log`。

本次验证未调用付费模型，也未覆盖真实 GitHub 发布与完整修复任务端到端联调。
