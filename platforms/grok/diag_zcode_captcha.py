"""任务 #4 诊断：查 zcode 实际 captcha 配置 + 阿里云 SDK 可达性"""
import requests

# 1) zcode 客户端配置（captcha sceneId/region/prefix 来源）
try:
    r = requests.get(
        "https://zcode.z.ai/api/v1/client/configs?app_version=3.0.0&platform=win32",
        timeout=25,
    )
    print("configs status:", r.status_code)
    print(r.text[:1500])
except Exception as e:
    print("configs ERR:", str(e)[:160])

print("=" * 50)
# 2) 阿里云 SDK 可达性
for url in [
    "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js",
    "https://aliyuncs.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js",
]:
    try:
        r2 = requests.get(url, timeout=25)
        print(f"SDK {url} status: {r2.status_code} len: {len(r2.text)} head: {r2.text[:60]}")
    except Exception as e:
        print(f"SDK {url} ERR: {str(e)[:140]}")
