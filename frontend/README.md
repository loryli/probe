# 练习场前端

项目唯一前端工程，使用 Vue 3 + TypeScript + Vite。设计、目录职责与样式约定见 [前端开发方案](../docs/frontend/README.md)，业务约束见 [业务保护规则](../docs/frontend/BUSINESS_RULES.md)。

需要 Node.js 20.19+ 或 22.12+。先从仓库根目录启动 FastAPI，再在此目录运行：

```powershell
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173/`。开发页面路由为 `/`、`/materials`、`/planner`、`/interviews`。

构建：

```powershell
npm run build
```

生成 `dist/` 后重启 FastAPI，打开 `http://127.0.0.1:8000/app/`。`/frontend-assets/` 是构建资源前缀，不是开发页面入口。前端依赖及构建产物不放入后端源码目录；旧原生页面已删除。
