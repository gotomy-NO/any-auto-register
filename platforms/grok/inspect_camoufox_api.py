"""确认 Camoufox sync_api 的 API 用法（new_context / new_page / cookies / close）"""
import inspect

from camoufox.sync_api import Camoufox

print("Camoufox 公开方法:", [m for m in dir(Camoufox) if not m.startswith("_")])
for name in ("new_context", "new_page", "close"):
    if hasattr(Camoufox, name):
        try:
            print(f"{name} 签名: {inspect.signature(getattr(Camoufox, name))}")
        except Exception as e:
            print(f"{name}: 无法取签名 {e}")
