"""RedisSaver 连通性与 RediSearch 模块探针（用户在自己机器上跑）。
运行：python test_redis_saver.py
不带参数 → 测本地 127.0.0.1:6379
带参数 → python test_redis_saver.py 192.168.2.221 6379 0 [password]

判断 3 件事：
1. Redis 是否能连
2. RediSearch 模块是否加载（LangGraph RedisSaver 必需）
3. JSON 模块是否加载（state 持久化必需）
"""
import sys

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 6379
db = int(sys.argv[3]) if len(sys.argv) > 3 else 0
password = sys.argv[4] if len(sys.argv) > 4 else None

print(f"[probe] 连 {host}:{port}/{db}  password={'有' if password else '无'}")

try:
    import redis
    client = redis.Redis(host=host, port=port, db=db, password=password,
                         socket_connect_timeout=3, socket_timeout=3)
    client.ping()
    print(f"[probe] ✅ Redis 连通 OK，版本: {client.info('server').get('redis_version')}")

    modules = {m.get("name"): m.get("ver", "?") for m in client.module_list()}
    print(f"[probe] 已加载模块: {list(modules.keys()) or '(无)'}")
    if "search" in modules or "RediSearch" in modules:
        print(f"[probe] ✅ RediSearch 模块可用 (v{modules.get('search') or modules.get('RediSearch')})")
    else:
        print(f"[probe] ❌ RediSearch 未加载！LangGraph RedisSaver 需要 Redis Stack")
        print(f"        安装：docker run -d -p 6379:6379 redis/redis-stack-server:latest")
        sys.exit(2)

    if "ReJSON" in modules or "json" in modules:
        print(f"[probe] ✅ JSON 模块可用")
    else:
        print(f"[probe] ⚠️ JSON 模块未加载，state 持久化可能失败")

    print("\n[probe] 全部就绪，可以启动 aftersale-cs 用 RedisSaver")
    print("配置（写进 .env）：")
    print(f"  AFTERSALE_CHECKPOINTER=redis")
    print(f"  AFTERSALE_REDIS_HOST={host}")
    print(f"  AFTERSALE_REDIS_PORT={port}")
    print(f"  AFTERSALE_REDIS_DB={db}")
    if password:
        print(f"  AFTERSALE_REDIS_PASSWORD={password}")

except ImportError:
    print("[probe] ❌ redis-py 未装：pip install redis")
    sys.exit(1)
except redis.exceptions.ConnectionError as e:
    print(f"[probe] ❌ 连不上 Redis：{e}")
    print("       先确认 Redis 是否在跑（systemctl status redis 或 docker ps）")
    sys.exit(1)
except Exception as e:
    print(f"[probe] ❌ 异常：{type(e).__name__}: {e}")
    sys.exit(1)