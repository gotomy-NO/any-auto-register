"""查询注册机 zcode 账号 + A2 zcode 凭据目录状态（只读）"""
import json
import os
import sqlite3
from pathlib import Path

DB = r"E:\AI\openAI\注册机1\any-auto-register-main\account_manager.db"
A2_ZCODE = Path(r"E:\AI\openAI\AIClient2API-main\configs\zcode")

con = sqlite3.connect(DB)
cur = con.cursor()
cur.execute(
    "SELECT email, platform, status, created_at FROM accounts WHERE platform='zcode' ORDER BY created_at DESC LIMIT 8"
)
rows = cur.fetchall()
print("=== 注册机 accounts 表 zcode 账号 ===")
for r in rows:
    print(r)
try:
    cur.execute("SELECT email, enabled, platform_trace_json FROM outlook_accounts ORDER BY id DESC LIMIT 5")
    print("=== 最近邮箱（含平台痕迹） ===")
    for r in cur.fetchall():
        email, enabled, trace = r
        t = ""
        try:
            t = json.loads(trace or "{}").get("zcode", {})
        except Exception:
            pass
        print(email, "enabled=", enabled, "zcode_trace=", t)
except Exception as e:
    print("outlook_accounts 查询失败:", e)
con.close()

print("=== A2 configs/zcode 目录 ===")
for d in sorted(A2_ZCODE.iterdir()):
    if d.is_dir():
        for f in d.iterdir():
            print(d.name, "/", f.name, os.path.getsize(f))
    else:
        print(d.name, os.path.getsize(d))
