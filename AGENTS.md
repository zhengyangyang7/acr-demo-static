# AGENTS.md

给代理的上下文指针。细节进下列文档，避免在本文件重复行为说明或口令。

## 必读指针

| 何时 | 打开 |
| --- | --- |
| 已交付功能、接口口径、角色菜单、测试条数 | `更新说明.md` |
| 采购全流程叙事、分期缺口 | `产品地图-采购全流程.md` |
| Docker / 备份 / 访问 URL / 换库 | `docs/部署运维.md` |
| 目录、进程、SQLite、权限边界 | `docs/架构概览.md` |
| 登录网络错误、哈希口令、Navicat 锁库 | `docs/常见问题.md` |
| 演示角色与点测（口令只在此） | `演示账号.txt` |
| 人读入口与快速启动 | `README.md` |

完成用户可感知的功能或修复后，在同一次改动中更新 `更新说明.md`（见 `.cursor/rules/update-notes.mdc`）：文首「最近一次」写打开页面能看到的结果；不写未落地计划；不写账号口令。

## 仓库约定（短）

- 正式库：`data/assets.db`；测试用临时库，勿指到正式路径。
- 管理端以 `frontend/main.html` 为准，改完同步 `index.html`。
- 角色菜单：`frontend/ams-role.js`；后端管理 API 目前是 `ADMIN_ROLES` 大桶，未与菜单一一对应。
- 交付 zip：排除 `.cursor`、缓存、`assets.db-wal` / `assets.db-shm`。
- 不把密钥、口令写入说明类 Markdown。

## 建议技能（本仓库 `.cursor/skills`）

| 任务 | 技能目录 |
| --- | --- |
| 写/改给代理读的说明、本文件指针 | `productivity/writing-for-agents` |
| 会话交接摘要 | `productivity/handoff` |
| 收紧方案并顺带沉淀 ADR/术语 | `engineering/grill-with-docs`（及 grilling、domain-modeling） |
| 架构改进评估 | `engineering/improve-codebase-architecture` |
| 实现前对齐规格 | `engineering/to-spec` / `implement-spec` |
