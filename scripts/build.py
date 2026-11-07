#!/usr/bin/env python3
"""渲染视图 + 变更记录。

纪律：零 LLM、输出确定性、写后断言、自更新账本。
"""
import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
ME = ROOT / "me"
sys.path.insert(0, str(ROOT / "scripts"))
from store import age_days, age_text  # noqa: E402

DOMAINS = ["identity", "work", "goals", "values", "emotion",
           "health", "relations", "leisure"]
TOPIC_ORDER = ["interface"] + DOMAINS + ["misc"]
TOPIC_NAME = {
    "interface": "怎么跟我协作（接口）",
    "identity": "身份与经历", "work": "职业与能力", "goals": "目标与钱",
    "values": "价值与雷区", "emotion": "情绪与压力", "health": "身体与精力",
    "relations": "重要他人", "leisure": "兴趣与消费", "misc": "其他",
}
CONF_RANK = {"confirmed": 0, "inferred": 1, "unconfirmed": 2, "unsure": 3}
TYPE_RANK = {"constant": 0, "state": 1, "disposition": 2, "inference": 3}
CONTEXT_MAX_LINES = 150   # 硬上限（安全网）
JUDGED = ("disposition", "inference")


def tidy(text):
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n") + "\n"


def load(name):
    p = ME / name
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def h(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]


def marks(c, today):
    m = ""
    if c.get("ambiguous"):
        m += " ❓"
    if c.get("confidence") in ("inferred", "unsure", "unconfirmed"):
        m += " 🔶"
    if c.get("review_after") and c["review_after"] < today:
        m += " ⏳"
    return m


def sort_key(c):
    return (TOPIC_ORDER.index(c["topic"]) if c["topic"] in TOPIC_ORDER else 99,
            TYPE_RANK.get(c.get("type"), 9),
            CONF_RANK.get(c.get("confidence"), 9),
            c["id"])


def meta_line(c):
    basis = c.get("basis", [])
    bits = [f"`{c.get('id','?')}`", f"`{c.get('confidence','?')}`", f"`{c.get('type','?')}`"]
    bits.append(f"复核 {c['review_after']}" if c.get("review_after") else "长期有效")
    ids = "、".join(basis[:2]) + ("…" if len(basis) > 2 else "")
    bits.append(f"依据 {len(basis)} 条（{ids}）")
    return " · ".join(bits)


def compute_level(judged):
    """完整度只看“判断”密度：每域 ≥1 / ≥2 / ≥3 条判断。状态不算。"""
    cnt = {d: sum(1 for c in judged if c["topic"] == d and c.get("type") in JUDGED)
           for d in DOMAINS}
    if all(cnt[d] >= 3 for d in DOMAINS):
        return "L2", cnt
    if all(cnt[d] >= 2 for d in DOMAINS):
        return "L1", cnt
    if all(cnt[d] >= 1 for d in DOMAINS):
        return "L0", cnt
    return "起步", cnt


def syn_fingerprint(claims):
    items = sorted(f"{c['id']}:{h(c['text'])}" for c in claims if c.get("status") == "active")
    return h("|".join(items))


def read_synthesis(claims):
    """返回 (正文, 是否过期)。没有文件返回 (None, False)。"""
    p = ME / "SYNTHESIS.md"
    if not p.exists():
        return None, False
    t = p.read_text(encoding="utf-8")
    m = re.search(r"<!--\s*fp:([0-9a-f]{8})\s*-->", t)
    body = re.sub(r"<!--.*?-->", "", t, flags=re.S).strip()
    stale = (m.group(1) != syn_fingerprint(claims)) if m else True
    return (body or None), stale


def render_portrait(claims, tensions, today, sess=None, topics=None):
    active = [c for c in claims if c.get("status") == "active"]
    active.sort(key=sort_key)
    consts = [c for c in active if c.get("type") == "constant"]
    judged = [c for c in active if c.get("type") != "constant"]
    disp = [c for c in judged if c.get("type") in JUDGED]
    states = [c for c in judged if c.get("type") == "state"]
    level, cnt = compute_level(judged)
    overdue = sum(1 for c in judged if c.get("review_after") and c["review_after"] < today)

    rnd = f"第 {sess.get('round')} 轮 · " if (sess or {}).get("round") else ""
    L = ["# 我的自画像", ""]
    L.append(f"> {rnd}{len(disp)} 条判断 · {len(states)} 条状态 · {len(consts)} 条背景 · "
             f"完整度 {level}（覆盖 {sum(1 for d in DOMAINS if cnt[d])}/8） · "
             f"{len([t for t in tensions if t.get('status')=='open'])} 条未解张力 · "
             f"{overdue} 条待复核")
    L.append("")

    body_syn, stale = read_synthesis(claims)
    if body_syn:
        L += [body_syn, ""]
        if stale:
            L += ["> ⚠️ 综述已过期（claims 变过），重写后跑 `synth.py --stamp`。", ""]
        L += ["---", ""]

    if consts:
        L += ["## 背景", ""]
        for c in consts:
            L.append(f"- {c['text']}{marks(c, today)}")
        L.append("")

    L += ["| 域 | 判断 | 状态 |", "|---|---|---|"]
    for d in DOMAINS:
        ns = sum(1 for c in judged if c["topic"] == d and c.get("type") == "state")
        L.append(f"| {TOPIC_NAME[d]} | {cnt[d]} | {ns} |")
    L.append("")
    iface = [c for c in active if c["topic"] == "interface"]
    if iface:
        ni = sum(1 for c in iface if c.get("type") in JUDGED)
        L += [f"接口层另有 {len(iface)} 条（{ni} 条判断，不计入完整度）。", ""]
    if topics:
        bits = []
        for t in topics:
            name = TOPIC_NAME.get(t["domain"], t["domain"])
            if t.get("state") == "skipped":
                bits.append(f"跳过 {name}（{t.get('reason', '')}）")
            else:
                bits.append(f"暂缓 {name}（到 {t.get('until', '?')}）")
        L += ["> " + " ｜ ".join(bits), ""]

    cur = None
    for c in judged:
        if c["topic"] != cur:
            if cur is not None:
                L += ["", "---", ""]
            cur = c["topic"]
            L += [f"## {TOPIC_NAME.get(cur, cur)}", ""]
        L.append(f"### {c['text']}{marks(c, today)}")
        L.append("")
        L.append(c.get("context", ""))
        L.append("")
        L.append(meta_line(c))
        L.append("")

    open_t = [t for t in tensions if t.get("status") == "open"]
    if open_t:
        L += ["## 还没解开的张力", ""]
        idx = {c["id"]: c for c in active}
        for t in open_t:
            L.append(f"- **{t.get('desc','')}**")
            for x in t.get("between", []):
                cc = idx.get(x)
                if cc:
                    L.append(f"  - `{x}` {cc['text']}")
        L.append("")

    dead = [c for c in claims if c.get("status") == "superseded"]
    if dead:
        L += ["## 曾经认为", ""]
        for d in sorted(dead, key=lambda x: x.get("since", ""), reverse=True):
            L.append(f"- ~~{d['text']}~~")
        L.append("")
        L.append("> 完整原因见 REVISIONS.md")
        L.append("")

    pend = [c for c in judged if c.get("confidence") in ("inferred", "unconfirmed", "unsure")
            or c.get("ambiguous")]
    if pend:
        L += ["## 待确认 / 有歧义", ""]
        for c in pend:
            L.append(f"- {c['text']}{marks(c, today)}")
        L.append("")

    n_retired = sum(1 for c in claims if c.get("status") == "retired")
    if n_retired:
        L += [f"> 已退役 {n_retired} 条（见 REVISIONS.md）", ""]

    return tidy("\n".join(L))


def evidence_age(c, byid, today):
    """这条判断最新的依据有多旧——只在超过 30 天时显示（不然每条都是"2天前"，纯噪音）。"""
    ds = [byid[b].get("date") for b in c.get("basis", [])
          if b in byid and byid[b].get("date")]
    if not ds:
        return ""
    days = age_days(max(ds), today)
    if days is None or days < 30:
        return ""
    return age_text(days)


def render_context(claims, tensions, today, facts=None):
    byid = {f["id"]: f for f in (facts or [])}
    active = [c for c in claims if c.get("status") == "active"]
    consts = [c for c in active if c.get("type") == "constant"]
    judged = [c for c in active if c.get("type") != "constant"]
    disp = [c for c in judged if c.get("type") in JUDGED]
    states = [c for c in judged if c.get("type") == "state"]
    overdue = sum(1 for c in judged if c.get("review_after") and c["review_after"] < today)

    head = ["# 关于我（给 AI 的上下文）", ""]
    head += [
        "## 硬规则",
        "- 不要用称呼，直接说“你”。",
        "- 问偏好给选项；问情况问具体场景，不要问“你是什么样的人”。",
        "- 任务要切小：大任务会卡住，小任务能做完。",
        "- 不要把推断当事实；不确定就说不确定。",
        "- 超过 30 天的信息后面会带 ·3月前 这样的年龄；用它回答前先说一句。",
        "",
        "## 标记",
        "🔶 未确认 · ❓ 歧义 · ⏳ 待复核",
        "",
        f"_{len(disp)} 条判断 · {len(states)} 条状态 · {overdue} 条待复核_",
        "",
    ]
    if consts:
        head += ["## 背景", "", " · ".join(c["text"] for c in consts), ""]

    # 全量：域内判断在前、状态在后
    order = {d: i for i, d in enumerate(TOPIC_ORDER)}
    judged.sort(key=lambda c: (order.get(c["topic"], 99),
                               1 if c.get("type") == "state" else 0,
                               CONF_RANK.get(c.get("confidence"), 9), c["id"]))
    body = []          # [(line, is_state)]
    cur = None
    for c in judged:
        if c["topic"] != cur:
            if cur is not None:
                body.append(("", False))
            cur = c["topic"]
            body += [(f"## {TOPIC_NAME.get(cur, cur)}", False), ("", False)]
        age = evidence_age(c, byid, today)
        tail = f" · {age}" if age else ""
        body.append((f"- {c['text']}{marks(c, today)}{tail}", c.get("type") == "state"))
    body.append(("", False))

    tail = []
    open_t = [t for t in tensions if t.get("status") == "open"]
    if open_t:
        tail += ["## 张力（两个都真，别替我消解）", ""]
        for t in open_t:
            tail.append(f"- {t.get('desc','')}")
        tail.append("")

    lines = head + [b[0] for b in body] + tail

    # 安全网：只有真超了才动手，且只砍状态行（从后往前）
    if len(lines) > CONTEXT_MAX_LINES:
        excess = len(lines) - CONTEXT_MAX_LINES + 2
        keep = list(body)
        for i in range(len(keep) - 1, -1, -1):
            if excess <= 0:
                break
            if keep[i][1]:
                keep.pop(i)
                excess -= 1
        dropped = len(body) - len(keep)
        lines = head + [b[0] for b in keep] + tail
        lines += [f"<!-- 因长度上限未显示 {dropped} 条状态；完整见 PORTRAIT.md -->", ""]

    return tidy(chr(10).join(lines))


def load_state():
    p = ME / "state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def render_changes(claims, prev):
    now = {c["id"]: h(c.get("text", "")) for c in claims if c.get("status") == "active"}
    old = prev.get("texts", {})
    added = sorted(set(now) - set(old))
    removed = sorted(set(old) - set(now))
    edited = sorted(i for i in set(now) & set(old) if now[i] != old[i])
    if not (added or removed or edited):
        return tidy("# 画像变更\n\n> 与上一次 build 相比。\n\n本次没有变化。\n")
    L = ["# 画像变更", "", "> 与上一次 build 相比。", ""]
    for label, ids, src in [("新增", added, claims), ("修改", edited, claims),
                            ("移除", removed, [])]:
        if not ids:
            continue
        L += [f"## {label}（{len(ids)}）", ""]
        for i in ids:
            t = next((c["text"] for c in src if c["id"] == i), "（已不在）")
            L.append(f"- {i} {t}")
        L.append("")
    return tidy("\n".join(L))


def render_revisions(claims):
    """被推翻的 + 已退役的：谁取代了谁、为什么；哪些不用了、为什么。"""
    active = {c["id"]: c for c in claims if c.get("status") == "active"}
    dead = [c for c in claims if c.get("status") == "superseded"]
    retired = [c for c in claims if c.get("status") == "retired"]
    if not dead and not retired:
        return None
    by_target = {}
    for c in active.values():
        for old in c.get("supersedes") or []:
            by_target.setdefault(old, []).append(c)

    L = ["# 修订记录", "", "> 我曾经这样认为，后来改了。", ""]
    for d in sorted(dead, key=lambda x: x.get("since", ""), reverse=True):
        L.append(f"## {d.get('since','?')} · {d['text']}")
        L.append("")
        L.append(f"当时的依据：{len(d.get('basis', []))} 条 ｜ 置信度：{d.get('confidence','?')}")
        L.append("")
        for new in by_target.get(d["id"], []):
            L.append(f"- 改为：**{new['text']}**")
            if new.get("reason"):
                L.append(f"- 原因：{new['reason']}")
        if not by_target.get(d["id"]):
            L.append("- （没有找到取代它的判断）")
        L.append("")
    if retired:
        L += ["---", "", "## 已退役", "",
              "> 这些判断我曾经用过，现在不再拿它当依据。没被推翻，只是不用了。", ""]
        for c in sorted(retired, key=lambda x: x.get("retired_at", ""), reverse=True):
            L.append(f"## {c.get('retired_at','?')} · {c['text']}")
            L.append("")
            L.append(f"理由：{c.get('retired_reason','')}")
            L.append("")
    return tidy(chr(10).join(L))


def main():
    if not (ME / "claims.jsonl").exists():
        print("me/ 还没初始化，先跑 python scripts/init.py")
        return 1

    claims = load("claims.jsonl")
    tensions = load("tensions.jsonl")
    facts = load("facts.jsonl")
    topics = load("topics.jsonl")
    today = date.today().isoformat()
    bj = ME / ".build.json"
    prev = json.loads(bj.read_text(encoding="utf-8")) if bj.exists() else {}

    state = load_state()
    portrait = render_portrait(claims, tensions, today, state, topics)
    ctx = render_context(claims, tensions, today, facts)
    atomic_write(ME / "PORTRAIT.md", portrait)
    atomic_write(ME / "CONTEXT.md", ctx)

    atomic_write(ME / "CHANGES.md", render_changes(claims, prev))
    rev = render_revisions(claims)
    if rev:
        atomic_write(ME / "REVISIONS.md", rev)

    active = [c for c in claims if c.get("status") == "active"]
    manifest = {
        "claims": sorted(c["id"] for c in active),
        "texts": {c["id"]: h(c.get("text", "")) for c in active},
        "views": {"PORTRAIT.md": h(portrait), "CONTEXT.md": h(ctx)},
        "lines_context": len(ctx.splitlines()),
    }
    atomic_write(bj, json.dumps(manifest, ensure_ascii=False, indent=2))

    print(f"✅ PORTRAIT {len(portrait.splitlines())} 行 / CONTEXT {manifest['lines_context']} 行 / CHANGES 已更新")
    if manifest["lines_context"] > CONTEXT_MAX_LINES:
        print(f"⚠️ CONTEXT 超过 {CONTEXT_MAX_LINES} 行，建议合并判断")
    return 0


if __name__ == "__main__":
    sys.exit(main())
