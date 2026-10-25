# -*- coding: utf-8 -*-
"""Kiro GitHub SOCIAL 手动模式 v4：Camoufox 打开 GitHub 登录页，用户手动登录。
改进：宽松 XHR 监听 + localStorage/cookies 轮询 dump（web 登录态凭据在 localStorage 必能抓到）。"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

SAVE_PATH = r"E:\AI\openAI\AIClient2API-main\configs\kiro\social_github_creds.json"
DUMP_PATH = r"E:\AI\openAI\AIClient2API-main\configs\kiro\localstorage_dump.json"
PROXY = {"server": "http://127.0.0.1:7897"}
captured = {}


def on_response(resp):
    try:
        url = resp.url
        if ("kiro.dev" in url or "awsapps.com" in url or "amazonaws" in url) and resp.status == 200:
            ct = resp.headers.get("content-type", "") if resp.headers else ""
            if "json" in ct:
                try:
                    d = resp.json()
                    if isinstance(d, dict):
                        for k in ("profileArn", "accessToken", "access_token", "refreshToken", "sessionToken", "idToken"):
                            if d.get(k):
                                captured.setdefault(k, d[k])
                        if "profileArn" in captured:
                            print(">>> PROFILEARN via XHR")
                except Exception:
                    pass
    except Exception:
        pass


with Camoufox(headless=False, proxy=PROXY, exclude_addons=[DefaultAddons.UBO]) as browser:
    page = browser.new_page()
    page.on("response", on_response)
    try:
        page.goto("https://app.kiro.dev/signin", wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(2500)
        gh = page.locator("button", has_text="GitHub")
        if gh.count() > 0:
            gh.first.click()
            page.wait_for_timeout(4000)
        print("WINDOW OPENED. URL:", page.url[:120])
        print(">>> PLEASE LOGIN MANUALLY IN THE CAMOUFOX WINDOW (github account/password/otp)")

        deadline = time.time() + 540  # 9 分钟
        last_dump = 0
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            # localStorage 轮询 dump
            if time.time() - last_dump > 6:
                last_dump = time.time()
                try:
                    ls = page.evaluate("() => { const o={}; for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i); o[k]=localStorage.getItem(k);} return o; }")
                    if ls:
                        dump = {}
                        for k, v in ls.items():
                            dump[k] = str(v)[:400]
                        with open(DUMP_PATH, "w", encoding="utf-8") as f:
                            json.dump(dump, f, ensure_ascii=False, indent=2)
                        # 从 localStorage 找 profileArn/token
                        for k, v in ls.items():
                            lk = k.lower()
                            if "profilearn" in lk or "profilarn" in lk:
                                captured["profileArn"] = str(v)
                            elif "accesstoken" in lk or "access_token" in lk:
                                captured.setdefault("accessToken", str(v))
                        if "profileArn" in captured:
                            print(">>> PROFILEARN via localStorage")
                except Exception:
                    pass
            if captured.get("profileArn"):
                break
        print("captured keys:", list(captured.keys()))
        with open(SAVE_PATH, "w", encoding="utf-8") as f:
            json.dump(captured, f, ensure_ascii=False, indent=2)
        print("saved to:", SAVE_PATH)
        # cookies 兜底
        try:
            cks = page.context.cookies()
            ck_dump = {c["name"]: c["value"][:200] for c in cks if "kiro" in c.get("domain", "") or "aws" in c.get("domain", "")}
            if ck_dump:
                with open(DUMP_PATH + ".cookies.json", "w", encoding="utf-8") as f:
                    json.dump(ck_dump, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    except Exception as e:
        print("exception:", str(e)[:250])
