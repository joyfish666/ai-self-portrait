#!/usr/bin/env python3
"""矛盾（tension）：两条都真、但指向相反行动的判断。

用法：
    python scripts/tension.py --list          # 看现有矛盾
    python scripts/tension.py --worksheet     # 按域摆出全部判断，供代理/人扫矛盾
    python scripts/tension.py --scan          # 词面线索（很弱，仅参考）
    python scripts/tension.py --add c-029 c-026 "描述"
    python scripts/tension.py --check         # 校验已记录矛盾

为什么默认不是自动检测：
    判断都被压到 ≤40 字且经过改写，**字面重合度极低**（实测最高 2 个 bigram）。
    矛盾只能靠语义判断 —— 脚本负责“把材料摆好 + 校验”，不负责“发现”。
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
ME = ROOT / "me"
sys.path.insert(0, str(ROOT / "scripts"))
from store import load  # noqa: E402

DOMAINS = ["identity", "work", "methods", "goals", "values", "emotion",
           "health", "relations", "leisure"]
NAME = {"identity": "身份", "work": "职业", "methods": "方法", "goals": "目标",
        "values": "价值", "emotion": "情绪", "health": "身体",
        "relations": "关系", "leisure": "兴趣"}
JUDGED = ("disposition", "inference")


COMMIT = Path(__file__).resolve().parent / "commit.py"


def call_commit(*argv):
    """写入只走 commit.py：它负责校验、重建视图、体检、提交内层仓。

    以前 --add / --close 自己 dump_jsonl，绕过了唯一写入路径——写进去的张力
    不会进 events、不会重算视图，也不会被 check 抓到（manifest 只看 claims 的 id）。
    """
    r = subprocess.run([sys.executable, str(COMMIT), *argv], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    print(((r.stdout or "") + (r.stderr or "")).strip())
    return r.returncode


def bigrams(t):
    t = re.sub(r"[\s，。；：、？！“”‘’（）()\-—…|·0-9]+", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def main() -> int:
    args = sys.argv[1:]
    claims = load("claims.jsonl")
    tensions = load("tensions.jsonl")
    active = [c for c in claims if c.get("status") == "active"]
    by_id = {c["id"]: c for c in active}

    if "--list" in args:
        if not tensions:
            print("还没有矛盾记录。")
        for t in tensions:
            a, b = (by_id.get(x, {}) for x in t.get("between", [])[:2])
            print(f"{t['id']} [{t.get('status')}] {t['desc']}")
            print(f"   A {a.get('id','?')} {a.get('text','')}")
            print(f"   B {b.get('id','?')} {b.get('text','')}")
        return 0

    if "--check" in args:
        bad = 0
        for t in tensions:
            ids = t.get("between", [])
            if len(ids) != 2:
                print(f"❌ {t.get('id')} between 必须是 2 个 id")
                bad += 1
                continue
            for x in ids:
                if x not in by_id:
                    print(f"❌ {t.get('id')} 指向不存在或非活跃的 {x}")
                    bad += 1
            if not (5 <= len(t.get("desc", "")) <= 100):
                print(f"❌ {t.get('id')} desc 应在 5–100 字")
                bad += 1
            if t.get("status") not in ("open", "closed"):
                print(f"❌ {t.get('id')} status 非法：{t.get('status')}")
                bad += 1
            if t.get("status") == "closed" and not t.get("closed_reason"):
                print(f"❌ {t.get('id')} 已关闭但没写 closed_reason")
                bad += 1
        print("✅ 矛盾数据合规" if not bad else f"共 {bad} 个问题")
        return 1 if bad else 0

    if "--close" in args or "--reopen" in args:
        flag = "--close" if "--close" in args else "--reopen"
        i = args.index(flag)
        tid = args[i + 1] if len(args) > i + 1 else ""
        if not tid:
            print(f"❌ 用法：{flag} t-001")
            return 1
        if flag == "--close":
            reason = args[args.index("--reason") + 1] if "--reason" in args else ""
            if not reason:
                print("❌ 关闭张力要写 --reason（这条张力是怎么收场的）")
                return 1
            return call_commit("--close-tension", tid, "--reason", reason)
        return call_commit("--reopen-tension", tid)

    if "--add" in args:
        i = args.index("--add")
        if len(args) < i + 4:
            print('❌ 用法：--add c-001 c-002 "描述"')
            return 1
        a, b, desc = args[i + 1], args[i + 2], args[i + 3]
        fd, path = tempfile.mkstemp(suffix=".json", prefix="tension_")
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                json.dump([{"between": [a, b], "desc": desc}], fh, ensure_ascii=False)
            return call_commit("--tensions", path)
        finally:
            Path(path).unlink(missing_ok=True)

    if "--worksheet" in args or not args:
        print("按域摆出全部判断。逐对问：**这两条会不会拉着人往相反方向走？**\n")
        for d in DOMAINS:
            sub = [c for c in active if c["topic"] == d and c.get("type") in JUDGED]
            if len(sub) < 2:
                continue
            print(f"## {NAME[d]}（{len(sub)} 条）")
            for c in sub:
                print(f"  {c['id']} {c['text']}")
            print("")
        print('有候选就：python scripts/tension.py --add c-xxx c-yyy "描述"')
        return 0

    # --scan：词面线索（很弱）
    print("⚠️ 字面线索仅供参考——判断是短句且经过改写，重合度天然很低。\n")
    cands = []
    for i, a in enumerate(active):
        if a.get("type") not in JUDGED:
            continue
        for b in active[i + 1:]:
            if b.get("type") not in JUDGED or a["topic"] != b["topic"]:
                continue
            ov = bigrams(a["text"]) & bigrams(b["text"])
            if len(ov) >= 2:
                cands.append((len(ov), a, b, ov))
    cands.sort(key=lambda x: -x[0])
    for n, a, b, ov in cands[:10]:
        print(f"[{n}] {a['id']} {a['text']}  ×  {b['id']} {b['text']}")
    print(f"\n词面线索 {len(cands)} 对（不代表是矛盾）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

