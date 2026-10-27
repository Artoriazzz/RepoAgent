# RepoAgent 任务监控前端

Vue 3 + TypeScript 监控控制台，用于查看修复任务队列、Worker 心跳、当前执行阶段、实时事件、验证结果和运行产物。

## 开发启动

先启动 Spring Boot 后端，再运行：

```bash
cp .env.example .env.local
npm install
npm run dev
```

默认地址为 `http://localhost:5173`。Vite 将 `/api` 转发到 `VITE_BACKEND_TARGET`。首次进入页面时填写开发阶段的用户 UUID，浏览器会将其保存到 `localStorage`，并作为 `X-User-Id` 请求头发送。

## 生产构建

```bash
npm run build
```

构建产物位于 `dist/`。生产环境建议由 Nginx 托管静态文件，并把 `/api` 反向代理到 Spring Boot；不要将 Worker 内部接口暴露到公网。

## 页面能力

- 队列、执行中、今日完成和 Worker 在线情况总览
- Worker 当前任务、阶段和最近心跳
- 任务分页、状态筛选和仓库/Issue 信息
- 任务阶段、运行指标、F2P/P2P 和错误详情
- 基于 SSE 的任务事件实时更新与断线补发
- 任务取消、显式重试和运行产物下载

