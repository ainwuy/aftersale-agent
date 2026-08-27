"""订单 + 物流系统（SQLite 模拟，aftersale-cs 的"能办事"业务后端）

职责（对应业务需求分析报告 §4.5 业务系统接入）：
- orders 表：订单号、用户名、商品、状态、金额、物流单号、创建时间
- shipments 表：物流单号、订单号、状态、当前位置、轨迹、预计到达
- API：get_order / list_user_orders / track_shipment / update_order_status / create_return_order / apply_refund
- 种子数据：启动时自动建表 + 插入 5 个测试订单（含 3 种状态流转）

为什么用 SQLite 而不是真 ERP：
- demo 自包含、零依赖、可重复跑
- 接口签名与真 ERP 对齐，生产时只需把 _conn 换成 HTTP 客户端
"""
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

DB_PATH = os.environ.get("AFTERSALE_ORDER_DB", os.path.join(os.path.dirname(__file__), "orders.db"))

# 订单状态枚举（业务流转链）
ORDER_STATUS = ["pending", "paid", "shipped", "delivered", "return_requested", "returned", "refunded"]
SHIPMENT_STATUS = ["created", "picked_up", "in_transit", "out_for_delivery", "delivered", "returned"]


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _init_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL,
            username TEXT NOT NULL,
            product TEXT NOT NULL,
            quantity INTEGER DEFAULT 1,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            tracking_no TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking_no TEXT UNIQUE NOT NULL,
            order_no TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            current_location TEXT,
            estimated_arrival TEXT,
            timeline TEXT,
            FOREIGN KEY (order_no) REFERENCES orders(order_no)
        );
        CREATE TABLE IF NOT EXISTS refunds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            refund_no TEXT UNIQUE NOT NULL,
            order_no TEXT NOT NULL,
            username TEXT NOT NULL,
            amount REAL NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY (order_no) REFERENCES orders(order_no)
        );
        """)
        # 种子数据：5 个订单覆盖主流状态
        if c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now()
            seed = [
                ("CS20260820001", "alice", "运动鞋 红色 38 码", 1, 299.0, "delivered",
                 "SF1029384756", now - timedelta(days=5)),
                ("CS20260820002", "alice", "羽绒服 黑色 M", 1, 599.0, "shipped",
                 "YT8374629102", now - timedelta(days=1)),
                ("CS20260820003", "bob", "耳机 蓝牙 Pro", 1, 199.0, "paid",
                 None, now - timedelta(hours=3)),
                ("CS20260820004", "charlie", "书包 帆布 大号", 2, 158.0, "pending",
                 None, now - timedelta(hours=1)),
                ("CS20260820005", "alice", "运动鞋 红色 38 码", 1, 299.0, "return_requested",
                 "SF9988776655", now - timedelta(days=4)),
            ]
            for order_no, user, prod, qty, amt, status, track, ts in seed:
                c.execute(
                    "INSERT INTO orders (order_no, username, product, quantity, amount, status, tracking_no, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (order_no, user, prod, qty, amt, status, track, ts.isoformat(), ts.isoformat())
                )
                if track:
                    eta = ts + timedelta(days=3)
                    c.execute(
                        "INSERT INTO shipments (tracking_no, order_no, status, current_location, estimated_arrival, timeline) "
                        "VALUES (?,?,?,?,?,?)",
                        (track, order_no,
                         "delivered" if status == "delivered" else "in_transit",
                         "北京分拨中心" if status != "delivered" else "已签收",
                         eta.isoformat(),
                         f"{(ts + timedelta(hours=2)).isoformat()} 已揽收\n{(ts + timedelta(hours=8)).isoformat()} 离开杭州\n{(ts + timedelta(days=1)).isoformat()} 到达北京分拨中心")
                    )


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()} if row else {}


def get_order(order_no: str) -> Optional[dict]:
    """查询单个订单详情（含物流轨迹）。"""
    with _conn() as c:
        order = c.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
        if not order:
            return None
        result = _row_to_dict(order)
        if order["tracking_no"]:
            ship = c.execute("SELECT * FROM shipments WHERE tracking_no = ?",
                             (order["tracking_no"],)).fetchone()
            result["shipment"] = _row_to_dict(ship) if ship else None
        return result


def list_user_orders(username: str) -> list[dict]:
    """列出某用户的全部订单。"""
    with _conn() as c:
        rows = c.execute("SELECT * FROM orders WHERE username = ? ORDER BY created_at DESC",
                         (username,)).fetchall()
        return [_row_to_dict(r) for r in rows]


def track_shipment(tracking_no: str) -> Optional[dict]:
    """查物流轨迹。"""
    with _conn() as c:
        row = c.execute("SELECT * FROM shipments WHERE tracking_no = ?",
                        (tracking_no,)).fetchone()
        return _row_to_dict(row) if row else None


def update_order_status(order_no: str, new_status: str, tracking_no: str = None) -> dict:
    """更新订单状态（如：已支付→已发货，需附物流单号）。"""
    if new_status not in ORDER_STATUS:
        return {"ok": False, "error": f"非法状态: {new_status}，合法: {ORDER_STATUS}"}
    with _conn() as c:
        order = c.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
        if not order:
            return {"ok": False, "error": f"订单 {order_no} 不存在"}
        if tracking_no:
            c.execute("UPDATE orders SET status=?, tracking_no=?, updated_at=? WHERE order_no=?",
                      (new_status, tracking_no, datetime.now().isoformat(), order_no))
            c.execute("INSERT OR REPLACE INTO shipments (tracking_no, order_no, status, current_location, estimated_arrival, timeline) "
                      "VALUES (?,?,?,?,?,?)",
                      (tracking_no, order_no, "picked_up", "仓库已出库",
                       (datetime.now() + timedelta(days=2)).isoformat(),
                       f"{datetime.now().isoformat()} 已出库"))
        else:
            c.execute("UPDATE orders SET status=?, updated_at=? WHERE order_no=?",
                      (new_status, datetime.now().isoformat(), order_no))
        return {"ok": True, "order_no": order_no, "new_status": new_status}


def create_return_order(order_no: str, reason: str) -> dict:
    """申请退货（订单状态 → return_requested）。"""
    with _conn() as c:
        order = c.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
        if not order:
            return {"ok": False, "error": f"订单 {order_no} 不存在"}
        if order["status"] not in ("delivered", "shipped"):
            return {"ok": False, "error": f"订单状态 {order['status']} 不支持退货（需已发货或已签收）"}
        c.execute("UPDATE orders SET status='return_requested', updated_at=? WHERE order_no=?",
                  (datetime.now().isoformat(), order_no))
        return {"ok": True, "order_no": order_no, "new_status": "return_requested", "reason": reason}


def apply_refund(order_no: str, amount: float, reason: str) -> dict:
    """申请退款（创建退款单，订单状态 → refunded）。"""
    with _conn() as c:
        order = c.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
        if not order:
            return {"ok": False, "error": f"订单 {order_no} 不存在"}
        if amount > order["amount"]:
            return {"ok": False, "error": f"退款金额 {amount} 超过订单金额 {order['amount']}"}
        refund_no = f"RF{int(time.time())}"
        c.execute("INSERT INTO refunds (refund_no, order_no, username, amount, reason, status, created_at) VALUES (?,?,?,?,?,?,?)",
                  (refund_no, order_no, order["username"], amount, reason, "approved", datetime.now().isoformat()))
        c.execute("UPDATE orders SET status='refunded', updated_at=? WHERE order_no=?",
                  (datetime.now().isoformat(), order_no))
        return {"ok": True, "refund_no": refund_no, "order_no": order_no, "amount": amount, "status": "approved"}


# 模块加载时自动建表 + 种子
_init_db()
if __name__ == "__main__":
    # 自检
    print(f"[order_system] DB: {DB_PATH}")
    print("get_order CS20260820001:", get_order("CS20260820001"))
    print("list_user_orders alice:", len(list_user_orders("alice")), "个订单")
    print("track_shipment SF1029384756:", track_shipment("SF1029384756"))