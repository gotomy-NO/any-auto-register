# -*- coding: utf-8 -*-
"""自有号（self-owned）9Router 连接登记工具 —— 双流调度设计（docs/双流账号调度设计-20261004.md）落地。

定位：注册机产号（source=pipeline，耗材可冲量）与用户自有付费账号（source=self-owned，
资产不可当耗材）在 9Router 混存同一 providerConnections 表，本脚本把「自有号」
第一次写进台账（新表 self_owned_accounts），让回查/清扫体系能识别并保护它们。

自有号判定（满足任一即自有）：
  A. 连接 name 以 'own:' 前缀开头（规范约定，改名操作在 9Router 面板做）；
  B. 连接在台账 accounts（注册产线表）无对应行 —— 按序匹配：
     ①extra.nr_conn_id == 连接 id；②(platform映射, email) 精确匹配；
     ③email 为空且 name 无邮箱 → 无台账行（如 opencode noauth / zcode-apikey-test）。
  C. --emails 显式补充指定（兜底人工通道，逗号分隔）。

注意：qoder/tokenrouter/xai/opencode 免费或第三方通道无 email、不可能有台账行，
也会被 B 判成自有 —— 语义正确：它们同样是「不可牺牲」的供给源，享受同等保护。

动作：
  1. 建表 self_owned_accounts（幂等，SQLite 原生建表，不依赖服务重启）：
     id INTEGER PK / platform TEXT / email TEXT / conn_id TEXT UNIQUE
     / source TEXT DEFAULT 'self-owned' / priority TEXT / notes TEXT / added_at TEXT
  2. 逐连接登记：自有号 → upsert（conn_id 冲突则更新）；产线号 → 不建行，
     只把 conn_id 补写进台账 accounts.extra.nr_conn_id（缺才补，不重复建行）。
  3. --deactivate-sweep 提示位（本脚本从不下架任何连接——保护者只登记不杀）。

用法（从注册机根目录）:
    .venv/Scripts/python.exe scripts/register_selfowned.py            # 实跑登记
    .venv/Scripts/python.exe scripts/register_selfowned.py --dry-run  # 只出判定报告
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OWN_PREFIX = "own:"                     # 9Router 连接 name 前缀约定（面板改名生效）
PROVIDER_MAP = {"grok": "grok-cli", "chatgpt": "codex", "codex": "codex"}  # 台账platform→9R provider
SELFOWN_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS self_owned_accounts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    platform  TEXT NOT NULL DEFAULT '',
    email     TEXT NOT NULL DEFAULT '',
    conn_id   TEXT NOT NULL UNIQUE,
    source    TEXT NOT NULL DEFAULT 'self-owned',
    priority  TEXT NOT NULL DEFAULT 'normal',
    notes     TEXT NOT NULL DEFAULT '',
    added_at  TEXT NOT NULL
)
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_table(conn) -> None:
    conn.execute(SELFOWN_TABLE_DDL)
    conn.commit()


def is_own_name(name: str) -> bool:
    return str(name or "").strip().lower().startswith(OWN_PREFIX)


def _email_in_name(name: str) -> bool:
    import re

    return bool(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", str(name or "")))


def find_ledger_row(conn, platform_ledger: str, email: str, conn_id: str):
    """台账 accounts 匹配：nr_conn_id → (platform,email)。返回 (id, extra_dict) 或 None。"""
    cur = conn.execute("SELECT id, email, extra_json FROM accounts WHERE extra_json LIKE ?", (f"%{conn_id}%",))
    for row in cur.fetchall():
        try:
            extra = json.loads(row["extra_json"] or "{}")
        except Exception:
            continue
        if extra.get("nr_conn_id") == conn_id:
            return row["id"], extra
    if email:
        cur = conn.execute(
            "SELECT id, email, extra_json FROM accounts WHERE platform=? AND lower(email)=lower(?)",
            (platform_ledger, email),
        )
        row = cur.fetchone()
        if row:
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except Exception:
                extra = {}
            return row["id"], extra
    return None


def classify_connection(conn_row: dict, extra_emails: set) -> dict:
    """对单个 9R 连接做自有/产线判定。返回 {id, provider, name, email, verdict, evidence}。"""
    cid = conn_row.get("id") or ""
    provider = conn_row.get("provider") or ""
    name = str(conn_row.get("name") or conn_row.get("displayName") or "")
    email = str(conn_row.get("email") or "").strip()
    verdict, evidence = None, ""

    if is_own_name(name):
        verdict, evidence = "self-owned", f"name 前缀 {OWN_PREFIX}"
    elif email and email.lower() in extra_emails:
        verdict, evidence = "self-owned", "--emails 显式指定"
    return {"id": cid, "provider": provider, "name": name, "email": email,
            "verdict": verdict, "evidence": evidence}


def run(dry_run: bool = False, extra_emails: set | None = None) -> dict:
    extra_emails = extra_emails or set()
    from core.nr_client import NRClient

    cli = NRClient()
    conns = cli.list_connections()

    import sqlite3
    from core.db import DEFAULT_DB_PATH

    report = {"ts": now_iso(), "dry_run": dry_run, "total_conns": len(conns),
              "self_owned": [], "pipeline": [], "ledger_link": [], "errors": []}

    db = sqlite3.connect(str(DEFAULT_DB_PATH))
    db.row_factory = sqlite3.Row
    ensure_table(db)

    # 已在册 conn_id → 不重复判定报告
    known = {r["conn_id"] for r in db.execute("SELECT conn_id FROM self_owned_accounts")}

    # 先做台账 (platform,email) 索引供产线判定
    ledger_emails = set()
    for row in db.execute("SELECT platform, email FROM accounts"):
        ledger_emails.add((row["platform"], (row["email"] or "").lower()))

    pending = []
    for c in conns:
        info = classify_connection(c, extra_emails)
        if info["verdict"] is None:
            p9 = info["provider"]
            email = info["email"]
            # 反向 provider 映射（9R provider → 台账 platform 候选）
            plats = [k for k, v in PROVIDER_MAP.items() if v == p9]
            hit = None
            if email:
                for pl in plats:
                    if (pl, email.lower()) in ledger_emails:
                        hit = pl
                        break
                if hit is None and any(email.lower() == le for _, le in ledger_emails):
                    hit = "email-only"
            if hit is None and email and not _email_in_name(info["name"]):
                # 有 email 但完全无台账行 → 自有（非产线临时邮箱形态）
                info["verdict"], info["evidence"] = "self-owned", "无台账对应行"
            elif hit is not None:
                info["verdict"], info["evidence"] = "pipeline", f"台账匹配 platform={hit}"
            else:
                # 有 email 且 name 也是该 email、但台账无行：opencode/qoder 等三方通道或未登记产线号
                info["verdict"], info["evidence"] = "self-owned", "无台账对应行"
        pending.append(info)

    for info in pending:
        if info["verdict"] == "pipeline":
            report["pipeline"].append(info)
            continue
        # ---- 自有号登记 ----
        p9 = info["provider"]
        plats = [k for k, v in PROVIDER_MAP.items() if v == p9]
        platform = plats[0] if plats else p9
        row = {
            "platform": platform,
            "email": info["email"],
            "conn_id": info["id"],
            "notes": f"auto@{now_iso()} via register_selfowned; evidence={info['evidence']}; "
                     f"name={info['name'][:60]}",
        }
        if info["id"] in known:
            row["note"] = "already-registered(更新 notes)"
            db.execute(
                "UPDATE self_owned_accounts SET platform=?, email=?, notes=? WHERE conn_id=?",
                (row["platform"], row["email"], row["notes"], row["conn_id"]),
            )
        else:
            row["note"] = "new-registered"
            db.execute(
                "INSERT INTO self_owned_accounts (platform, email, conn_id, source, priority, notes, added_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (row["platform"], row["email"], row["conn_id"], "self-owned", "normal", row["notes"], now_iso()),
            )
        report["self_owned"].append(row)

    # ---- 产线号补 conn 关联（不建新行）----
    for info in report["pipeline"]:
        p9 = info["provider"]
        plats = [k for k, v in PROVIDER_MAP.items() if v == p9]
        if not info["email"] or not plats:
            continue
        hit = find_ledger_row(db, plats[0], info["email"], info["id"])
        if hit is None:
            continue
        acct_id, extra = hit
        if extra.get("nr_conn_id") != info["id"]:
            if not dry_run:
                extra["nr_conn_id"] = info["id"]
                db.execute("UPDATE accounts SET extra_json=? WHERE id=?",
                           (json.dumps(extra, ensure_ascii=False), acct_id))
            report["ledger_link"].append({"account_id": acct_id, "email": info["email"],
                                          "conn_id": info["id"], "action": "补写 nr_conn_id"})

    if not dry_run:
        db.commit()
    db.close()
    return report


def render(report: dict) -> str:
    mode = "DRY-RUN（未写库）" if report.get("dry_run") else "实跑"
    lines = [f"=== register_selfowned {report['ts']} [{mode}] 9R连接共 {report['total_conns']} ==="]
    lines.append(f"\n[自有号登记] {len(report['self_owned'])} 条：")
    for r in report["self_owned"]:
        lines.append(f"  - {r['platform']:<10} {(r['email'] or '(无email)')[:34]:<36} "
                     f"conn={r['conn_id'][:8]} [{r['note']}]")
    lines.append(f"\n[产线号（有台账行，不建自有行）] {len(report['pipeline'])} 条")
    if report["ledger_link"]:
        lines.append(f"[补写台账 nr_conn_id] {len(report['ledger_link'])} 条：")
        for r in report["ledger_link"]:
            lines.append(f"  - account#{r['account_id']} {r['email'][:34]} ← conn {r['conn_id'][:8]}")
    else:
        lines.append("[补写台账 nr_conn_id] 0 条（产线号关联均已齐全）")
    if report["errors"]:
        lines.append(f"[异常] {report['errors']}")
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = sys.argv[1:]
    extra = set()
    for a in args:
        if a.startswith("--emails="):
            extra = {x.strip().lower() for x in a.split("=", 1)[1].split(",") if x.strip()}
    rep = run(dry_run="--dry-run" in args, extra_emails=extra)
    print(render(rep))
    # 报告落盘（scratch，不做长期台账）
    try:
        scratch = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scratch")
        os.makedirs(scratch, exist_ok=True)
        with open(os.path.join(scratch, "register_selfowned_last.json"), "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
