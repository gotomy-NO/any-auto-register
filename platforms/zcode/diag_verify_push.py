"""任务 #3 核查：注册成功账号是否入库 + 推送 A2 configs/zcode + provider_pools 关联"""
import json
import os
import sqlite3

DB = r"E:\AI\openAI\注册机1\any-auto-register-main\account_manager.db"
A2_ZCODE = r"E:\AI\openAI\AIClient2API-main\configs\zcode"
POOL = r"E:\AI\openAI\AIClient2API-main\configs\provider_pools.json"

print("=" * 60)
print("1) 数据库 accounts 表最新 zcode 账号:")
conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("SELECT id, platform, email, status, token FROM accounts WHERE platform='zcode' ORDER BY id DESC LIMIT 5")
for row in cur.fetchall():
    tok = (row[4] or "")[:20]
    print(f"  id={row[0]} email={row[1] and row[2]} status={row[3]} token={tok}...")
conn.close()

print("=" * 60)
print("2) A2 configs/zcode 凭据目录（按时间排序）:")
dirs = sorted([d for d in os.listdir(A2_ZCODE) if d.endswith("_zcode-auth")])
for d in dirs:
    jf = os.path.join(A2_ZCODE, d, d + ".json")
    try:
        j = json.load(open(jf, encoding="utf-8"))
        print(f"  {d} email={j.get('email')} username={j.get('username')} token_len={len(j.get('token') or '')}")
    except Exception as e:
        print(f"  {d} ERR {e}")

print("=" * 60)
print("3) provider_pools.json zcode-web 池条目:")
pool = json.load(open(POOL, encoding="utf-8"))
for e in pool.get("zcode-web", []):
    print(f"  {e.get('customName')} | isDisabled={e.get('isDisabled')} | {e.get('ZCODE_COOKIE_TOKEN')}")
