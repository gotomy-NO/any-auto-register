"""查注册机后端日志，确认 grok 会话代理调用时的实际行为（消息是否发出/回复捕获失败原因）"""
import glob
import os

# 找日志文件
for pattern in ("logs/*.log", "*.log"):
    files = sorted(glob.glob(os.path.join(r"E:\AI\openAI\注册机1\any-auto-register-main", pattern)), key=os.path.getmtime, reverse=True)
    if files:
        break

if not files:
    print("未找到日志文件")
    raise SystemExit

latest = files[0]
print("日志:", latest, os.path.getsize(latest), "B")
with open(latest, "r", encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
print("总行数:", len(lines))
print("--- 含 GrokProxy / 发送 / chat / 未捕获 的行（最近 60 条）---")
hits = 0
for line in lines[-300:]:
    if any(k in line for k in ("GrokProxy", "grok chat", "未捕获", "已发送", "年龄", "会话")):
        print(line.rstrip()[:200])
        hits += 1
        if hits > 40:
            break
