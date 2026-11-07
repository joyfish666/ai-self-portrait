#!/usr/bin/env python3
"""消化队列：挑出一批待消化的事实，按"判断最少的域优先 + 最早优先"。

用法：
    python scripts/digest.py          # 默认 5 条
    python scripts/digest.py 8        # 8 条
    python scripts/digest.py --oldest # 只处理最老的一笔（到期重试用这个）

输出给代理看；代理按 AGENTS.md 的规则把它们变成 claim / 排除。
"""
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
ME = ROOT / "me"
sys.path.insert(0, str(ROOT / "scripts"))
import check as check_mod  # noqa: E402
from store import load  # noqa: E402

DOMAINS = ["identity", "work", "goals", "values", "emotion",
           "health", "relations", "leisure"]
JUDGED = ("disposition", "inference")


def main() -> int:
    args = sys.argv[1:]
    facts, claims = load("facts.jsonl"), load("claims.jsonl")

    if "--oldest" in args:
        dr = check_mod.debt_report(facts, claims)
        if not dr["count"]:
            print("✅ 没有待消化的事实")
            return 0
        o = dr["oldest"]
        print(f"最老的一笔：{o['id']}（欠了 {dr['age']} 天，topic={o.get('topic')}）")
        print(f"  问的是：{o.get('asked') or '（没记 asked）'}")
        print(f"  原话：{o['text']}")
        same = [c for c in claims
                if c.get("status") == "active" and c.get("topic") == o.get("topic")]
        print(f"\n同域现有判断（{len(same)} 条）：")
        for c in same:
            print(f"  {c['id']} {c['text']}")
        if dr["cands"]:
            print("\n字面最像的：")
            for c in dr["cands"]:
                print(f"  {c['id']} 重合 {c['shared']} —— {c['text']}")
        print("\n三选一：")
        print(f"  挂上去   python scripts/commit.py --attach c-xxx:{o['id']}")
        print(f"  立新判断 basis 里放 {o['id']}")
        print(f"  标废     python scripts/commit.py --exclude {o['id']} "
              f"--reason \"无判断价值：…\"")
        return 0

    n = int(args[0]) if args and args[0].isdigit() else 5

    have = {d: sum(1 for c in claims
                   if c.get("topic") == d and c.get("type") in JUDGED and
                   c.get("status") == "active") for d in DOMAINS}

    cited = {b for c in claims for b in c.get("basis", [])}
    pending = [f for f in facts if not f.get("excluded") and f["id"] not in cited]
    pending.sort(key=lambda f: (have.get(f.get("topic"), 99), f.get("date", ""), f["id"]))
    batch = pending[:n]

    if not batch:
        print("✅ 没有待消化的事实")
        return 0

    print(f"待消化 {len(pending)} 条，本批 {len(batch)} 条")
    print(f"判断密度：{ {d: have[d] for d in DOMAINS} }")
    print("")
    for f in batch:
        print(f"[{f['id']}] {f['date']} · {f.get('kind','?')} · topic={f.get('topic')}")
        if f.get("asked"):
            print(f"  问的是：{f['asked']}")
        print(f"  原话：{f['text']}")
        print("")
    print("接下来：对每条判断——支撑已有 claims / 新增 claim / 标 noise。")
    print("新增的 claim 必须带 basis（就用上面的 id）和 context。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
