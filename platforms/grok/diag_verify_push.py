"""任务 #3 核查：grok 注册→入库→补传链路现状"""
import sqlite3

DB = r"E:\AI\openAI\注册机1\any-auto-register-main\account_manager.db"

print("1) 数据库 grok 账号:")
conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("SELECT id, platform, email, status, substr(token,1,25) FROM accounts WHERE platform='grok' ORDER BY id DESC LIMIT 10")
rows = cur.fetchall()
if not rows:
    print("  （无 grok 账号记录）")
for r in rows:
    print(f"  id={r[0]} email={r[2]} status={r[3]} token={r[4] or ''}...")
conn.close()

print()
print("2) A2 configs/grok 凭据目录:")
import os, json
base = r"E:\AI\openAI\AIClient2API-main\configs\grok"
if os.path.isdir(base):
    for d in sorted(os.listdir(base)):
        p = os.path.join(base, d)
        if os.path.isdir(p):
            print(f"  {d}/")
        else:
            print(f"  {d} ({os.path.getsize(p)}B)")
else:
    print("  （目录不存在）")

print()
print("3) A2 provider_pools.json 是否含 grok-web 池:")
pool_path = r"E:\AI\openAI\AIClient2API-main\configs\provider_pools.json"
pool = json.load(open(pool_path, encoding="utf-8"))
if "grok-web" in pool:
    gw = pool["grok-web"]
    print(f"  grok-web 池条目数: {len(gw)}")
    for e in gw:
        print(f"   - {e.get('customName')} isDisabled={e.get('isDisabled')} token={e.get('GROK_COOKIE_TOKEN')}")
else:
    print("  （provider_pools.json 无 grok-web 池）")
    print("  现有池:", list(pool.keys()))
