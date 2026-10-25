"""排查：A2 configs/zcode 新凭据 + 注册机数据库 zcode 账号对应关系"""
import json
import os
import sqlite3

A2_ZCODE = r"E:\AI\openAI\AIClient2API-main\configs\zcode"
DB = r"E:\AI\openAI\注册机1\any-auto-register-main\account_manager.db"
KNOWN_OLD = {"1787977538174_zcode-auth", "1787986739492_zcode-auth"}

print("=" * 60)
print("A2 configs/zcode 凭据目录:")
for d in sorted(os.listdir(A2_ZCODE)):
    if not d.endswith("_zcode-auth"):
        continue
    jf = os.path.join(A2_ZCODE, d, d + ".json")
    try:
        j = json.load(open(jf, encoding="utf-8"))
        tag = "（旧）" if d in KNOWN_OLD else "（新）"
        print(f"  {d} {tag} email={j.get('email')} username={j.get('username')} token_len={len(j.get('token') or '')}")
    except Exception as e:
        print(f"  {d} ERR {e}")

print("=" * 60)
print("注册机数据库 zcode 账号:")
conn = sqlite3.connect(DB)
cur = conn.cursor()
try:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    print("tables:", tables)
except Exception as e:
    print("list tables ERR:", e)
for t in ("accounts", "account", "zcode_accounts"):
    if t not in tables:
        continue
    try:
        cur.execute(f"SELECT * FROM {t} WHERE platform='zcode' ORDER BY id DESC LIMIT 10")
        cols = [c[0] for c in cur.description]
        print(f"--- {t} 列: {cols}")
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            extra = str(d.get("extra") or "")[:120]
            print(f"  id={d.get('id')} email={d.get('email')} status={d.get('status')} extra={extra}")
    except Exception as e:
        print(f"{t} query ERR: {e}")
conn.close()
