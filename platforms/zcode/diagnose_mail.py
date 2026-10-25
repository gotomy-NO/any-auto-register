"""只读诊断：读取 <REDACTED_EMAIL> 收件箱的 Verify Your Email 邮件原文
不弹号、不写库、不发信——仅 SELECT 账号 + Graph 读邮件，检查链接格式与正则匹配。
"""
import re
import sys

sys.path.insert(0, r"E:\AI\openAI\注册机1\any-auto-register-main")

from sqlmodel import Session, select
from core.db import engine, OutlookAccountModel
from core.base_mailbox import OutlookMailbox
from platforms.zcode.core import VERIFY_URL_PATTERN

mailbox = OutlookMailbox(backend="graph")

with Session(engine) as session:
    acct = session.exec(
        select(OutlookAccountModel).where(OutlookAccountModel.email == "<REDACTED_EMAIL>")
    ).first()
    if not acct:
        print("!! 库里没有 <REDACTED_EMAIL>")
        sys.exit(1)
    print("账号存在: enabled=", acct.enabled)

from core.base_mailbox import MailboxAccount

ma = MailboxAccount(
    email=acct.email,
    account_id=str(acct.id),
    extra={
        "provider": "microsoft",
        "password": acct.password or "",
        "client_id": acct.client_id or "",
        "refresh_token": acct.refresh_token or "",
        "outlook_backend": "graph",
    },
)

try:
    token = mailbox._get_oauth_access_token(ma, preferred_backend="graph")
    print("OAuth token OK, len=", len(token or ""))
except Exception as e:
    print("!! OAuth 失败:", str(e)[:200])
    sys.exit(1)

msgs = mailbox._graph_list_messages(access_token=token, folder="inbox")
print("inbox 邮件数:", len(msgs))
for m in msgs:
    subj = str(m.get("subject") or "")
    if "verify" in subj.lower() or "z.ai" in subj.lower() or "z.ai" in (m.get("bodyPreview") or "").lower():
        print("=" * 70)
        print("SUBJECT:", subj)
        print("PREVIEW:", str(m.get("bodyPreview") or "")[:400])
        mid = str(m.get("id") or "")
        detail = mailbox._graph_get_message(access_token=token, message_id=mid)
        body = detail.get("body") or {}
        content = str(body.get("content") or "")
        content_type = body.get("contentType") or ""
        print("BODY contentType:", content_type, "len:", len(content))
        print("--- RAW BODY (first 3000) ---")
        print(content[:3000])
        print("--- decoded text ---")
        txt = mailbox._graph_message_text(detail)
        print(txt[:1500])
        m2 = re.search(VERIFY_URL_PATTERN, txt)
        print("--- VERIFY_URL_PATTERN 匹配:", "命中 -> " + m2.group(0) if m2 else "未命中")
        # 看看有没有任何 chat.z.ai / auth 相关链接
        found = re.findall(r"https?://[^\s\"'<>]+", txt)
        print("--- 邮件中的全部链接 ---")
        for u in found[:20]:
            print("  ", u[:160])
        break
