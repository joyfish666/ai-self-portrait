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
from itertools import combinations
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
ME = ROOT / "me"
sys.path.insert(0, str(ROOT / "scripts"))
import check as check_mod  # noqa: E402
from store import load  # noqa: E402

DOMAINS = ["identity", "work", "methods", "goals", "values", "emotion",
           "health", "relations", "leisure"]
NAME = {"identity": "身份", "work": "职业", "methods": "方法", "goals": "目标",
        "values": "价值", "emotion": "情绪", "health": "身体",
        "relations": "关系", "leisure": "兴趣"}
JUDGED = ("disposition", "inference")


def cut(t, n=22):
    t = " ".join(str(t).split())
    return t if len(t) <= n else t[:n] + "…"


def candidates(claims, tensions, domain=None):
    """只筛范围，不下判断：同域 + 都是判断类 + 没登记过 + 不是重复。

    为什么是这两个条件（不是"全部两两比"）：
    - 跨域两条很少真冲突：工作上看重稳定 vs 周末爱冒险，是两个场合的取舍，
      报出来也没法处理（你删不掉其中一条）。
    - type 不同比不出冲突：constant 是背景（几岁、在哪）、state 是当下情况
      （最近在带新人）、disposition/inference 是倾向。前两者跟后者是描述关系，
      不是互相抵消。「每天在开会」和「最怕给别人做东西」不是打架，是解释。

    剩下的才交给 LLM 逐对判「会不会往相反方向走」——这一步代码不做。
    """
    done = {frozenset(t.get("between") or []) for t in tensions}
    live = [c for c in claims
            if c.get("status") == "active"
            and c.get("type") in ("disposition", "inference")
            and (domain is None or c.get("topic") == domain)]
    groups = {}
    for c in live:
        groups.setdefault(c.get("topic"), []).append(c)

    out = []
    for topic in sorted(groups):
        group = sorted(groups[topic], key=lambda x: x.get("id", ""))
        for a, b in combinations(group, 2):
            if frozenset((a.get("id"), b.get("id"))) in done:
                continue
            # 字面几乎一样的是"重复"，那是 check 的坏味道，不是张力
            if check_mod.contain(a.get("text", ""), b.get("text", "")) >= 0.9:
                continue
            out.append((a, b))
    return out


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

    if "--candidates" in args:
        i = args.index("--candidates")
        dom = args[i + 1] if (len(args) > i + 1 and not args[i + 1].startswith("-")) else None
        pairs = candidates(claims, tensions, dom)
        if not pairs:
            print("✅ 没有待判的候选对（同域判断都登记过了，或全被判过）")
            return 0
        per = {}
        for a, b in pairs:
            per[a.get("topic")] = per.get(a.get("topic"), 0) + 1
        print(f"候选 {len(pairs)} 对 —— 同域、都是判断类、没登记过、也不是重复\n")
        cur = None
        for a, b in pairs:
            if a.get("topic") != cur:
                cur = a.get("topic")
                print(f"## {NAME.get(cur, cur)}（{per[cur]} 对）")
            print(f"  {a['id']} × {b['id']}  {cut(a['text'])} ｜ {cut(b['text'])}")
        print("\n逐对回答一个问题：**这两条会不会让人往相反方向走？**")
        print('答「会」的：python scripts/tension.py --add c-xxx c-yyy "描述"')
        print("只看一个域：python scripts/tension.py --candidates methods")
        print("这一步只筛范围、不写任何文件；判不判、判给谁，都还是你的事。")
        return 0

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

