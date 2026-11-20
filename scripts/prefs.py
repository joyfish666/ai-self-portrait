#!/usr/bin/env python3
"""维护动作的确认偏好：问不问，由本人定一次，之后按它走。

为什么需要这个文件：
    AGENTS.md 里的「一条铁律」只写了别手改数据、别绕过 commit.py，
    但没回答一个更早的问题——**合并、改名、挪域这些维护动作，
    到底要不要每次找本人点头？**
    两种做法都合理：有人想每次签字，有人只想看结果。这个偏好是人的决定，
    既不能由代理替他猜，也不该每轮重复问。

所以：一次问清，写进 me/state.json 的 prefs，留一条事件，
之后每一轮的对账单都会带一行提示——没设定就问一次，设定了就不再问。

用法：
    python scripts/prefs.py                       # 看当前设定
    python scripts/prefs.py --set never  --reason "这类维护不用问我"
    python scripts/prefs.py --set ask    --reason "合并前先跟我说"
    python scripts/prefs.py --set always --reason "所有维护动作都要点头"

三档：
    never   维护动作（合并/收窄/改名/挪域/挂依据）代理自己决定，不找他
    ask     只在「会改变意思」的动作前问（合并、改名），其余自己定
    always  每次维护都先问

code 对"哪档算对"完全沉默——它只负责把选定的一档记住，并保证没人能静默改它。
"""
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from store import ME, atomic_write, dump_jsonl, load, next_id, today_iso

KEY = "confirm_maintenance"
LEVELS = {
    "never": "维护动作自己定，不找他",
    "ask": "会改意思的动作前问一句（合并、改名）",
    "always": "每次维护都先问",
}


def read_prefs():
    p = ME / "state.json"
    if not p.exists():
        return {}, {}
    st = json.loads(p.read_text(encoding="utf-8"))
    prefs = st.get("prefs") or {}
    return st, prefs


def show(st, prefs) -> int:
    hit = (prefs.get(KEY) or {})
    value = hit.get("value")
    if value in LEVELS:
        print(f"🧭 {KEY} = {value} ｜{LEVELS[value]}")
        print(f"   决定于 {hit.get('decided_at', '?')}，由 {hit.get('by', '?')}"
              f"（{hit.get('reason', '无理由')}）")
        print("   → 已设定：启动时不必再问，按这一档走。")
        return 0
    print(f"🧭 {KEY} = 未设定")
    print("   → 启动时**必须问一次**：「合并、改名、挪域这类维护动作，要不要每次问你？」")
    print("   问完立刻落库：python scripts/prefs.py --set never|ask|always --reason \"他的原话\"")
    return 0


def set_pref(value, reason, by) -> int:
    if value not in LEVELS:
        print(f"❌ 只收 {sorted(LEVELS)}，收到 {value!r}")
        return 1
    p = ME / "state.json"
    if not p.exists():
        print("❌ me/state.json 不存在：先跑 python scripts/init.py")
        return 1
    if (ME / ".pending.json").exists():
        print("❌ me/.pending.json 还在：上一轮写盘没走完，先 --recover")
        return 1
    st, prefs = read_prefs()
    prefs[KEY] = {
        "value": value,
        "by": by,
        "reason": reason,
        "decided_at": today_iso(),
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    st["prefs"] = prefs
    atomic_write(p, json.dumps(st, ensure_ascii=False, indent=2) + "\n")

    events = load("events.jsonl")
    events.append({"id": next_id(events, "e", 4), "kind": "pref", "target": KEY,
                   "actor": by, "at": today_iso(),
                   "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "reason": f"{reason}（→ {value}）"})
    dump_jsonl(ME / "events.jsonl", events)
    print(f"✅ {KEY} = {value} ｜{LEVELS[value]}")
    print(f"   理由：{reason}（{by}）")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return show(*read_prefs())
    if "--set" not in args:
        print(__doc__)
        return 1
    value = args[args.index("--set") + 1] if args.index("--set") + 1 < len(args) else ""
    reason = args[args.index("--reason") + 1] if "--reason" in args else ""
    by = args[args.index("--by") + 1] if "--by" in args else "user"
    if not reason.strip():
        print("❌ --set 必须带 --reason：人的决定要留痕")
        return 1
    return set_pref(value, reason.strip(), by)


if __name__ == "__main__":
    sys.exit(main())