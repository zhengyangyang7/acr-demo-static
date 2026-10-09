#!/bin/bash
# 资产管理系统 - 运维一键脚本 manage.sh
# 用法： bash manage.sh <命令> [参数]
#
# 设计原则（来自踩坑复盘）：
#   生产容器名由 deploy.sh 固定为 ams-backend / ams-nginx，且 COMPOSE_PROJECT_NAME=ams。
#   因此所有运维动作一律【直接用容器名操作 docker】，绝不依赖 `docker compose` 的项目名，
#   否则在老版本 Docker（只有 docker-compose）或不同工作目录下会报
#   “service has no container to start” 之类的问题。
#
# 命令：
#   deploy            重新部署（= deploy.sh，会自动备份并保留 data/assets.db）
#   status            查看 ams 容器状态
#   logs [行数]       跟踪后端日志（默认 50 行，Ctrl+C 退出）
#   restart           重启后端容器
#   backup-db         备份当前数据库（带时间戳，存 data/assets.db.bak-YYYYMMDD-HHMMSS）
#   swap-db <文件>    安全替换数据库并重启（验证数据用，自动备份旧库）
#   restore-db <文件> 用指定备份恢复数据库
#   bind-lark <ID> <员工ID>   强绑定某员工的 Lark ID（用于免密登录）
#   set-superadmin [LarkID] [邮箱] [姓名] [所在地] [密码]
#                         强绑定“唯一超管”并降级其他超管（默认即 Cookie.gu/古基/Aaa77377）
#
# 注意：cp 一律用 `command cp -f` 绕过服务器上 `cp -i` 别名，避免交互卡住批量粘贴。

set -e
cd "$(dirname "$0")"

DB="data/assets.db"

# 解析 compose 命令（仅 deploy 命令内部会用到，其余动作用容器名）
if docker compose version &>/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose &>/dev/null; then
  DC="docker-compose"
else
  DC=""
fi

cmd="${1:-help}"; shift || true

case "$cmd" in
  deploy)
    echo ">>> 执行 deploy.sh 重新部署（保留现有 data/assets.db）"
    sudo bash deploy.sh
    ;;

  status)
    docker ps -a --filter "name=ams-backend" --filter "name=ams-nginx" \
      --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    ;;

  logs)
    n="${1:-50}"
    docker logs --tail "$n" -f ams-backend
    ;;

  restart)
    echo ">>> 重启 ams-backend"
    docker restart ams-backend
    sleep 2
    curl -s -m 5 http://localhost:8088/api/auth/check | head -c 200; echo
    ;;

  backup-db)
    ts=$(date +%Y%m%d-%H%M%S)
    command cp -f "$DB" "data/assets.db.bak-$ts"
    echo "✓ 已备份 -> data/assets.db.bak-$ts"
    ;;

  swap-db)
    [ -z "$1" ] && { echo "用法: bash manage.sh swap-db <db文件>"; exit 1; }
    [ -f "$1" ] || { echo "✗ 文件不存在: $1"; exit 1; }
    src="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
    echo ">>> 1/4 停止后端"; docker stop ams-backend 2>/dev/null || true
    ts=$(date +%Y%m%d-%H%M%S)
    echo ">>> 2/4 备份旧库 -> data/assets.db.bak-$ts"; command cp -f "$DB" "data/assets.db.bak-$ts"
    echo ">>> 3/4 换入 $src"; command cp -f "$src" "$DB"
    echo ">>> 4/4 启动后端"; docker start ams-backend 2>/dev/null || docker restart ams-backend
    echo ">>> 等待健康检查..."; sleep 3
    if curl -s -m 5 http://localhost:8088/api/auth/check | grep -q success; then
      echo "✓ 后端已用新库启动，API 正常"
    else
      echo "✗ 后端可能未就绪，请执行: bash manage.sh logs"
    fi
    ;;

  restore-db)
    [ -z "$1" ] && { echo "用法: bash manage.sh restore-db <db文件>"; exit 1; }
    [ -f "$1" ] || { echo "✗ 文件不存在: $1"; exit 1; }
    src="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
    docker stop ams-backend 2>/dev/null || true
    command cp -f "$src" "$DB"
    docker start ams-backend 2>/dev/null || docker restart ams-backend
    echo "✓ 已恢复: $src"
    ;;

  bind-lark)
    [ -z "$1" ] && { echo "用法: bash manage.sh bind-lark <LarkUserID> [员工ID]"; echo "  例: bash manage.sh bind-lark 85ca2c44 1"; exit 1; }
    LARK_ID="$1"; EMP_ID="${2:-1}"
    echo ">>> 将 Lark ID=$LARK_ID 绑定到员工 id=$EMP_ID"
    docker exec ams-backend python3 - <<PY
import sqlite3
c = sqlite3.connect('/app/data/assets.db')
# 先解除该 Lark ID 在其他账号上的绑定，避免重复
c.execute("UPDATE employees SET lark_user_id=NULL, lark_open_id=NULL WHERE lark_user_id=?", ("$LARK_ID",))
c.execute("UPDATE employees SET lark_user_id=?, lark_open_id=? WHERE id=?", ("$LARK_ID","$LARK_ID", $EMP_ID))
c.commit()
r = c.execute("SELECT id, name, role, lark_user_id FROM employees WHERE id=?", ($EMP_ID,)).fetchone()
print("✓ 绑定结果:", dict(r) if r else "员工不存在")
c.close()
PY
    ;;

  set-superadmin)
    # 默认参数即 Cookie.gu / 古基 的唯一超管配置；可按需覆盖
    # 用法: bash manage.sh set-superadmin [LarkID] [邮箱] [姓名] [所在地] [密码]
    LARK_ID="${1:-85ca2c44}"
    EMAIL="${2:-cookie.gu@nhwhk.com}"
    NAME="${3:-古基}"
    LOC="${4:-深圳}"
    PWD="${5:-Aaa77377}"
    echo ">>> 强绑定唯一超管: $NAME <$EMAIL> @$LOC  Lark=$LARK_ID"
    docker exec ams-backend python3 - "$LARK_ID" "$EMAIL" "$NAME" "$LOC" "$PWD" <<'PY'
import sqlite3, sys
LARK, EMAIL, NAME, LOC, PWD = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
c = sqlite3.connect('/app/data/assets.db'); c.row_factory = sqlite3.Row
# 1) 解除该 Lark ID 在其他账号上的绑定，避免登录歧义
c.execute("UPDATE employees SET lark_user_id=NULL, lark_open_id=NULL WHERE lark_user_id=?", (LARK,))
# 2) 定位目标账号：优先沿用“现有超管”（即之前的那个超管），其次 email/name，都没有则新建
row = c.execute("SELECT id FROM employees WHERE role='超管' ORDER BY id LIMIT 1").fetchone()
if not row:
    row = c.execute("SELECT id FROM employees WHERE email=?", (EMAIL,)).fetchone()
if not row:
    row = c.execute("SELECT id FROM employees WHERE name=?", (NAME,)).fetchone()
if row:
    eid = row['id']
else:
    c.execute("INSERT INTO employees (name, location, email, role, active, lark_only, status, created_at) VALUES (?,?,?,?,1,0,'在职', datetime('now','localtime'))",
              (NAME, LOC, EMAIL, '超管'))
    eid = c.lastrowid
# 3) 强绑定完整资料 + 超管 + 密码 + Lark（超管 lark_only 固定为 0：可密码登录也可免登）
c.execute("""UPDATE employees SET name=?, location=?, email=?, role='超管', active=1,
                    lark_only=0, status='在职', password=?, lark_user_id=?, lark_open_id=?
             WHERE id=?""", (NAME, LOC, EMAIL, PWD, LARK, LARK, eid))
# 4) 其余所有超管降级为普通用户，确保“唯一超管”
c.execute("UPDATE employees SET role='普通用户' WHERE id<>? AND role='超管'", (eid,))
c.commit()
r = c.execute("SELECT id,name,email,role,location,lark_user_id,lark_only,active FROM employees WHERE id=?", (eid,)).fetchone()
print("✓ 唯一超管已就绪:", dict(r))
print("✓ 当前超管总数:", c.execute("SELECT count(*) FROM employees WHERE role='超管'").fetchone()[0])
c.close()
PY
    ;;

  *)
    echo "资产管理系统 运维一键脚本"
    echo ""
    echo "用法: bash manage.sh <命令> [参数]"
    echo "  deploy            重新部署（保留现有数据库）"
    echo "  status            查看容器状态"
    echo "  logs [行数]        跟踪后端日志（默认50）"
    echo "  restart           重启后端"
    echo "  backup-db         备份数据库（带时间戳）"
    echo "  swap-db <文件>    安全替换数据库并重启（验证用）"
    echo "  restore-db <文件>  用备份恢复数据库"
    echo "  bind-lark <ID> <员工ID>  强绑定 Lark ID（默认员工id=1）"
    echo "  set-superadmin [LarkID] [邮箱] [姓名] [所在地] [密码]  强绑定唯一超管（默认 Cookie.gu/古基/Aaa77377）"
    ;;
esac
