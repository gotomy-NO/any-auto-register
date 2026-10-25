"""验证 base_mailbox.py 修复：所有临时邮箱类 get_email 均接受 platform 参数（不触碰任何邮箱资源）"""
import inspect
import sys

sys.path.insert(0, r"E:\AI\openAI\注册机1\any-auto-register-main")

from core import base_mailbox as mb

classes = [
    ("BaseMailbox(抽象)", mb.BaseMailbox),
    ("AppleMailMailbox", mb.AppleMailMailbox),
    ("LaoudoMailbox", mb.LaoudoMailbox),
    ("AitreTempMailbox", getattr(mb, "AitreTempMailbox", None)),
    ("TempMailLolMailbox", mb.TempMailLolMailbox),
    ("DuckMailMailbox", mb.DuckMailMailbox),
    ("CloudMailMailbox", mb.CloudMailMailbox),
    ("MaliAPIMailbox", getattr(mb, "MaliAPIMailbox", None)),
    ("GPTMailMailbox", getattr(mb, "GPTMailMailbox", None)),
    ("OpenTrashMailMailbox", getattr(mb, "OpenTrashMailMailbox", None)),
    ("CFWorkerMailbox", getattr(mb, "CFWorkerMailbox", None)),
    ("MoeMailMailbox", getattr(mb, "MoeMailMailbox", None)),
    ("LuckMailMailbox", getattr(mb, "LuckMailMailbox", None)),
    ("FreemailMailbox", mb.FreemailMailbox),
    ("OutlookMailbox(对照)", mb.OutlookMailbox),
]

ok = 0
for name, cls in classes:
    if cls is None:
        print(f"[SKIP] {name}: 类不存在（名称可能不同）")
        continue
    sig = inspect.signature(cls.get_email)
    params = list(sig.parameters)
    has_platform = "platform" in params
    mark = "PASS" if has_platform else "FAIL"
    if has_platform:
        ok += 1
    print(f"[{mark}] {name}: get_email{params}")

print(f"\n==== {ok} 个类接受 platform 参数（其余未找到/对照）====")
