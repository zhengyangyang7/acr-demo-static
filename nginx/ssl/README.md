# HTTPS 证书目录（可选）

本目录用于存放 HTTPS 证书，对应 `nginx/conf.d/default.conf` 末尾被注释的 443 server 块。

## 启用 HTTPS 步骤

1. 将证书文件放入本目录：
   - `server.crt` —— 证书（公司证书或 Let's Encrypt 免费证书均可）
   - `server.key` —— 私钥
2. 编辑 `nginx/conf.d/default.conf`，取消末尾 443 server 块的注释。
3. 如需强制 HTTP 跳 HTTPS，同时取消 80 server 里 `return 301` 行的注释。
4. 重启：`docker restart ams-nginx`

## 说明

- **当前默认仅启用 HTTP(80)**，本目录为空不影响系统正常运行。
- 若 HTTPS 已由前置 LB / 网关终结（浏览器访问 `https://aap.hknhw.com` 已带锁），
  则**无需**启用本配置，保持现状即可。
- 证书为敏感文件，请勿提交到 git（已在 .gitignore 中排除 `*.key`/`*.crt` 时生效；如未排除请手动添加）。
