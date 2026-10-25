"""查 4 个 enabled 邮箱的 grok trace（冷却时间）——确认"账号池为空"根因"""
import json
import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect(r"E:\AI\openAI\注册机1\any-auto-register-main\account_manager.db")
rows = conn.execute(
    "SELECT email, platform_trace_json FROM outlook_accounts WHERE enabled=1"
).fetchall()
now = datetime.now(timezone.utc)
for email, trace_json in rows:
    d = json.loads(trace_json or "{}")
    g = d.get("grok") or {}
    lfa = g.get("last_fail_at") or ""
    cooled = ""
    if lfa:
        try:
            dt = datetime.fromisoformat(lfa)
            hours = (now - dt).total_seconds() / 3600
            cooled = f"{hours:.1f}h 前（{'冷却中<24h' if hours < 24 else '已过冷却'})"
        except Exception as e:
            cooled = f"解析失败:{e}"
    print(f"== {email}")
    print(f"   grok: fails={g.get('fails')} excluded={g.get('excluded')} success={g.get('success')} last_fail_at={lfa} {cooled}")
    print(f"   平台: {list(d.keys())}")
conn.close()
