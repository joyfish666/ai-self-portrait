#!/usr/bin/env python3
"""给判断找证据：按字面重合度+同域，为每条 claim 推荐候选 basis。

用法：
    python scripts/suggest.py                 # 只报告
    python scripts/suggest.py c-029           # 只看某条
    python scripts/suggest.py --auto          # 高置信度自动补（同域 + 重合≥6 + 分值≥0.10）
    python scripts/suggest.py --apply c-029 f-0249,f-0269

原则：算法只做"召回"，不替人做判断。--auto 的阈值很保守，可随时改回。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
ME = ROOT / "me"
sys.path.insert(0, str(ROOT / "scripts"))
from store import load, overlap  # noqa: E402

MIN_SHARED = 3
AUTO_SHARED = 6
AUTO_SCORE = 0.10
TOP = 3


COMMIT = Path(__file__).resolve().parent / "commit.py"


def call_commit(*argv):
    """补依据也走 commit.py：它会重建视图、体检、提交内层仓。

    以前 --apply / --auto 直接改 claims.jsonl。数据改了，视图没重建，
    check 也只比对 build 写下的哈希——两边一起旧，谁都不会报。
    """
    r = subprocess.run([sys.executable, str(COMMIT), *argv], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    print(((r.stdout or "") + (r.stderr or "")).strip())
    return r.returncode


def score(claim, fact):
    return overlap(claim["text"] + claim.get("context", ""), fact["text"])


def main() -> int:
    args = sys.argv[1:]

    if "--apply" in args:
        i = args.index("--apply")
        if len(args) < i + 3:
            print("❌ 用法：--apply c-029 f-0249,f-0269")
            return 1
        cid, ids = args[i + 1], args[i + 2]
        return call_commit("--attach", f"{cid}:{ids}")

    claims = [c for c in load("claims.jsonl") if c.get("status") == "active"]
    facts = [f for f in load("facts.jsonl") if not f.get("excluded")]

    facts_by_id = {f["id"]: f for f in facts}
    if "--auto" in args:
        specs, added, touched = [], 0, 0
        for c in claims:
            picked = []
            for f in facts:
                if f["id"] in c["basis"]:
                    continue
                s, shared = score(c, f)
                if f["topic"] == c["topic"] and shared >= AUTO_SHARED and s >= AUTO_SCORE:
                    picked.append(f["id"])
            if picked:
                specs.append(f"{c['id']}:{','.join(picked[:2])}")
                added += len(picked[:2])
                touched += 1
                if "--dry" in args:
                    print(f"{c['id']} {c['text']}")
                    for x in picked[:2]:
                        print(f"   + {x} {facts_by_id[x]['text'][:56]}")
        if "--dry" in args:
            print(f"[dry] 自动补齐：{touched} 条判断，共 +{added} 条依据"
                  f"（阈值 同域/重合≥{AUTO_SHARED}/分值≥{AUTO_SCORE}）")
            return 0
        if not specs:
            print("没有需要补依据的判断")
            return 0
        return call_commit("--attach", *specs)

    only = next((a for a in args if a.startswith("c-")), None)
    thin = 0
    shown = 0
    for c in sorted(claims, key=lambda x: len(x["basis"])):
        if only and c["id"] != only:
            continue
        if len(c["basis"]) >= 2 and not only:
            continue
        cands = []
        for f in facts:
            if f["id"] in c["basis"]:
                continue
            s, shared = score(c, f)
            if shared >= MIN_SHARED:
                cands.append((s, shared, f))
        cands.sort(key=lambda x: -x[0])
        cands = cands[:TOP]
        thin += 1
        print(f"\n{c['id']} [{c['topic']}] {c['text']}")
        print(f"   现有依据 {len(c['basis'])} 条：{', '.join(c['basis'])}")
        for s, shared, f in cands:
            print(f"   ? {f['id']} score={s:.2f} shared={shared}  {f['text'][:52]}")
        if not cands:
            print("   （没有候选）")
        shown += 1

    print(f"\n依据不足（<2 条）的判断：{thin} 条；已展示 {shown} 条")
    print("确认后： python scripts/suggest.py --apply c-029 f-0249,f-0269")
    return 0


if __name__ == "__main__":
    sys.exit(main())

