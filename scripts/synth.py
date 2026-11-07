#!/usr/bin/env python3
"""综述层：LLM 写、每句带引用、claims 变了就会过期。

用法：
    python scripts/synth.py --worksheet   # 摆材料，供写作
    python scripts/synth.py --check       # 校验引用与时效
    python scripts/synth.py --stamp       # 写完正文后盖时间戳（记录它基于哪版 claims）
"""
import hashlib
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
ME = ROOT / "me"
sys.path.insert(0, str(ROOT / "scripts"))
from store import atomic_write, load  # noqa: E402

SYN = ME / "SYNTHESIS.md"
FP_RE = re.compile(r"<!--\s*fp:([0-9a-f]{8})\s*-->")
CITE_RE = re.compile(r"\[(c-\d{3,4})\]")
JUDGED = ("disposition", "inference")


def h(t):
    return hashlib.md5(t.encode("utf-8")).hexdigest()[:8]


def fingerprint(claims):
    items = sorted(f"{c['id']}:{h(c['text'])}" for c in claims if c.get("status") == "active")
    return h("|".join(items))


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    claims = load("claims.jsonl")
    active = [c for c in claims if c.get("status") == "active"]
    by_id = {c["id"]: c for c in active}
    tens = [t for t in load("tensions.jsonl") if t.get("status") == "open"]
    fp = fingerprint(claims)

    if mode == "--worksheet":
        print(f"当前指纹：{fp}（{len(active)} 条判断）\n")
        core = {x for t in tens for x in t.get("between", [])}
        print("## 张力涉及的判断（优先写）")
        for cid in sorted(core):
            if cid in by_id:
                print(f"  {cid} {by_id[cid]['text']}")
        print("\n## 全部判断")
        for c in active:
            if c.get("type") in JUDGED:
                print(f"  {c['id']} [{c['topic'][:4]}] {c['text']}")
        print("\n写完后：python scripts/synth.py --stamp")
        return 0

    if mode == "--stamp":
        if not SYN.exists():
            print("还没有 SYNTHESIS.md")
            return 1
        t = SYN.read_text(encoding="utf-8")
        t = FP_RE.sub("", t).rstrip()
        t = f"<!-- fp:{fp} -->\n" + t + "\n"
        atomic_write(SYN, t)
        print(f"已盖戳：{fp}")
        return 0

    # --check
    if not SYN.exists():
        print("还没有 SYNTHESIS.md（可选文件）")
        return 0
    t = SYN.read_text(encoding="utf-8")
    m = FP_RE.search(t)
    body = re.sub(r"<!--.*?-->", "", t, flags=re.S).strip()
    lines = [l for l in body.splitlines() if l.strip() and not l.startswith("#")]
    cited = CITE_RE.findall(body)
    bad = 0
    if not m:
        print("❌ 缺指纹：跑 --stamp")
        bad += 1
    elif m.group(1) != fp:
        print(f"⚠️  综述过期：写作时 fp={m.group(1)}，现在是 {fp}（重写后 --stamp）")
    for cid in set(cited):
        if cid not in by_id:
            print(f"❌ 引用了不存在或已失效的判断 {cid}")
            bad += 1
    with_cite = sum(1 for l in lines if CITE_RE.search(l))
    ratio = 100 * with_cite / len(lines) if lines else 0
    if lines and ratio < 70:
        print(f"❌ 带引用的段落只有 {ratio:.0f}%（要求 ≥70%）")
        bad += 1
    print(f"段落 {len(lines)} · 引用 {len(cited)} 处 · 覆盖 {ratio:.0f}%")
    print("✅ 综述合规" if not bad else f"共 {bad} 个问题")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

