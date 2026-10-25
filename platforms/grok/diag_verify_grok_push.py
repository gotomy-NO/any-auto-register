"""任务 #12/#4 核查：grok 注册成功 → 入库 → 自动推送 A2 configs/grok"""
import json
import os
import sqlite3

DB = r"E:\AI\openAI\注册机1\any-auto-register-main\account_manager.db"
A2_GROK = r"E:\AI\openAI\AIClient2API-main\configs\grok"

print("1) 数据库 grok 账号:")
conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("SELECT id, platform, email, status, substr(extra_json,1,200) FROM accounts WHERE platform='grok' ORDER BY id DESC LIMIT 5")
rows = cur.fetchall()
for r in rows:
    print(f"  id={r[0]} email={r[2]} status={r[3]} extra={r[4][:160]}")
conn.close()

print()
print("2) A2 configs/grok 凭据目录:")
if os.path.isdir(A2_GROK):
    for d in sorted(os.listdir(A2_GROK)):
        p = os.path.join(A2_GROK, d)
        if os.path.isdir(p):
            print(f"  {d}/")
            for f in os.listdir(p):
                fp = os.path.join(p, f)
                print(f"    {f} ({os.path.getsize(fp)}B)")
        else:
            print(f"  {d} ({os.path.getsize(p)}B)")
else:
    print("  （目录不存在）")

print()
print("3) A2 provider_pools.json grok-web 池:")
pool_path = r"E:\AI\openAI\AIClient2API-main\configs\provider_pools.json"
pool = json.load(open(pool_path, encoding="utf-8"))
print("  现有池:", list(pool.keys()))
if "grok-web" in pool:
    for e in pool["grok-web"]:
        print(f"   - {e.get('customName')} isDisabled={e.get('isDisabled')}")
