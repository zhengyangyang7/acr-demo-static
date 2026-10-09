#!/bin/bash
# 资产管理系统 - Docker 一键部署脚本 v7.2
# 用法：上传 ams-deploy-package.tar.gz 到 /opt/，执行 sudo bash deploy.sh
# 日常运维（重启/看日志/换库验证）：bash manage.sh <命令>

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
log_err()  { echo -e "  ${RED}✗${NC} $1"; }
log_info() { echo -e "  ${BLUE}→${NC} $1"; }

echo "============================================"
echo "  资产管理系统 v7.2 一键部署"
echo "============================================"

[ "$EUID" -ne 0 ] && { log_err "请用 sudo bash deploy.sh 执行"; exit 1; }

# ── 1. Docker ──
if ! command -v docker &>/dev/null; then
    log_info "安装 Docker..."
    yum install -y yum-utils >/dev/null 2>&1
    yum-config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo >/dev/null 2>&1 || \
    yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo >/dev/null 2>&1
    yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1
    systemctl start docker && systemctl enable docker
fi
log_ok "Docker: $(docker --version 2>/dev/null | head -1)"

# ── 2. Compose ──
# 固定 compose 项目名，避免在不同目录(/opt/ams-deploy-package、/opt/asset-management-system)下
# 运行时产生多个项目名、却共用 container_name(ams-backend/ams-nginx) 导致的 "already in use" 冲突
export COMPOSE_PROJECT_NAME="ams"
DOCKER_COMPOSE="docker compose"
docker compose version &>/dev/null || {
    DOCKER_COMPOSE="docker-compose"
    command -v docker-compose &>/dev/null || {
        curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
        chmod +x /usr/local/bin/docker-compose
    }
}
log_ok "compose: $DOCKER_COMPOSE"

# ── 3. 项目目录 ──
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
PROJECT_DIR="/opt/asset-management-system"
[ -f "$SCRIPT_DIR/Dockerfile" ] && PROJECT_DIR="$SCRIPT_DIR"
mkdir -p "$PROJECT_DIR" && cd "$PROJECT_DIR"

# ── 4. 解压部署包 ──
TARBALL=""
for p in /opt/ams-deploy-package.tar.gz ./ams-deploy-package.tar.gz; do
    [ -f "$p" ] && TARBALL="$p" && break
done

if [ -n "$TARBALL" ]; then
    log_info "解压部署包..."
    TMPDIR="/tmp/ams-tmp-$$"
    rm -rf "$TMPDIR"; mkdir -p "$TMPDIR"
    tar -xzf "$TARBALL" -C "$TMPDIR"
    SRC=$(find "$TMPDIR" -name Dockerfile -type f | head -1 | xargs dirname 2>/dev/null)
    [ -z "$SRC" ] && SRC="$TMPDIR/ams-deploy-package"
    # 备份数据库（部署包可能包含空数据库，避免覆盖线上数据）
    [ -f "$PROJECT_DIR/data/assets.db" ] && cp "$PROJECT_DIR/data/assets.db" "$PROJECT_DIR/data/assets.db.bak" 2>/dev/null || true
    cp -rf "$SRC"/* "$PROJECT_DIR/" 2>/dev/null || true
    # 如果覆盖了数据库，从备份恢复
    [ -f "$PROJECT_DIR/data/assets.db" ] && [ -f "$PROJECT_DIR/data/assets.db.bak" ] && cp "$PROJECT_DIR/data/assets.db.bak" "$PROJECT_DIR/data/assets.db"
    rm -rf "$TMPDIR"
    log_ok "文件已就位"
elif [ -f "$PROJECT_DIR/Dockerfile" ]; then
    log_ok "项目文件已存在"
else
    log_err "找不到部署包！上传 ams-deploy-package.tar.gz 到 /opt/"
    exit 1
fi

cd "$PROJECT_DIR"

# ── 5. 配置注入 ──
log_info "检查配置..."

grep -q "DATA_DIR=/app/data" docker-compose.yml 2>/dev/null || {
    sed -i '/PYTHONUNBUFFERED/a\      - DATA_DIR=/app/data' docker-compose.yml
    log_ok "DATA_DIR 已注入"
}

# Lark 凭证改从 .env 读取（不硬编码密钥）。首次部署若无 .env，从模板生成并提示填写。
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        log_err ".env 已从模板生成，请编辑填入真实 LARK_APP_ID / LARK_APP_SECRET 后重新执行 deploy.sh"
    else
        log_err "缺少 .env 配置文件（请先 cp .env.example .env 并填入 Lark 凭证）"
    fi
    exit 1
fi
log_ok "Lark 凭证已从 .env 读取"

mkdir -p data/uploads data/backups logs/nginx 2>/dev/null

# ── 5.5 初始种子数据（首次部署无数据库时自动使用）──
if [ ! -f "data/assets.db" ] || [ ! -s "data/assets.db" ]; then
  if [ -f "data/assets-initial.seed" ]; then
    cp -f "data/assets-initial.seed" "data/assets.db"
    log_ok "已从种子数据初始化数据库 (assets-initial.seed → assets.db)"
  else
    log_info "未找到种子数据文件，后端启动时会自动创建空库"
  fi
fi

# 安全加固（P1-4 + 复审2-③）：数据/日志目录属主对齐容器内运行用户(appuser, uid 1000)，
# 权限由 777 收紧为 755（owner 可读写执行，组/其他仅读执行）。
# 关键：chown 必须在 seed 拷贝之后执行——否则首部署 assets.db 由 root 拷贝、属主 root，
# 非 root 容器(appuser uid1000)写库失败。root 运行时 root 无视属主仍可写，两种模式兼容。
chown -R 1000:1000 data/ logs/ 2>/dev/null || true
chmod -R 755 data/ logs/ 2>/dev/null || true
log_ok "目录权限已设置（属主uid1000 / 权限755）"

# ── 6. 构建并启动 ──
log_ok "配置检查完成"
log_info "构建镜像..."

$DOCKER_COMPOSE down --remove-orphans 2>/dev/null || true
# 强制清理可能残留的同名容器（旧部署/异常退出遗留，往往属于不同 compose 项目名，
# 导致 down 清不掉、up 时 "container name already in use" 冲突）
docker rm -f ams-backend ams-nginx 2>/dev/null || true
docker builder prune -f 2>/dev/null || true
$DOCKER_COMPOSE build --no-cache backend 2>&1 | grep -E '(Step|Successfully|error)' | tail -3
# 二次保险：up 前再清一次，规避 daemon 在 rm/up 之间的时序窗口导致清不干净
docker rm -f ams-backend ams-nginx 2>/dev/null || true
$DOCKER_COMPOSE up -d --remove-orphans

# ── 7. 验证 ──
echo ""
log_info "等待服务启动..."
for i in $(seq 1 30); do
    curl -s http://localhost:8088/api/auth/check >/dev/null 2>&1 && break
    sleep 1
done

if curl -s http://localhost:8088/api/auth/check | grep -q "success"; then
    log_ok "后端 API: 正常"
    curl -s http://localhost:8088/api/auth/lark-signature | grep -q "signature" && log_ok "Lark 签名: 正常"
else
    log_err "后端未启动，查看日志:"
    $DOCKER_COMPOSE logs --tail=30 backend 2>/dev/null | head -30
    exit 1
fi

echo ""
echo "============================================"
echo -e "  ${GREEN}✓ 部署完成！${NC}"
echo "============================================"
echo "  访问     http://本机IP/login.html  （或 https://aap.hknhw.com）"
echo "  超管账号 Cookie.gu"
# 优先展示 data/INITIAL_ADMIN_PASSWORD.txt（交付包预置或首次建库写入），避免只能翻 docker logs
if [ -f "data/INITIAL_ADMIN_PASSWORD.txt" ]; then
    echo "  超管密码 见文件 data/INITIAL_ADMIN_PASSWORD.txt ："
    echo "  --------"
    sed 's/^/  /' data/INITIAL_ADMIN_PASSWORD.txt
    echo "  --------"
    echo "  登录后请立即修改密码；生产环境改密后可删除该文件。"
else
    echo "  超管密码 未找到 data/INITIAL_ADMIN_PASSWORD.txt"
    echo "  可执行: docker logs ams-backend 2>&1 | grep 初始密码"
    echo "  或重置: bash manage.sh set-superadmin \"\" \"\" \"\" \"\" '你的新密码'"
fi
echo ""
echo "  管理: bash manage.sh status / logs / restart / swap-db"
echo ""
