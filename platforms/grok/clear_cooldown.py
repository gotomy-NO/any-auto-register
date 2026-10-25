"""清除 4 个 enabled 邮箱的 grok 误伤冷却（解除 last_fail_at，保留 fails 历史计数）"""
import json
import sqlite3

conn = sqlite3.connect(r"E:\AI\openAI\注册机1\any-auto-register-main\account_manager.db")
rows = conn.execute(
    "SELECT id, platform_trace_json FROM outlook_accounts WHERE enabled=1"
).fetchall()
cleared = 0
for account_id, trace_json in rows:
    d = json.loads(trace_json or "{}")
    g = d.get("grok")
    if isinstance(g, dict) and g.get("last_fail_at"):
        g.pop("last_fail_at", None)  # 解除 24h 冷却（保留 fails 历史）
        conn.execute(
            "UPDATE outlook_accounts SET platform_trace_json=? WHERE id=?",
            (json.dumps(d, ensure_ascii=False), account_id),
        )
        cleared += 1
conn.commit()
print(f"已清除 {cleared} 个邮箱的 grok 冷却")
# 验证
rows = conn.execute(
    "SELECT email, platform_trace_json FROM outlook_accounts WHERE enabled=1"
).fetchall()
for email, tj in rows:
    g = (json.loads(tj or "{}").get("grok") or {})
    print(f"  {email}: grok fails={g.get('fails')} last_fail_at={g.get('last_fail_at')}")
conn.close()
