# 资产管理系统（AMS）

Flask + SQLite 资产与采购台账。管理端 `frontend/main.html`，个人门户 `frontend/my.html`，反向代理为 Nginx。

## 文档入口

| 文档 | 用途 |
| --- | --- |
| [更新说明.md](./更新说明.md) | **已交付行为**的权威说明（接口口径、角色菜单、测试命令） |
| [产品地图-采购全流程.md](./产品地图-采购全流程.md) | 采购端到端叙事与分期缺口 |
| [演示账号.txt](./演示账号.txt) | 演示角色与点测路径（口令只写在此文件） |
| [docs/部署运维.md](./docs/部署运维.md) | Docker 启停、备份、访问地址、换库 |
| [docs/架构概览.md](./docs/架构概览.md) | 目录、进程、数据文件、权限边界 |
| [docs/常见问题.md](./docs/常见问题.md) | 登录网络错误、密码哈希、用工具打开库等 |
| [AGENTS.md](./AGENTS.md) | 给 Cursor / 代理的上下文指针 |

行为变更以 `更新说明.md` 为准；本 README 不重复接口细则。

## 快速启动（Docker）

在含 `docker-compose.yml` 的本目录执行：

```bash
docker compose up -d --build
```

Windows 本机常见访问：

- 页面：`http://127.0.0.1/login.html`（Nginx :80）
- 或直连后端：`http://127.0.0.1:8088/login.html`
- 局域网：`http://<本机局域网IP>/login.html`

容器名固定为 `ams-backend`、`ams-nginx`。运维命令见 `manage.sh` 与 [docs/部署运维.md](./docs/部署运维.md)。

Linux 服务器一键部署（需 root）：

```bash
sudo bash deploy.sh
```

## 本地测试（不改正式库）

```bash
cd backend
python -m pytest tests/test_main_flows.py -q
```

正式数据在 `data/assets.db`；pytest 使用临时库。

## 演示数据

```bash
cd backend
python _seed_demo_charts.py
```

账号与建议验证路径见 `演示账号.txt`。重新跑种子会重置演示账号密码，**不改**超管密码。

## 技术栈摘要

- 后端：`backend/app.py`（Flask）、`backend/database.py`（SQLite）
- 前端：静态 HTML + Element Plus；角色菜单 `frontend/ams-role.js`
- 部署：`Dockerfile`、`docker-compose.yml`、`nginx/`
