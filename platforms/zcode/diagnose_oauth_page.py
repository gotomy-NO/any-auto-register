"""快速诊断：打开 zcode OAuth 页，dump 页面真实结构（URL/文本/按钮/输入框）
用法: python diagnose_oauth_page.py
"""
import sys
import time
from urllib.parse import quote

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

OAUTH_URL = (
    "https://chat.z.ai/auth?response_type=code&client_id=client_P8X5CMWmlaRO9gyO-KSqtg"
    "&redirect_uri=" + quote("https://zcode.z.ai/login", safe="")
)


def dump(page, tag):
    try:
        print(f"[{tag}] URL: {page.url[:130]}")
    except Exception:
        pass
    try:
        body = page.evaluate('document.body ? document.body.innerText.slice(0, 800) : ""')
        print(f"[{tag}] 页面文本: {body.replace(chr(10), ' | ')[:700]}")
    except Exception as e:
        print(f"[{tag}] 文本读取失败: {e}")
    try:
        inputs = page.eval_on_selector_all(
            "input:visible",
            "els => els.map(e => (e.type || '') + '|' + (e.placeholder || '')).filter(t => t)",
        )
        print(f"[{tag}] 输入框: {inputs}")
    except Exception as e:
        print(f"[{tag}] 输入框失败: {e}")
    try:
        btns = page.eval_on_selector_all(
            "button:visible", "els => els.map(e => e.innerText.trim()).filter(t => t)"
        )
        print(f"[{tag}] 按钮: {btns[:15]}")
    except Exception as e:
        print(f"[{tag}] 按钮失败: {e}")


with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    dump(page, "STEP1-4s")
    page.wait_for_timeout(6000)
    dump(page, "STEP2-10s")
    # 等更久看是否延迟加载
    page.wait_for_timeout(8000)
    dump(page, "STEP3-18s")
    print(">>> 保持 20s 观察")
    time.sleep(20)
print(">>> 结束")
