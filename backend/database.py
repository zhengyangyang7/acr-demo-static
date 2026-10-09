"""
资产管理系统 - 数据库初始化与 ORM 模型
"""
import sqlite3
import os
import hashlib
import secrets
import threading
from datetime import datetime

DB_PATH = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(__file__), '..', 'data')) + '/assets.db'


def _data_dir():
    return os.path.dirname(os.path.abspath(DB_PATH))


def write_initial_admin_password(password: str) -> str:
    """把超管初始明文密码写入 data/INITIAL_ADMIN_PASSWORD.txt，便于 Docker 部署后查看。
    返回写入路径。仅应在「新建超管」或交付包预设时调用，勿在每次启动覆盖。"""
    path = os.path.join(_data_dir(), 'INITIAL_ADMIN_PASSWORD.txt')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = (
        "企业资产管理系统 — 超管初始密码\n"
        "================================\n"
        f"账号: Cookie.gu\n"
        f"密码: {password}\n"
        "\n"
        "请登录后立即修改密码。本文件仅便于首次部署查看，\n"
        "生产环境改密后可删除本文件。\n"
    )
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 加盐哈希，格式 pbkdf2$iterations$salt_hex$hash_hex。
    仅用标准库（hashlib.pbkdf2_hmac + secrets），零外部依赖（避免引入 bcrypt 供应链风险）。"""
    salt = secrets.token_bytes(16)
    iterations = 150000
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。支持 PBKDF2 哈希格式；为兼容存量明文，非 pbkdf2$ 前缀时按明文比对
    （登录校验通过后由调用方立即升级为哈希，完成平滑迁移，根治明文存储）。"""
    if not password or not stored:
        return False
    if stored.startswith('pbkdf2$'):
        try:
            _tag, iterations, salt_hex, hash_hex = stored.split('$')
            dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                     bytes.fromhex(salt_hex), int(iterations))
            return secrets.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    return secrets.compare_digest(password.encode('utf-8'), stored.encode('utf-8'))


# 线程本地连接池 —— 避免每次请求都新建 SQLite 连接
_local = threading.local()


class _SafeConnection(sqlite3.Connection):
    """包装 SQLite 连接：业务代码调用 close() 时一律设为空操作（不提交也不回滚），
    避免嵌套调用（如 get_current_user_id→verify_token、notify_lark 内部 close）把外层
    正在使用且尚未提交的事务关闭/回滚，从而解决两类问题：
      1. 'Cannot operate on a closed database'（外层连接被关闭）
      2. 事务被提前回滚导致已执行的 UPDATE/INSERT 丢失（如 execute_operation 内
         调用 notify_lark 触发 conn.close() 把待提交事务回滚）
    真正的关闭由 close_conn_on_teardown() 调用 real_close() 在请求结束时完成。"""
    def close(self):
        pass

    def real_close(self):
        sqlite3.Connection.close(self)


def get_conn():
    """获取可复用的数据库连接（线程安全）。"""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            pass  # 连接已失效，重新创建
    conn = sqlite3.connect(DB_PATH, factory=_SafeConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    _local.conn = conn
    return conn


def close_conn_on_teardown(exc=None):
    """请求结束时真正关闭当前线程持有的共享连接，避免跨请求遗留事务/连接。"""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        try:
            conn.real_close()
        except Exception:
            pass
        _local.conn = None


def init_db():
    conn = get_conn()
    c = conn.cursor()

    # ── 资产主表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS assets (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_no    TEXT UNIQUE NOT NULL,          -- 资产编号
        category    TEXT NOT NULL,                 -- 类目
        brand       TEXT,                          -- 品牌
        model       TEXT,                          -- 型号
        spec        TEXT,                          -- 配置信息
        sn          TEXT,                          -- S/N 序列号
        mac_wired   TEXT,                          -- 有线 MAC
        mac_wireless TEXT,                         -- 无线 MAC
        status      TEXT NOT NULL DEFAULT '在库',  -- 在库/在用/维修中/已报废/已出库
        location    TEXT,                          -- 存放地/办公室
        assignee    TEXT,                          -- 当前关联人
        key_info    TEXT,                          -- KEY
        account_pwd TEXT,                          -- 关联账号密码
        purchase_date TEXT,                        -- 采购/入库时间
        warranty_expire TEXT,                      -- 保修到期日
        purchase_price REAL,                       -- 采购价格
        notes       TEXT,                          -- 备注
        created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # ── 出入库记录表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS stock_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id    INTEGER NOT NULL REFERENCES assets(id),
        asset_no    TEXT NOT NULL,
        log_type    TEXT NOT NULL,    -- 入库/出库/归还/调拨
        operator    TEXT,             -- 经办人
        target_user TEXT,             -- 领用/归还人
        from_location TEXT,
        to_location TEXT,
        remark      TEXT,
        log_time    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        notified    INTEGER DEFAULT 0  -- 是否已推送飞书
    )''')

    # ── 维修记录表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS repair_logs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id     INTEGER NOT NULL REFERENCES assets(id),
        asset_no     TEXT NOT NULL,
        fault_desc   TEXT,            -- 故障描述
        repair_vendor TEXT,           -- 维修供应商
        submit_by    TEXT,            -- 提交人
        submit_time  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        repair_status TEXT DEFAULT '待处理',  -- 待处理/维修中/已完成/无法修复
        finish_time  TEXT,
        repair_cost  REAL,
        repair_notes TEXT,
        notified     INTEGER DEFAULT 0
    )''')

    # ── 人员台账（可选，用于下拉选择） ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS employees (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        name     TEXT NOT NULL,
        dept     TEXT,
        location TEXT,
        email    TEXT,
        role     TEXT DEFAULT '普通用户',
        active   INTEGER DEFAULT 1,
        avatar   TEXT,                     -- 头像URL
        lark_user_id  TEXT,                -- Lark User ID（租户内唯一，管理员可见，绑定键）
        lark_open_id  TEXT,                -- Lark Open ID（应用内标识，仅 OAuth 可获取）
        lark_union_id TEXT,                -- Lark Union ID（同开发者跨应用唯一，历史脏数据迁入）
        status   TEXT DEFAULT '在职',      -- 在职/离职
        password TEXT,                     -- 密码（超管用）
        lark_only INTEGER DEFAULT 1,       -- 是否强制Lark访问（0=不限制，1=必须Lark）
        created_at  TEXT DEFAULT (datetime('now','localtime')),
        last_login  TEXT,                   -- 最后登录时间
        custom_fields TEXT DEFAULT '{}'     -- 自定义字段JSON（如性别等）
    )''')
    
    # 添加 password 字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE employees ADD COLUMN password TEXT")
    except:
        pass  # 字段已存在
    
    # 添加 lark_only 字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE employees ADD COLUMN lark_only INTEGER DEFAULT 1")
    except:
        pass  # 字段已存在
    
    # 添加 status 字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE employees ADD COLUMN status TEXT DEFAULT '在职'")
    except:
        pass  # 字段已存在
    
    # 添加 last_login 字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE employees ADD COLUMN last_login TEXT")
    except:
        pass  # 字段已存在
    
    # 添加 custom_fields 字段（兼容旧数据库，存储自定义字段JSON）
    try:
        c.execute("ALTER TABLE employees ADD COLUMN custom_fields TEXT DEFAULT '{}'")
    except:
        pass  # 字段已存在
    
    # 添加 lark_union_id 字段（兼容旧数据库，存 Lark Union ID）
    try:
        c.execute("ALTER TABLE employees ADD COLUMN lark_union_id TEXT")
    except:
        pass  # 字段已存在

    # 迁移历史脏数据：旧版本把 union_id(on_ 前缀) 误存进 lark_user_id，
    # 现迁到 lark_union_id，lark_user_id 只保留真正的 Lark User ID（租户内唯一）。
    # 幂等：迁移后 lark_user_id 被清空，on_ 前缀不再存在，重复执行无副作用。
    try:
        c.execute("""
            UPDATE employees
            SET lark_union_id = lark_user_id, lark_user_id = ''
            WHERE lark_user_id LIKE 'on_%' AND (lark_union_id IS NULL OR lark_union_id = '')
        """)
    except:
        pass  # 迁移失败不阻断启动
    
    # 创建默认超管账号（仅当不存在时创建；已存在则不再强制重置密码）
    # 修复：历史版本每次启动都 UPDATE password='Aaa77377'，导致用户改密后被重启覆盖（P0-1）。
    admin = c.execute("SELECT * FROM employees WHERE role = '超管' LIMIT 1").fetchone()
    if not admin:
        # 安全加固（复审⑦）：初始密码改为随机生成（仅首次创建时打印一次），
        # 不再硬编码 Aaa77377，避免源码/日志泄露后任何人可用固定口令登录超管。
        import secrets as _s
        init_pwd = 'Ams@' + _s.token_urlsafe(6)
        c.execute('''
            INSERT INTO employees (name, role, dept, email, password, lark_only, status, created_at)
            VALUES ('Cookie.gu', '超管', 'IT部', 'admin@company.com', ?, 0, '在职', datetime('now','localtime'))
        ''', (hash_password(init_pwd),))
        try:
            pwd_file = write_initial_admin_password(init_pwd)
            print(f"[OK] 已创建默认超管账号: Cookie.gu，初始密码: {init_pwd}", flush=True)
            print(f"[OK] 初始密码已写入文件（部署后可直接打开）: {pwd_file}", flush=True)
        except Exception as _e:
            print(f"[OK] 已创建默认超管账号: Cookie.gu，初始密码: {init_pwd}（写文件失败: {_e}）", flush=True)
    else:
        # 超管已存在：只同步基础属性，不重置密码（避免覆盖用户改密）。
        c.execute("UPDATE employees SET name = 'Cookie.gu', lark_only = 0 WHERE role = '超管'")
        # 安全加固（复审2-②）：存量明文密码（含种子库 seed 铺入的）启动时自动升级为哈希，
        # 解决「seed 铺库绕过随机密码修复」——无论密码来源，只要仍是明文就立即哈希化
        # （密码值不变，仍可用原密码登录，登录后即为哈希存储）。覆盖所有超管行（含重复超管）。
        for _row in c.execute("SELECT id, password FROM employees WHERE role='超管'").fetchall():
            _pwd = _row['password'] or ''
            if _pwd and not _pwd.startswith('pbkdf2$'):
                c.execute("UPDATE employees SET password=? WHERE id=?", (hash_password(_pwd), _row['id']))

    # ── 飞书通知配置 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS lark_config (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,           -- 配置名称（如"深圳IT群"）
        webhook_url TEXT NOT NULL,
        secret      TEXT,                    -- 签名密钥（可选）
        enabled     INTEGER DEFAULT 1,
        notify_stock  INTEGER DEFAULT 1,     -- 出入库通知
        notify_repair INTEGER DEFAULT 1,     -- 维修通知
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # ── 耗材主表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS consumables (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        item_no     TEXT UNIQUE NOT NULL,          -- 耗材编号
        name        TEXT NOT NULL,                 -- 耗材名称
        category    TEXT NOT NULL,                 -- 类目（如：墨盒、硒鼓、纸张、网线等）
        brand       TEXT,                          -- 品牌
        model       TEXT,                          -- 型号/规格
        unit        TEXT DEFAULT '个',             -- 单位
        quantity    INTEGER NOT NULL DEFAULT 0,    -- 当前库存数量
        min_stock   INTEGER DEFAULT 10,            -- 库存预警阈值
        location    TEXT,                          -- 存放位置
        status      TEXT NOT NULL DEFAULT '正常',  -- 正常/低库存/停用
        notes       TEXT,                          -- 备注
        created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # ── 耗材出入库记录表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS consumable_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id     INTEGER NOT NULL REFERENCES consumables(id),
        item_no     TEXT NOT NULL,
        log_type    TEXT NOT NULL,    -- 入库/出库
        quantity    INTEGER NOT NULL, -- 数量（正数）
        operator    TEXT,             -- 经办人
        target_user TEXT,             -- 领用人
        location    TEXT,             -- 存放位置（出库时记录）
        remark      TEXT,
        log_time    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # ── 系统配置 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS sys_config (
        key   TEXT PRIMARY KEY,
        value TEXT,
        desc  TEXT
    )''')

    # 默认系统配置
    defaults = [
        ('asset_no_prefix', 'ASSET', '资产编号前缀'),
        ('company_name', '企业资产管理', '公司/系统名称'),
        ('consumable_default_min_stock', '10', '耗材默认预警阈值'),
        ('consumable_low_stock_notify', '1', '耗材低库存通知(1=开启,0=关闭)'),
    ]
    for key, val, desc in defaults:
        c.execute('INSERT OR IGNORE INTO sys_config(key,value,desc) VALUES(?,?,?)',
                  (key, val, desc))

    # ── 照片记录表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS photos (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ref_type    TEXT NOT NULL,        -- 关联类型: stock/repair/dispose/asset/consumable
        ref_id      INTEGER NOT NULL,     -- 关联记录ID
        file_name   TEXT NOT NULL,        -- 文件名
        file_path   TEXT NOT NULL,        -- 文件路径
        file_size   INTEGER,              -- 文件大小
        mime_type   TEXT,                 -- MIME类型
        uploaded_by TEXT,                 -- 上传人
        uploaded_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # ── 资产处理记录表（报废、捐赠、出售等） ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS dispose_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id    INTEGER NOT NULL REFERENCES assets(id),
        asset_no    TEXT NOT NULL,
        dispose_type TEXT NOT NULL,       -- 报废/捐赠/出售/丢失/其他
        reason      TEXT,                 -- 处理原因
        operator    TEXT,                 -- 经办人
        approved_by TEXT,                 -- 审批人
        dispose_date TEXT,                -- 处理日期
        notes       TEXT,                 -- 备注
        created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # ── 删除申请表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS delete_applications (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        target_type TEXT NOT NULL,        -- 删除对象类型: asset/consumable
        target_id   INTEGER NOT NULL,     -- 删除对象ID
        target_no   TEXT NOT NULL,        -- 删除对象编号/名称
        target_info TEXT,                 -- 删除对象信息(JSON存储)
        apply_by    TEXT NOT NULL,        -- 申请人
        apply_time  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        reason      TEXT,                 -- 删除原因
        status      TEXT DEFAULT 'pending', -- pending/approved/rejected
        approver    TEXT,                 -- 审批人(超管)
        approve_time TEXT,                -- 审批时间
        approve_note TEXT,                -- 审批备注
        notified    INTEGER DEFAULT 0     -- 是否已通知(预留飞书)
    )''')

    # ── 采购入库审批表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS stock_in_applications (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id     INTEGER NOT NULL,                    -- 关联采购订单ID
        order_no     TEXT NOT NULL,                       -- 订单号(冗余便于显示)
        asset_ids    TEXT NOT NULL,                       -- 本批新建资产ID列表(JSON数组)
        apply_by     TEXT NOT NULL,                       -- 申请人
        apply_time   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        status       TEXT DEFAULT 'pending',              -- pending/approved/rejected
        reviewed_by  TEXT,                               -- 审批人(超管)
        reviewed_at  TEXT,                               -- 审批时间
        remark       TEXT,                               -- 审批备注
        notified     INTEGER DEFAULT 0                    -- 是否已通知(预留飞书)
    )''')

    # ── 资产申请表（换设备/加显示器/配件/借用/报修等，走审批中心）──
    # 注：此前仅靠运行库残留存在，init_db 未声明，回滚不自包含；此处补齐使其幂等
    c.execute('''
    CREATE TABLE IF NOT EXISTS asset_applications (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        app_no          TEXT,                              -- 申请单号 APP-YYYYMMDD-XXXX
        app_type        TEXT NOT NULL,                    -- new_asset/borrow/return/repair/consumable
        applicant_id    INTEGER,                          -- 申请人ID(关联employees)
        applicant_name  TEXT,                             -- 申请人姓名
        asset_id        INTEGER,                          -- 关联资产ID(可选)
        asset_no        TEXT,                             -- 关联资产编号(可选)
        title           TEXT,                             -- 申请标题
        reason          TEXT,                             -- 申请原因/自定义文本
        detail          TEXT,                             -- 明细(JSON)
        status          TEXT DEFAULT 'pending',           -- pending/approved/rejected
        reviewer_id     INTEGER,                          -- 审批人ID
        reviewer_name   TEXT,                             -- 审批人姓名
        review_comment  TEXT,                             -- 审批意见
        apply_time      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        review_time     TEXT,                             -- 审批时间
        close_time      TEXT,                             -- 关闭时间
        created_at      TEXT DEFAULT (datetime('now','localtime')),
        fulfilled       INTEGER DEFAULT 0,                -- 新资产类申请是否已发放（0=待发放）
        po_id           INTEGER                           -- 关联采购订单（可空）
    )''')

    # 兼容旧库：asset_applications 补 fulfilled / po_id
    try:
        c.execute("ALTER TABLE asset_applications ADD COLUMN fulfilled INTEGER DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE asset_applications ADD COLUMN po_id INTEGER")
    except:
        pass

    # ── 资产操作审批表（出库/回收/调拨/维修/处置，均走审批中心确认后才执行）──
    c.execute('''
    CREATE TABLE IF NOT EXISTS operation_applications (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        op_type      TEXT NOT NULL,        -- stock_out/stock_return/transfer/asset_transfer/repair/dispose
        asset_ids    TEXT NOT NULL,        -- 受影响资产ID列表(JSON数组)
        asset_nos    TEXT,                 -- 受影响资产编号列表(JSON数组，便于展示)
        payload      TEXT NOT NULL,        -- 操作参数(JSON)
        applicant    TEXT NOT NULL,        -- 申请人
        applicant_id INTEGER,              -- 申请人ID
        apply_time   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        status       TEXT DEFAULT 'pending',   -- pending/approved/rejected
        reviewer     TEXT,                   -- 审批人(超管)
        review_time  TEXT,                    -- 审批时间
        remark       TEXT,                    -- 审批备注
        proof_info   TEXT,                    -- 处置证明信息(JSON: [{file_name,file_path}])
        notified     INTEGER DEFAULT 0         -- 是否已通知(预留飞书)
    )''')

    # ── 盘点任务表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS inventory_tasks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_no     TEXT UNIQUE NOT NULL, -- 盘点单号
        task_name   TEXT NOT NULL,        -- 盘点任务名称
        scope_type  TEXT NOT NULL,        -- 盘点范围: all(全部)/in_stock(仅库存)/location(指定位置)
        scope_value TEXT,                 -- 范围值(如位置名称，scope_type=location时使用)
        status      TEXT DEFAULT 'pending', -- pending(待盘点)/ongoing(盘点中)/completed(已完成)/cancelled(已取消)
        created_by  TEXT NOT NULL,        -- 创建人
        created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        started_at  TEXT,                 -- 开始盘点时间
        completed_at TEXT,                -- 完成时间
        total_count INTEGER DEFAULT 0,    -- 应盘资产总数
        checked_count INTEGER DEFAULT 0,  -- 已盘点数量
        normal_count INTEGER DEFAULT 0,   -- 正常数量
        abnormal_count INTEGER DEFAULT 0, -- 异常数量(盘亏/盘盈)
        notes       TEXT                  -- 备注
    )''')

    # ── 采购订单表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS purchase_orders (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no     TEXT UNIQUE NOT NULL,         -- 订单编号，如 PO-20260410-001
        title        TEXT NOT NULL,                -- 订单标题/描述
        supplier     TEXT,                         -- 供应商
        order_date   TEXT,                         -- 下单日期
        expected_date TEXT,                        -- 预计到货日期
        actual_date  TEXT,                         -- 实际到货日期
        total_amount REAL DEFAULT 0,               -- 订单总金额
        currency     TEXT DEFAULT 'CNY',           -- 货币
        status       TEXT DEFAULT '待到货',        -- 待到货/部分到货/已到货/已取消
        purchase_by  TEXT,                         -- 采购人
        invoice_no   TEXT,                         -- 发票号
        contract_no  TEXT,                         -- 合同号
        notes        TEXT,
        created_by   TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # ── 订单明细表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS order_items (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id     INTEGER NOT NULL REFERENCES purchase_orders(id),
        item_type    TEXT NOT NULL DEFAULT 'asset',  -- asset / consumable
        item_name    TEXT NOT NULL,                -- 品名/型号描述
        category     TEXT,                         -- 类目
        brand        TEXT,
        model        TEXT,
        quantity     INTEGER NOT NULL DEFAULT 1,   -- 采购数量
        unit_price   REAL DEFAULT 0,               -- 单价
        arrived_qty  INTEGER DEFAULT 0,            -- 已到货数量（入库后累加）
        notes        TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # assets 表加 order_id 字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE assets ADD COLUMN order_id INTEGER REFERENCES purchase_orders(id)")
    except:
        pass
    try:
        c.execute("ALTER TABLE assets ADD COLUMN order_item_id INTEGER REFERENCES order_items(id)")
    except:
        pass
    # assets 表加 关联账号/密码 独立字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE assets ADD COLUMN account_name TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE assets ADD COLUMN account_password TEXT")
    except:
        pass

    # consumables 表加 order_id 字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE consumables ADD COLUMN order_id INTEGER REFERENCES purchase_orders(id)")
    except:
        pass
    try:
        c.execute("ALTER TABLE consumables ADD COLUMN order_item_id INTEGER REFERENCES order_items(id)")
    except:
        pass

    # stock_logs 加 order_id（兼容旧数据库）
    try:
        c.execute("ALTER TABLE stock_logs ADD COLUMN order_id INTEGER REFERENCES purchase_orders(id)")
    except:
        pass

    # consumable_logs 加 order_id（兼容旧数据库）
    try:
        c.execute("ALTER TABLE consumable_logs ADD COLUMN order_id INTEGER REFERENCES purchase_orders(id)")
    except:
        pass

    # purchase_orders 加费控系统订单号字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE purchase_orders ADD COLUMN expense_order_no TEXT")
    except:
        pass

    # purchase_orders 加发票文件路径字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE purchase_orders ADD COLUMN invoice_file TEXT")
    except:
        pass

    # purchase_orders 加主体和地区字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE purchase_orders ADD COLUMN subject_code TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE purchase_orders ADD COLUMN region_code TEXT")
    except:
        pass

    # ── 盘点明细表 ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS inventory_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     INTEGER NOT NULL REFERENCES inventory_tasks(id),
        asset_id    INTEGER NOT NULL REFERENCES assets(id),
        asset_no    TEXT NOT NULL,        -- 资产编号
        asset_info  TEXT,                 -- 资产信息快照(JSON)
        
        -- 盘点前状态
        before_status TEXT,               -- 盘点前资产状态
        before_location TEXT,             -- 盘点前位置
        before_assignee TEXT,             -- 盘点前使用人
        
        -- 盘点结果
        check_status TEXT DEFAULT 'pending', -- pending(待盘点)/normal(正常)/abnormal(异常)/missing(盘亏)
        check_method TEXT,                -- 盘点方式: manual(手动)/scan(扫码)/self_confirm(责任人确认)
        checked_by  TEXT,                 -- 盘点人/确认人
        checked_at  TEXT,                 -- 盘点时间
        
        -- 异常记录
        abnormal_type TEXT,               -- 异常类型: missing(盘亏)/location_mismatch(位置不符)/status_mismatch(状态不符)/other(其他)
        abnormal_desc TEXT,               -- 异常描述
        
        -- 责任人确认（分发资产使用）
        confirm_required INTEGER DEFAULT 0, -- 是否需要责任人确认
        confirm_sent_at TEXT,             -- 通知发送时间
        confirm_deadline TEXT,            -- 确认截止时间
        
        notes       TEXT,                 -- 盘点备注
        created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # ── 资产主体表（公司/BU） ──────────────────────────────────────────
    c.execute('''
    CREATE TABLE IF NOT EXISTS asset_subjects (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        code        TEXT UNIQUE NOT NULL,    -- 主体代码，如 NHWHK
        name        TEXT NOT NULL,           -- 主体名称，如 香港华科
        region      TEXT,                    -- 所属地区，如 香港/北京
        active      INTEGER DEFAULT 1,       -- 是否启用
        sort_order  INTEGER DEFAULT 0,      -- 排序
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # ── 资产类型表 ────────────────────────────────────────────────────
    c.execute('''
    CREATE TABLE IF NOT EXISTS asset_types (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        code        TEXT UNIQUE NOT NULL,    -- 类型代码，如 LT/DP/APP
        name        TEXT NOT NULL,           -- 类型名称，如 笔记本/显示器/软件
        active      INTEGER DEFAULT 1,
        sort_order  INTEGER DEFAULT 0,
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # ── 资产存放地/地区表 ──────────────────────────────────────────────
    c.execute('''
    CREATE TABLE IF NOT EXISTS asset_regions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        code        TEXT UNIQUE NOT NULL,    -- 地区代码，如 SZ/BJ/HK
        name        TEXT NOT NULL,           -- 地区名称，如 深圳/北京/香港
        active      INTEGER DEFAULT 1,
        sort_order  INTEGER DEFAULT 0,
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # ── 操作日志表 ────────────────────────────────────────────────────
    c.execute('''
    CREATE TABLE IF NOT EXISTS action_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        action_type TEXT NOT NULL,            -- 操作类型: create/update/delete/approve/reject/arrive/cancel/upload
        module      TEXT NOT NULL,            -- 模块: asset/consumable/order/repair/dispose/inventory/approval
        target_type TEXT,                     -- 目标类型: asset/consumable/order等
        target_id   INTEGER,                  -- 目标ID
        target_no   TEXT,                     -- 目标编号(如资产编号、订单号)
        target_name TEXT,                     -- 目标名称/标题
        detail      TEXT,                     -- 操作详情(JSON存储)
        before_data TEXT,                     -- 修改前数据(JSON)
        after_data  TEXT,                     -- 修改后数据(JSON)
        operator    TEXT NOT NULL,            -- 操作人
        operator_role TEXT,                   -- 操作人角色: 超管/普通用户
        ip_address  TEXT,                     -- IP地址
        user_agent  TEXT,                     -- 浏览器信息
        created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    
    # 添加索引
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_action_logs_module ON action_logs(module)")
    except:
        pass
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_action_logs_operator ON action_logs(operator)")
    except:
        pass
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_action_logs_created_at ON action_logs(created_at)")
    except:
        pass
    
    # ── 订单修改申请表 ───────────────────────────────────────────────
    c.execute('''
    CREATE TABLE IF NOT EXISTS order_modifications (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id        INTEGER NOT NULL REFERENCES purchase_orders(id),
        order_no        TEXT NOT NULL,                 -- 订单编号
        title           TEXT NOT NULL,                 -- 订单标题
        modifications    TEXT NOT NULL,                 -- 修改内容(JSON): {field: {old, new}}
        reason          TEXT,                           -- 修改原因
        attachments      TEXT,                          -- 附件列表(JSON): [{file_name, file_path, file_size}]
        apply_by        TEXT NOT NULL,                  -- 申请人
        apply_by_role   TEXT NOT NULL,                  -- 申请人角色
        status          TEXT DEFAULT 'pending',          -- pending/approved/rejected
        approver        TEXT,                           -- 审批人
        approve_time    TEXT,                           -- 审批时间
        approve_note    TEXT,                           -- 审批备注
        created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # ── 资产类型参数表（折旧、可发放年限等）────────────────────────────
    c.execute('''
    CREATE TABLE IF NOT EXISTS asset_type_params (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        type_code       TEXT UNIQUE NOT NULL,    -- 资产类型代码，如 LT/DP/APP
        type_name       TEXT NOT NULL,           -- 资产类型名称，如 笔记本/显示器
        depreciation_months INTEGER DEFAULT 36,  -- 折旧月数
        residual_rate   REAL DEFAULT 0.05,       -- 残值率（0.05 = 5%）
        max_issue_years INTEGER DEFAULT 2,       -- 可发放年限（0表示不限）
        created_at      TEXT DEFAULT (datetime('now','localtime')),
        updated_at      TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # 初始化默认配置数据
    _init_asset_config(c)
    # 初始化资产类型参数
    _init_asset_type_params(c)
    # 初始化默认字段选项
    _init_field_options(c)

    # assets 表添加新字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE assets ADD COLUMN subject_code TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE assets ADD COLUMN region_code TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE assets ADD COLUMN asset_type_code TEXT")
    except:
        pass

    # assets 表添加是否可借字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE assets ADD COLUMN is_borrowable INTEGER DEFAULT 0")
    except:
        pass

    # 软件资产 & 续期提醒相关字段（兼容旧数据库）
    try:
        c.execute("ALTER TABLE assets ADD COLUMN asset_class TEXT DEFAULT 'hardware'")
    except:
        pass
    try:
        c.execute("ALTER TABLE assets ADD COLUMN license_type TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE assets ADD COLUMN expire_date TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE assets ADD COLUMN seat_count INTEGER")
    except:
        pass
    try:
        c.execute("ALTER TABLE assets ADD COLUMN renew_remind_days INTEGER DEFAULT 30")
    except:
        pass

    # 按类目名称回填空的 asset_type_code（使账面价值可算）
    try:
        c.execute("""
            UPDATE assets
            SET asset_type_code = (
                SELECT p.type_code FROM asset_type_params p
                WHERE p.type_name = assets.category LIMIT 1
            )
            WHERE (asset_type_code IS NULL OR asset_type_code = '')
              AND category IS NOT NULL AND category != ''
              AND EXISTS (
                SELECT 1 FROM asset_type_params p WHERE p.type_name = assets.category
              )
        """)
    except:
        pass

    # lark_config 加续期提醒通知开关（兼容旧数据库）
    try:
        c.execute("ALTER TABLE lark_config ADD COLUMN notify_expire INTEGER DEFAULT 1")
    except:
        pass

    # token 持久化表（gunicorn worker 回收不会丢失登录态）
    c.execute("""
        CREATE TABLE IF NOT EXISTS active_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES employees(id)
        )
    """)

    # 飞书待绑定表：同名/新用户首次飞书登录时，不直接绑定，
    # 而是生成待绑定记录并通知超管，由管理员手动确认绑定。
    c.execute("""
        CREATE TABLE IF NOT EXISTS lark_pending_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lark_user_id TEXT,
            lark_open_id TEXT,
            lark_union_id TEXT,
            name TEXT,
            avatar TEXT,
            department TEXT,
            email TEXT,
            candidate_employee_id INTEGER,   -- 系统按姓名预匹配到的未绑定员工（仅供参考）
            status TEXT DEFAULT 'pending',   -- pending / approved / rejected
            handled_by INTEGER,
            handled_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)

    # 兼容旧库：lark_pending_bindings 补 lark_union_id 字段
    try:
        c.execute("ALTER TABLE lark_pending_bindings ADD COLUMN lark_union_id TEXT")
    except:
        pass  # 字段已存在

    conn.commit()
    conn.close()


def _init_field_options(c):
    """初始化默认字段选项数据"""
    # 资产类别
    categories = [
        ('LT', '笔记本', '#1677ff'),
        ('DP', '显示器', '#722ed1'),
        ('PC', '台式电脑', '#13c2c2'),
        ('NB', '网络设备', '#52c41a'),
        ('PR', '打印机', '#fa8c16'),
        ('PHONE', '手机', '#eb2f96'),
        ('PAD', '平板', '#faad14'),
        ('APP', '软件', '#8c8c8c'),
        ('OTH', '其他', '#bfbfbf'),
    ]
    for i, (code, name, color) in enumerate(categories):
        c.execute("INSERT OR IGNORE INTO field_options(field_type, field_code, field_name, color, sort_order) VALUES(?,?,?,?,?)",
                  ('category', code, name, color, i))

    # 耗材类别
    consumable_categories = [
        ('INK', '墨盒/硒鼓', '#1677ff'),
        ('PAPER', '纸张', '#52c41a'),
        ('CABLE', '线缆', '#fa8c16'),
        ('MOUSE', '鼠标键盘', '#722ed1'),
        ('STORAGE', '存储设备', '#13c2c2'),
        ('OTHER', '其他', '#8c8c8c'),
    ]
    for i, (code, name, color) in enumerate(consumable_categories):
        c.execute("INSERT OR IGNORE INTO field_options(field_type, field_code, field_name, color, sort_order) VALUES(?,?,?,?,?)",
                  ('consumable_category', code, name, color, i))

    # 品牌
    brands = [
        ('DELL', '戴尔', '#1677ff'),
        ('HP', '惠普', '#00857c'),
        ('LENOVO', '联想', '#e5004d'),
        ('HUAWEI', '华为', '#cf0a2c'),
        ('APPLE', '苹果', '#555555'),
        ('ASUS', '华硕', '#0058a3'),
        ('ACER', '宏碁', '#004098'),
        ('SAMSUNG', '三星', '#1428a0'),
        ('MICROSOFT', '微软', '#00a4ef'),
        ('OTHER', '其他', '#8c8c8c'),
    ]
    for i, (code, name, color) in enumerate(brands):
        c.execute("INSERT OR IGNORE INTO field_options(field_type, field_code, field_name, color, sort_order) VALUES(?,?,?,?,?)",
                  ('brand', code, name, color, i))

    # 供应商
    suppliers = [
        ('JD', '京东', '#e1251b'),
        ('TMALL', '天猫', '#ff5000'),
        ('VIVO', 'vivo官方', '#415fff'),
        ('HUAWEI', '华为官方', '#cf0a2c'),
        ('DELL', '戴尔官方', '#008a05'),
        ('OTHER', '其他', '#8c8c8c'),
    ]
    for i, (code, name, color) in enumerate(suppliers):
        c.execute("INSERT OR IGNORE INTO field_options(field_type, field_code, field_name, color, sort_order) VALUES(?,?,?,?,?)",
                  ('supplier', code, name, color, i))

    # 单位
    units = [
        ('个', '个', '#1677ff'),
        ('台', '台', '#52c41a'),
        ('套', '套', '#722ed1'),
        ('件', '件', '#fa8c16'),
        ('箱', '箱', '#13c2c2'),
        ('包', '包', '#eb2f96'),
        ('张', '张', '#faad14'),
        ('卷', '卷', '#8c8c8c'),
    ]
    for i, (code, name, color) in enumerate(units):
        c.execute("INSERT OR IGNORE INTO field_options(field_type, field_code, field_name, color, sort_order) VALUES(?,?,?,?,?)",
                  ('unit', code, name, color, i))

    # 存放地
    locations = [
        ('SZ-OFFICE', '深圳办公室', '#1677ff'),
        ('BJ-OFFICE', '北京办公室', '#52c41a'),
        ('HK-OFFICE', '香港办公室', '#722ed1'),
        ('WAREHOUSE', '仓库', '#fa8c16'),
        ('OTHER', '其他', '#8c8c8c'),
    ]
    for i, (code, name, color) in enumerate(locations):
        c.execute("INSERT OR IGNORE INTO field_options(field_type, field_code, field_name, color, sort_order) VALUES(?,?,?,?,?)",
                  ('location', code, name, color, i))

    # 资产状态
    asset_statuses = [
        ('in_stock', '在库', '#52c41a'),
        ('in_use', '在用', '#1677ff'),
        ('repairing', '维修中', '#faad14'),
        ('scrapped', '已报废', '#8c8c8c'),
        ('transferred', '已出库', '#722ed1'),
    ]
    for i, (code, name, color) in enumerate(asset_statuses):
        c.execute("INSERT OR IGNORE INTO field_options(field_type, field_code, field_name, color, sort_order) VALUES(?,?,?,?,?)",
                  ('asset_status', code, name, color, i))


def _init_asset_type_params(c):
    """初始化资产类型参数（折旧、可发放年限等）"""
    # 默认资产类型参数：类型代码, 类型名称, 折旧月数, 残值率, 可发放年限
    params = [
        ('LT', '笔记本', 36, 0.05, 2),
        ('DP', '显示器', 60, 0.05, 4),
        ('PC', '台式电脑', 48, 0.05, 3),
        ('NB', '网络设备', 60, 0.05, 4),
        ('PR', '打印机', 48, 0.05, 3),
        ('PHONE', '手机', 24, 0.00, 1),
        ('PAD', '平板', 36, 0.05, 2),
        ('APP', '软件', 36, 0.00, 0),  # 软件残值0%，不限发放年限
        ('OTH', '其他', 36, 0.05, 2),
    ]
    for code, name, months, residual, issue_years in params:
        c.execute("""INSERT OR IGNORE INTO asset_type_params 
                     (type_code, type_name, depreciation_months, residual_rate, max_issue_years) 
                     VALUES(?,?,?,?,?)""", 
                  (code, name, months, residual, issue_years))


def _init_asset_config(c):
    """初始化默认资产配置数据"""
    # 默认主体
    subjects = [
        ('XJBH', '深圳京北汇', '深圳'),
        ('NHWS', '香港华科服务', '香港'),
        ('NHIB', '香港华科国际', '香港'),
        ('NHWHK', '香港华科', '香港'),
    ]
    for code, name, region in subjects:
        c.execute("INSERT OR IGNORE INTO asset_subjects(code, name, region) VALUES(?,?,?)", (code, name, region))

    # 默认资产类型
    types = [
        ('LT', '笔记本'),
        ('DP', '显示器'),
        ('APP', '软件'),
        ('PC', '台式电脑'),
        ('NB', '网络设备'),
        ('PR', '打印机'),
        ('PHONE', '手机'),
        ('PAD', '平板'),
        ('OTH', '其他'),
    ]
    for code, name in types:
        c.execute("INSERT OR IGNORE INTO asset_types(code, name) VALUES(?,?)", (code, name))

    # 默认存放地
    regions = [
        ('SZ', '深圳'),
        ('BJ', '北京'),
        ('HK', '香港'),
        ('SH', '上海'),
    ]
    for code, name in regions:
        c.execute("INSERT OR IGNORE INTO asset_regions(code, name) VALUES(?,?)", (code, name))

    # ── 字段维护表（可自定义下拉选项）─────────────────────────────
    c.execute('''
    CREATE TABLE IF NOT EXISTS field_options (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        field_type  TEXT NOT NULL,           -- 字段类型: category/brand/model/unit/supplier/location/status等
        field_code  TEXT NOT NULL,           -- 选项代码/英文
        field_name  TEXT NOT NULL,           -- 选项名称/中文
        field_value TEXT,                    -- 扩展值(JSON)
        color       TEXT,                    -- 颜色值(如 #1677ff)
        sort_order  INTEGER DEFAULT 0,       -- 排序
        active      INTEGER DEFAULT 1,       -- 是否启用
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # 添加唯一索引
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_field_options_unique ON field_options(field_type, field_code)")
    except:
        pass

    # 初始化默认字段选项
    _init_field_options(c)

    print(f"[OK] 数据库初始化完成: {os.path.abspath(DB_PATH)}")


if __name__ == '__main__':
    init_db()
