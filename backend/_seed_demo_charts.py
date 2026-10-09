"""扩展演示数据：资产/订单/申请/人员/盘点/耗材，便于全流程点测。
编号前缀 DEMO-。可重复执行：先清 DEMO 相关再写入。
演示账号密码统一 DemoPass1（勿用于生产）。
"""
import os
import sys
import json
import sqlite3
from datetime import date, timedelta

BACKEND = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND)
from database import hash_password  # noqa: E402

DB = os.path.abspath(os.path.join(BACKEND, '..', 'data', 'assets.db'))
PWD = hash_password('DemoPass1')


def months_ago(today, n):
    y, m = today.year, today.month - n
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(today.day, 28))


ASSETS = [
    # no, category, code, status, location, assignee, price, months_ago, warranty_days
    ('DEMO-LT-01', '笔记本', 'LT', '在用', '深圳', '林晓', 9800, 14, 60),
    ('DEMO-LT-02', '笔记本', 'LT', '在用', '深圳', '林晓', 8600, 8, 200),
    ('DEMO-LT-03', '笔记本', 'LT', '在库', '北京', '', 7200, 4, 400),
    ('DEMO-LT-04', '笔记本', 'LT', '维修中', '上海', '周宁', 9100, 20, -5),
    ('DEMO-LT-05', '笔记本', 'LT', '在用', '香港', '陈可', 11200, 6, 15),
    ('DEMO-LT-06', '笔记本', 'LT', '在库', '深圳', '', 7800, 2, 365),
    ('DEMO-LT-07', '笔记本', 'LT', '在库', '深圳', '', 6900, 1, 365),
    ('DEMO-LT-08', '笔记本', 'LT', '在库', '广州', '', 6500, 100, 30),  # 闲置>90天
    ('DEMO-DP-01', '显示器', 'DP', '在用', '深圳', '林晓', 1800, 18, 90),
    ('DEMO-DP-02', '显示器', 'DP', '在用', '北京', '赵敏', 2200, 10, 120),
    ('DEMO-DP-03', '显示器', 'DP', '在库', '上海', '', 1600, 5, 300),
    ('DEMO-DP-04', '显示器', 'DP', '在库', '深圳', '', 1500, 95, 200),  # 闲置
    ('DEMO-PC-01', '台式电脑', 'PC', '在用', '深圳', '赵敏', 6500, 22, 45),
    ('DEMO-PC-02', '台式电脑', 'PC', '在库', '广州', '', 5400, 3, 500),
    ('DEMO-PH-01', '手机', 'PHONE', '在用', '深圳', '陈可', 4999, 7, 20),
    ('DEMO-PH-02', '手机', 'PHONE', '在用', '北京', '周宁', 3999, 11, -2),
    ('DEMO-PH-03', '手机', 'PHONE', '在用', '深圳', '演示员工甲', 4299, 5, 180),
    ('DEMO-PR-01', '打印机', 'PR', '在库', '上海', '', 2800, 16, 100),
    ('DEMO-NB-01', '网络设备', 'NB', '在用', '深圳', '赵敏', 3600, 24, 60),
    ('DEMO-PAD-01', '平板', 'PAD', '在库', '香港', '', 3200, 9, 250),
    ('DEMO-OTH-01', '其他', 'OTH', '已报废', '深圳', '', 800, 30, None),
]

EMPLOYEES = [
    # name, role, dept, email
    ('演示财务', '财务', '财务部', 'demo.finance@test.com'),
    ('演示操作员', '操作员', 'IT部', 'demo.ops@test.com'),
    ('演示行政', '普通管理员', '行政部', 'demo.admin@test.com'),
    ('演示员工甲', '普通用户', '产品部', 'demo.user1@test.com'),
    ('演示员工乙', '普通用户', '市场部', 'demo.user2@test.com'),
    ('林晓', '普通用户', '研发部', 'linxiao@test.com'),
    ('周宁', '普通用户', '研发部', 'zhouning@test.com'),
    ('陈可', '普通用户', '设计部', 'chenke@test.com'),
    ('赵敏', '普通用户', '运维部', 'zhaomin@test.com'),
]


def clear_demo(conn):
    old = conn.execute("SELECT id FROM assets WHERE asset_no LIKE 'DEMO-%'").fetchall()
    ids = [r[0] for r in old]
    if ids:
        q = ','.join('?' * len(ids))
        conn.execute(f'DELETE FROM stock_logs WHERE asset_id IN ({q})', ids)
        conn.execute(f'DELETE FROM repair_logs WHERE asset_id IN ({q})', ids)
        try:
            conn.execute(
                f'DELETE FROM inventory_items WHERE asset_id IN ({q})', ids)
        except sqlite3.OperationalError:
            pass
        conn.execute(f'DELETE FROM assets WHERE id IN ({q})', ids)

    conn.execute("DELETE FROM purchase_orders WHERE order_no LIKE 'DEMO-PO-%'")
    conn.execute("DELETE FROM order_items WHERE order_id NOT IN (SELECT id FROM purchase_orders)")
    conn.execute(
        "DELETE FROM asset_applications WHERE app_no LIKE 'DEMO-APP-%' OR reason LIKE '演示%'")
    conn.execute("DELETE FROM consumables WHERE item_no LIKE 'DEMO-C-%'")
    try:
        conn.execute("DELETE FROM consumable_logs WHERE item_no LIKE 'DEMO-C-%'")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "DELETE FROM inventory_tasks WHERE task_no LIKE 'DEMO-INV-%'")
    # 演示员工（保留超管）
    names = [e[0] for e in EMPLOYEES]
    conn.execute(
        f"DELETE FROM employees WHERE name IN ({','.join('?'*len(names))})", names)


def seed_employees(conn):
    for name, role, dept, email in EMPLOYEES:
        conn.execute(
            """INSERT INTO employees
               (name, role, dept, email, password, lark_only, status, created_at)
               VALUES (?,?,?,?,?,0,'在职', datetime('now','localtime'))""",
            (name, role, dept, email, PWD),
        )


def seed_assets(conn, today):
    id_by_no = {}
    for no, cat, code, status, loc, assignee, price, months, wdays in ASSETS:
        pdate = months_ago(today, months).isoformat()
        wexp = None
        if wdays is not None:
            wexp = (today + timedelta(days=wdays)).isoformat()
        cur = conn.execute(
            """INSERT INTO assets
               (asset_no, category, brand, model, status, location, assignee,
                purchase_date, purchase_price, asset_type_code, warranty_expire, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (no, cat, '演示', 'Demo', status, loc, assignee,
             pdate, price, code, wexp, '演示数据'),
        )
        id_by_no[no] = cur.lastrowid
    return id_by_no


def seed_stock_logs(conn, today, id_by_no):
    counts = [2, 1, 3, 2, 4, 1, 3, 2]
    asset_ids = list(id_by_no.values())
    k = 0
    for i, n in enumerate(counts):
        month = months_ago(today, 7 - i)
        when = month.replace(day=min(12, 28)).isoformat() + ' 10:00:00'
        for _ in range(n):
            aid = asset_ids[k % len(asset_ids)]
            ano = next(no for no, vid in id_by_no.items() if vid == aid)
            conn.execute(
                """INSERT INTO stock_logs
                   (asset_id, asset_no, log_type, operator, remark, log_time)
                   VALUES (?,?,?,?,?,?)""",
                (aid, ano, '入库', '演示', '演示入库', when),
            )
            k += 1
    # 几笔出库动态
    for no, user in [('DEMO-LT-01', '林晓'), ('DEMO-PH-03', '演示员工甲')]:
        conn.execute(
            """INSERT INTO stock_logs
               (asset_id, asset_no, log_type, operator, target_user, remark, log_time)
               VALUES (?,?,?,?,?,?,datetime('now','localtime'))""",
            (id_by_no[no], no, '出库', '演示', user, '演示出库'),
        )


def seed_repairs(conn, id_by_no):
    repairs = [
        ('DEMO-LT-04', '待处理', '键盘失灵'),
        ('DEMO-LT-01', '维修中', '屏幕闪烁'),
        ('DEMO-DP-01', '已完成', '支架松动'),
        ('DEMO-PC-01', '已完成', '风扇异响'),
        ('DEMO-PH-02', '无法修复', '进水'),
    ]
    for no, st, desc in repairs:
        conn.execute(
            """INSERT INTO repair_logs
               (asset_id, asset_no, fault_desc, submit_by, repair_status)
               VALUES (?,?,?,?,?)""",
            (id_by_no[no], no, desc, '演示', st),
        )


def seed_orders(conn):
    orders = [
        ('DEMO-PO-01', '演示-待到货笔记本', 18600, '待到货', ''),
        ('DEMO-PO-02', '演示-部分到货显示器', 4400, '部分到货', ''),
        ('DEMO-PO-03', '演示-已到货有票', 4999, '已到货', 'INV-DEMO-1'),
        ('DEMO-PO-04', '演示-待到货平板', 6400, '待到货', ''),
    ]
    po_ids = {}
    for no, title, amount, status, invoice in orders:
        cur = conn.execute(
            """INSERT INTO purchase_orders
               (order_no, title, supplier, total_amount, status, invoice_no, created_by)
               VALUES (?,?,?,?,?,?,?)""",
            (no, title, '演示供应商', amount, status, invoice, '演示'),
        )
        po_ids[no] = cur.lastrowid
        conn.execute(
            """INSERT INTO order_items
               (order_id, item_type, item_name, category, quantity, unit_price)
               VALUES (?,?,?,?,?,?)""",
            (cur.lastrowid, 'asset', title, '笔记本', 1, amount),
        )
    return po_ids


def seed_applications(conn, today, id_by_no, po_ids):
    def emp(name):
        return conn.execute(
            "SELECT id, name FROM employees WHERE name=?", (name,)
        ).fetchone()

    u1 = emp('演示员工甲')
    u2 = emp('演示员工乙')
    lx = emp('林晓')

    apps = [
        # app_no, type, user, title, reason, status, fulfilled, po_key, detail
        ('DEMO-APP-01', 'new_asset', u1, '新资产-演示员工甲笔记本', '演示待审批新资产',
         'pending', 0, None, {'category': '笔记本', 'type': 'existing'}),
        ('DEMO-APP-02', 'new_asset', u2, '新资产-演示员工乙显示器', '演示待发放无订单',
         'approved', 0, None, {'category': '显示器', 'type': 'existing'}),
        ('DEMO-APP-03', 'new_asset', u1, '新资产-已关联订单', '演示待发放已有订单',
         'approved', 0, 'DEMO-PO-04', {'category': '平板', 'type': 'existing'}),
        ('DEMO-APP-04', 'return', lx, '归还-林晓笔记本', '演示归还待审批',
         'pending', 0, None, {'asset_id': id_by_no['DEMO-LT-02']}),
        ('DEMO-APP-05', 'repair', emp('周宁'), '报修-周宁手机', '演示报修待审批',
         'pending', 0, None, {'asset_id': id_by_no['DEMO-PH-02']}),
        ('DEMO-APP-06', 'borrow', u2, '借用-在库笔记本', '演示借用待审批',
         'pending', 0, None, {'asset_id': id_by_no['DEMO-LT-06']}),
    ]
    for app_no, app_type, user, title, reason, status, fulfilled, po_key, detail in apps:
        if not user:
            continue
        po_id = po_ids.get(po_key) if po_key else None
        asset_id = detail.get('asset_id')
        asset_no = ''
        if asset_id:
            row = conn.execute(
                "SELECT asset_no FROM assets WHERE id=?", (asset_id,)
            ).fetchone()
            asset_no = row[0] if row else ''
        review_time = None
        if status == 'approved':
            review_time = today.isoformat() + ' 09:00:00'
        conn.execute(
            """INSERT INTO asset_applications
               (app_no, app_type, applicant_id, applicant_name, asset_id, asset_no,
                title, reason, detail, status, review_time, fulfilled, po_id, apply_time)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
            (app_no, app_type, user[0], user[1], asset_id, asset_no,
             title, reason, json.dumps(detail, ensure_ascii=False),
             status, review_time, fulfilled, po_id),
        )


def seed_consumables(conn):
    items = [
        ('DEMO-C-01', '签字笔', '办公文具', 120, 50, '正常'),
        ('DEMO-C-02', 'A4纸', '办公文具', 8, 20, '低库存'),
        ('DEMO-C-03', '网线', 'IT耗材', 3, 10, '低库存'),
        ('DEMO-C-04', '鼠标垫', 'IT耗材', 40, 10, '正常'),
    ]
    for no, name, cat, qty, min_s, st in items:
        try:
            conn.execute(
                """INSERT INTO consumables
                   (item_no, name, category, quantity, min_stock, unit, status, notes)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (no, name, cat, qty, min_s, '个', st, '演示耗材'),
            )
        except sqlite3.OperationalError:
            # 旧表字段可能不同，尽量写入
            conn.execute(
                """INSERT INTO consumables (item_no, name, category, quantity, unit, notes)
                   VALUES (?,?,?,?,?,?)""",
                (no, name, cat, qty, '个', '演示耗材'),
            )


def seed_inventory(conn, today, id_by_no):
    task_no = 'DEMO-INV-01'
    cur = conn.execute(
        """INSERT INTO inventory_tasks
           (task_no, task_name, scope_type, status, created_by, started_at)
           VALUES (?,?,?,?,?,datetime('now','localtime'))""",
        (task_no, '演示盘点-在库资产', 'in_stock', 'ongoing', '演示行政'),
    )
    task_id = cur.lastrowid
    stock_nos = [row[0] for row in ASSETS if row[3] == '在库']
    for no in stock_nos[:6]:
        aid = id_by_no[no]
        a = conn.execute(
            "SELECT asset_no, category, location, status, assignee FROM assets WHERE id=?",
            (aid,),
        ).fetchone()
        conn.execute(
            """INSERT INTO inventory_items
               (task_id, asset_id, asset_no, before_status, before_location, before_assignee, check_status)
               VALUES (?,?,?,?,?,?,?)""",
            (task_id, aid, a['asset_no'], a['status'], a['location'], a['assignee'] or '', 'pending'),
        )


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    today = date.today()
    clear_demo(conn)
    seed_employees(conn)
    id_by_no = seed_assets(conn, today)
    seed_stock_logs(conn, today, id_by_no)
    seed_repairs(conn, id_by_no)
    po_ids = seed_orders(conn)
    seed_applications(conn, today, id_by_no, po_ids)
    seed_consumables(conn)
    seed_inventory(conn, today, id_by_no)
    conn.commit()
    print('OK demo data in', DB)
    print('accounts (password DemoPass1):')
    for name, role, *_ in EMPLOYEES:
        print(f'  - {name} / {role}')
    print('tips: 待审批 DEMO-APP-01/04/05/06；待发放 DEMO-APP-02(可生成订单)、DEMO-APP-03(已有订单)；')
    print('      在库可发放 DEMO-LT-06/07；闲置 DEMO-LT-08/DP-04；进行中盘点 DEMO-INV-01')
    conn.close()


if __name__ == '__main__':
    main()
