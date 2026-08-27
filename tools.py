"""业务工具集：把 order_system 的能力包装成 LangGraph @tool，让 order_agent 能调用。

包含工具（@tool 装饰，自动从签名+docstring 生成 JSON Schema）：
- lookup_order(order_no)         : 查订单详情
- list_user_orders(username)      : 列出某用户的全部订单
- track_logistics(tracking_no)   : 查物流轨迹
- request_return(order_no, reason): 申请退货
- request_refund(order_no, amount, reason): 申请退款
"""
from langchain_core.tools import tool
import order_system as ods


@tool
def lookup_order(order_no: str) -> str:
    """查询订单详情（含商品、金额、状态、物流单号）。
    order_no: 订单号，如 CS20260820001
    返回订单的 JSON 字符串；订单不存在时明确说明。
    """
    import json
    o = ods.get_order(order_no)
    if not o:
        return f"订单 {order_no} 不存在，请确认订单号是否正确"
    return json.dumps(o, ensure_ascii=False, indent=2)


@tool
def list_user_orders(username: str) -> str:
    """列出某用户的全部订单。
    username: 用户名，如 alice / bob
    返回订单列表的 JSON 字符串；为空时说明该用户无订单。
    """
    import json
    orders = ods.list_user_orders(username)
    if not orders:
        return f"用户 {username} 暂无任何订单"
    return json.dumps(orders, ensure_ascii=False, indent=2)


@tool
def track_logistics(tracking_no: str) -> str:
    """查询物流轨迹与当前位置。
    tracking_no: 物流单号，如 SF1029384756
    返回物流详情 JSON；单号不存在时明确说明。
    """
    import json
    s = ods.track_shipment(tracking_no)
    if not s:
        return f"物流单号 {tracking_no} 不存在"
    return json.dumps(s, ensure_ascii=False, indent=2)


@tool
def request_return(order_no: str, reason: str) -> str:
    """为用户申请退货（订单状态会变为 return_requested）。
    order_no: 要退货的订单号
    reason: 退货原因（如尺码不合适 / 商品不喜欢 / 质量问题）
    返回处理结果 JSON：成功或失败原因。
    """
    import json
    return json.dumps(ods.create_return_order(order_no, reason), ensure_ascii=False, indent=2)


@tool
def request_refund(order_no: str, amount: float, reason: str) -> str:
    """为用户申请退款（订单状态会变为 refunded，创建退款单）。
    order_no: 要退款的订单号
    amount: 退款金额（不能超过订单原金额）
    reason: 退款原因
    返回处理结果 JSON，含退款单号 refund_no。
    """
    import json
    return json.dumps(ods.apply_refund(order_no, amount, reason), ensure_ascii=False, indent=2)


# 给 cs_supervisor.py 的 retrieve 节点用的工具集合
ORDER_TOOLS = [lookup_order, list_user_orders, track_logistics, request_return, request_refund]