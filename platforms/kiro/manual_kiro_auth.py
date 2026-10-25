# -*- coding: utf-8 -*-
"""Kiro GitHub Social 授权（Camoufox + 7897 代理）：打开 authUrl 让用户手动完成 GitHub 授权，
轮询捕获跳转 URL（kiro://... 含 code），供 A2 manual-callback 使用。"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

AUTH_URL = open(
    r"E:\AI\openAI\注册机1\any-auto-register-main\scripts\kiro_authurl.txt", encoding="utf-8"
).read().strip()
SAVE_PATH = r"E:\AI\openAI\注册机1\any-auto-register-main\scripts\kiro_redirect_url.txt"
PROXY = {"server": "http://127.0.0.1:7897"}

with Camoufox(headless=False, proxy=PROXY, exclude_addons=[DefaultAddons.UBO]) as browser:
    page = browser.new_page()
    try:
        page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        print("WINDOW OPENED (Camoufox + 7897 proxy). URL:", page.url[:140])
        print(">>> PLEASE COMPLETE GITHUB LOGIN/AUTH IN THE CAMOUFOX WINDOW")

        deadline = time.time() + 540  # 9 分钟
        captured_url = ""
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            try:
                cur = page.url
                if "code=" in cur or cur.startswith("kiro://") or "authenticate-success" in cur:
                    captured_url = cur
                    print(">>> CAPTURED redirect URL:", cur[:300])
                    break
                if cur != page.url:
                    print("URL:", cur[:150])
            except Exception:
                pass

        if captured_url:
            with open(SAVE_PATH, "w", encoding="utf-8") as f:
                f.write(captured_url)
            print("saved redirect URL to:", SAVE_PATH)
        else:
            print("no redirect captured within timeout")
            print("final URL:", page.url[:200])
    except Exception as e:
        print("exception:", str(e)[:250])
