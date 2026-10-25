"""查 outlook 邮箱池：用户导入的 3 个新 outlook 邮箱"""
import sqlite3

DB = r"E:\AI\openAI\注册机1\any-auto-register-main\account_manager.db"
conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("SELECT * FROM outlook_accounts ORDER BY id DESC LIMIT 15")
cols = [c[0] for c in cur.description]
print("列:", cols)
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print(" ", {k: (str(v)[:40] if v is not None else None) for k, v in d.items()})
conn.close()
