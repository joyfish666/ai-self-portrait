#!/usr/bin/env python3
"""唯一写入路径：代理交出 JSON，代码承担所有机械动作。

代理（LLM）只负责"说了什么、怎么理解"；下面这些它一件都不许做：
    分配 id、拼 JSONL、算复核期、判重、数超额、写盘、更新进度、出对账单

用法
    python scripts/commit.py --said said.json --facts new.json --claims new.json
    python scripts/commit.py --attach c-023:f-0180,f-0181
    python scripts/commit.py --exclude f-0001,f-0002 --action noise --reason "过程记录"
    python scripts/commit.py --drafts              # 看草稿区
    python scripts/commit.py --drop d-001 --reason "已重写成 c-0102"
    python scripts/commit.py --capture said.json   # 第一步：只登记原话
    python scripts/commit.py --facts f.json --claims c.json --from-capture 51
    python scripts/commit.py --ignore s-0120 --reason "纯日常"
    python scripts/commit.py --confirm c-013 --reason "本人认这条" --by user
    python scripts/commit.py --narrow c-013 --text "…" --reason "…"
    python scripts/commit.py --migrate             # 一次性存量迁移（幂等）
    python scripts/commit.py --dry --facts ...     # 只预演，不写

输入格式
    原话：["我上个月报了个班，去了两次就没去了", {"text":"嗯", "ignored":"语气词"}]
    事实：{"ref":"a","date":"…","kind":"…","asked":"…","text":"…","topic":"…","source":"…"}
    判断：{"topic":"…","type":"…","text":"…","context":"…","source":"…",
           "confidence":"…","since":"…","basis":["f-0012","a"],"supersedes":[],"reason":null}
    矛盾：{"between":["c-0100","c-0098"],"desc":"…"}

闸门（不合格就不进正式层）
    - 校验不过 → 进 me/drafts.jsonl，不丢
    - 和同域已有判断几乎一样（≥92%）→ 进草稿，先想清楚是不是同一件事
    - 超过安全网（判断 150 / 状态 100 / 张力 10）→ 整批拒绝
    - 判断 60 / 状态 40 只是**软线**：过了会提示，不拦
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
ME = ROOT / "me"
sys.path.insert(0, str(ROOT / "scripts"))

import build as build_mod          # noqa: E402
import check as check_mod          # noqa: E402
from store import (FRAG, add_days, age_days, atomic_write, covered_by,  # noqa: E402
                   dump_jsonl, has_fragment, is_choice_code, load, load_any,
                   next_id, today_iso, to_date)

JUDGED = ("disposition", "inference")
TYPES = {"constant", "state", "disposition", "inference"}
CONFS = {"confirmed", "inferred", "unsure", "unconfirmed"}
KINDS = {"自述", "行为", "他人转述", "AI观察"}
SOURCES = {"user", "ai", "external"}
FP_RE = re.compile(r"<!--\s*fp:([0-9a-f]{8})\s*-->")


class Receipt:
    """一条输入一个 slot；add() 认领最近一个还没终态的 slot。

    以前「丢失」是 10 类图标做减法算出来的：inputs − 入库 − 排除 − …。
    那种写法每加一种收场动作就要同步改一次减法，漏一类就把真实写入算成丢失
    （selftest 里那个「剔除悬空依据不会算成负数」用例就是这么来的）。
    现在只回答一个问题：还有几条输入没有任何终态？图标只负责显示。
    """

    def __init__(self):
        self.rows = []
        self.slots = []

    def count_input(self, n=1):
        for _ in range(n):
            self.slots.append({"seq": len(self.slots) + 1, "claimed": None})

    def add(self, icon, ident, what, detail="", *, claim=True):
        """claim=False 用于纯提示（如 ⚠️）：它不构成一条输入的终态。"""
        self.rows.append((icon, ident, what, detail))
        if claim:
            for s in self.slots:          # 顺序处理：认领最早一条还没有终态的输入
                if s["claimed"] is None:
                    s["claimed"] = (icon, ident, what)
                    break

    @property
    def inputs(self):
        return len(self.slots)

    def unclaimed(self):
        return [s for s in self.slots if s["claimed"] is None]

    def print(self):
        c = Counter(r[0] for r in self.rows)
        lost = len(self.unclaimed())
        print(f"\n本轮对账（输入 {self.inputs} 条）")
        for icon, ident, what, detail in self.rows:
            tail = f"  {detail}" if detail else ""
            print(f"{icon} {ident} {what}{tail}")
        parts = [f"{c['✅']} 入库", f"{c['🚫']} 排除"]
        for icon, name in (("🚪", "退役/续期"), ("📁", "改归档"),
                           ("✂️", "撤依据"), ("🔄", "换依据"),
                           ("🕶️", "遮敏感"), ("✏️", "收窄措辞")):
            if c[icon]:
                parts.append(f"{c[icon]} {name}")
        parts += [f"{c['🔶']} 进草稿", f"{c['➖']} 跳过", f"{lost} 丢失"]
        print("\n📊 " + " ｜ ".join(parts))
        if lost:
            print("⚠️  有输入没有终态：" +
                  "、".join(f"#{s['seq']}" for s in self.unclaimed()[:5]))
        return lost


def clean(text, n=48):
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[:n] + "…"


def id_list(s):
    """'c-013, c-014' → ['c-013', 'c-014']。批量收场用，一个理由收一批。"""
    return [x.strip() for x in str(s or "").split(",") if x.strip()]


def draft(drafts, rc, kind, payload, reason, note):
    drafts.append({"id": next_id(drafts, "d", 3), "kind": kind, "payload": payload,
                   "reason": reason, "note": note, "retryable": True,
                   "attempts": 1, "since": today_iso(), "status": "open"})
    rc.add("🔶", drafts[-1]["id"], "进草稿", note)


def take_fact(raw, facts, rc, drafts, refmap):
    """校验一条事实；合格就分配 id 并入列。"""
    f = dict(raw)
    ref = f.pop("ref", None)
    rc.count_input()

    missing = [k for k in ("date", "kind", "text", "topic") if not f.get(k)]
    if missing:
        return draft(drafts, rc, "fact_draft", raw, "schema", f"缺字段 {missing}")
    if not to_date(f["date"]):
        return draft(drafts, rc, "fact_draft", raw, "bad_date", f"date 不是 YYYY-MM-DD：{f['date']}")
    if f["kind"] not in KINDS:
        return draft(drafts, rc, "fact_draft", raw, "schema", f"kind 非法：{f['kind']}")
    if len(f["text"]) > 1000:
        return draft(drafts, rc, "fact_draft", raw, "too_long", f"正文 {len(f['text'])} 字，上限 1000")
    if is_choice_code(f["text"]):
        return draft(drafts, rc, "fact_draft", raw, "choice_code",
                     "text 是选项编号，请填他点中的那项文字")
    if not f.get("asked"):
        return draft(drafts, rc, "fact_draft", raw, "missing_asked",
                     "缺 asked —— 四步判定的第 2 步靠它")
    f.setdefault("source", "user")
    if f["source"] not in SOURCES:
        return draft(drafts, rc, "fact_draft", raw, "schema", f"source 非法：{f['source']}")

    norm = "".join(str(f["text"]).split())
    for old in facts:
        if not old.get("excluded") and "".join(str(old.get("text", "")).split()) == norm:
            return draft(drafts, rc, "fact_draft", raw, "duplicate", f"和 {old['id']} 重复")

    f["id"] = next_id(facts, "f", 4)   # 已入列的最大号 +1；本批连续分配
    facts.append(f)
    if ref:
        refmap[ref] = f["id"]
    rc.add("✅", f["id"], "入库", clean(f["text"]))


def take_claim(raw, claims, fid_map, rc, drafts, refmap, tensions=None):
    c = dict(raw)
    ref = c.pop("ref", None)
    rc.count_input()

    missing = [k for k in ("topic", "type", "text", "context") if not c.get(k)]
    if missing:
        return draft(drafts, rc, "claim_draft", raw, "schema", f"缺字段 {missing}")
    if c["type"] not in TYPES:
        return draft(drafts, rc, "claim_draft", raw, "schema", f"type 非法：{c['type']}")
    if len(c["text"]) > 40:
        return draft(drafts, rc, "claim_draft", raw, "too_long", f"text {len(c['text'])} 字，上限 40")
    n = len(c["context"])
    if not (30 <= n <= 150):
        return draft(drafts, rc, "claim_draft", raw, "context_length", f"context {n} 字，应 30–150")

    c.setdefault("source", "ai")
    if c["source"] not in SOURCES:
        return draft(drafts, rc, "claim_draft", raw, "schema", f"source 非法：{c['source']}")
    # confirmed 只能由一条独立的 --confirm 事件产生，不接受提交方自报。
    # 以前红线只拦 source=ai，把 source 填成 user 就能让 AI 写的判断拿到
    # 「本人已确认」；迁移前有一批判断就是这么来的。现在这条路由代码定死。
    downgraded = c.get("confidence") == "confirmed"
    c["confidence"] = "inferred" if c["source"] == "ai" else "unconfirmed"

    basis, bad = [], []
    for b in c.get("basis") or []:
        real = refmap.get(b, b)
        (basis if real in fid_map else bad).append(real)
    if not basis:
        return draft(drafts, rc, "claim_draft", raw, "dangling_basis",
                     f"依据无法解析：{bad or '空'}")
    c["basis"] = basis

    src_text = "".join(fid_map[b]["text"] for b in basis)
    if not has_fragment(c["context"], src_text):
        return draft(drafts, rc, "claim_draft", raw, "context_no_fragment",
                     "context 没有引用依据内容（至少 8 个字连续）")

    c.setdefault("since", today_iso())
    if not to_date(c["since"]):
        return draft(drafts, rc, "claim_draft", raw, "bad_date", f"since 非法：{c['since']}")
    # 复核期不再入库：它是 since 的派生值。固定 +365 天还会把同一批建的判断
    # 挤到同一个月到期（同一批判断会撞在同一个月），造出一个永远还不完的队列。
    # 年龄由视图现算，用到时再判断，不排队、不催、不续命。
    c.pop("review_after", None)

    norm = "".join(str(c["text"]).split())
    for old in claims:
        if old.get("status") == "active" and "".join(str(old.get("text", "")).split()) == norm:
            return draft(drafts, rc, "claim_draft", raw, "duplicate", f"和 {old['id']} 重复")

    sup = c.get("supersedes") or []
    if sup:
        alive = {x["id"] for x in claims if x.get("status") == "active"}
        miss = [x for x in sup if x not in alive]
        if miss:
            return draft(drafts, rc, "claim_draft", raw, "dangling_supersedes",
                         f"要推翻的判断不存在或非活跃：{miss}")
        if not c.get("reason"):
            return draft(drafts, rc, "claim_draft", raw, "missing_reason",
                         "推翻了旧判断却没写 reason")
        open_t = [t["id"] for t in (tensions or [])
                  if t.get("status") == "open" and set(t.get("between") or []) & set(sup)]
        if open_t:
            return draft(drafts, rc, "claim_draft", raw, "in_tension",
                         f"{open_t[0]} 还挂着要取代的判断 —— 先 tension.py --close {open_t[0]} --reason …")

    # 新增要给理由（病历 Problem List 的做法：加一条，要么解决旧条目，要么说明为什么不能）。
    # 落库之前先比一次"同域最像的那条"——高度重合说明它不是一条新的，是旧的一条的另一种说法。
    twin = (0.0, "", "")
    for o in claims:
        if o.get("status") != "active" or o.get("topic") != c.get("topic"):
            continue
        s = check_mod.contain(c.get("text", ""), o.get("text", ""))
        if s > twin[0]:
            twin = (s, o["id"], o.get("text", ""))
    if twin[0] >= 0.92:
        return draft(drafts, rc, "claim_draft", raw, "duplicate_claim",
                     f"和 {twin[1]}「{clean(twin[2], 24)}」重合 {int(twin[0] * 100)}%"
                     f" —— 合进那条，或者把区别说清楚")

    c["id"] = next_id(claims, "c", 3)
    c["status"] = "active"
    c.setdefault("ambiguous", False)
    c["supersedes"] = sup
    claims.append(c)
    old_basis = []
    for x in sup:
        for old in claims:
            if old["id"] == x:
                old["status"] = "superseded"
                old_basis += old.get("basis", [])
    if ref:
        refmap[ref] = c["id"]
    note = clean(c["text"])
    orph = orphans_of(claims, fid_map, old_basis) if old_basis else []
    if orph:
        note += f"（被取代的依据里，有 {len(orph)} 条没人用了：{'、'.join(orph[:3])}）"
    if bad:
        note += f"（剔除了悬空依据 {','.join(bad)}）"
    if downgraded:
        note += "（confirmed 自报不被接受：要 --confirm 事件）"
    rc.add("✅", c["id"], "新增判断", note)

    if 0.75 <= twin[0] < 0.92:
        rc.add("⚠️", c["id"], f"和 {twin[1]} 像 {int(twin[0] * 100)}%",
               "两条都在说这件事吗？是的话合并，不是就把区别写进 context",
               claim=False)

    # 话别说得比证据远：依据全来自"干活现场"（kind=行为），判断却落在生活域
    if c.get("topic") not in ("work", "methods", "interface", "misc"):
        kinds = [fid_map[b].get("kind") for b in c.get("basis", []) if b in fid_map]
        if kinds and all(k == "行为" for k in kinds):
            rc.add("⚠️", c["id"], f"落在 {c['topic']}，但依据全来自干活现场",
                   "这条是不是写高了？拿掉这个场合，还有别的场合也符合吗",
                   claim=False)


def detach_basis(specs, claims, facts, rc):
    """撤掉一条接错的依据：c-055:f-0093,f-0156。

    什么时候用：判断引用的那条"事实"其实是我自己写的结论（kind=AI观察），
    真正的原话在库里躺着。这不是"清理"，是**把线接到该接的地方**。

    铁底：撤完必须还剩 ≥1 条依据。撤空了就不是修线，是把判断掏空——
    那种情况该走 supersede（换一条新判断），不能靠连删。
    """
    by_c = {c["id"]: c for c in claims if c.get("status") == "active"}
    by_f = {f["id"]: f for f in facts}
    for spec in specs:
        if ":" not in spec:
            rc.count_input()
            rc.add("➖", spec, "格式不对", "应为 c-055:f-0093,f-0156")
            continue
        cid, ids = spec.split(":", 1)
        c = by_c.get(cid)
        if not c:
            rc.count_input()
            rc.add("➖", cid, "不存在或非活跃")
            continue
        want = [x.strip() for x in ids.split(",") if x.strip()]
        gone = [i for i in want if i in c.get("basis", [])]
        miss = [i for i in want if i not in c.get("basis", [])]
        if miss:
            rc.count_input()
            rc.add("➖", cid, "这些本来就不在它的依据里", f"{miss}")
        if not gone:
            continue
        if len(c["basis"]) - len(gone) < 1:
            rc.count_input()
            rc.add("➖", cid, "撤完一条依据都不剩", "这不是接错线，是这条判断没证据——该 supersede 或 retire")
            continue
        for i in gone:
            rc.count_input()          # 一条依据一条账，否则对账单会算出负数
            c["basis"].remove(i)
            f = by_f.get(i, {})
            tail = "（那条其实是我写的结论）" if f.get("kind") == "AI观察" else ""
            rc.add("✂️", cid, f"撤依据 {i}", f"{clean(f.get('text', ''), 40)}{tail}")


def rewire_claims(rows, claims, facts, rc):
    """换依据：一条判断原来指向"我自己的结论"，改成指向他的原话。

    为什么必须是一个动作，而不是 --attach + --detach：
    `context` 里引用的是依据内容，换了依据，context 往往也引错了对象——
    所以"换线"在数据里是**依据 + context 一起换**的三件事。拆开做，
    中间那个状态是坏的（context 指向一条已经被撤掉的依据）。

    rows：[{"id":"c-055","add":[…],"del":[…],"context":"新解释（可省）","reason":"为什么换"}]
    """
    by_c = {c["id"]: c for c in claims if c.get("status") == "active"}
    by_f = {f["id"]: f for f in facts}
    for row in rows:
        cid = row.get("id")
        rc.count_input()
        c = by_c.get(cid)
        if not c:
            rc.add("➖", cid, "不存在或非活跃")
            continue
        add = [x for x in (row.get("add") or [])]
        dele = [x for x in (row.get("del") or [])]
        bad = [x for x in add if x not in by_f or by_f[x].get("excluded")]
        if bad:
            rc.add("➖", cid, "要接的原话不存在或已被排除", f"{bad}")
            continue
        newb = [b for b in c["basis"] if b not in dele]
        newb += [x for x in add if x not in newb]
        if not newb:
            rc.add("➖", cid, "换完一条依据都不剩", "该 supersede 或 retire，不是换线")
            continue
        ctx = row.get("context")
        if ctx is not None:
            if not (30 <= len(ctx) <= 150):
                rc.add("➖", cid, f"新 context {len(ctx)} 字", "应 30–150")
                continue
            src = "".join((by_f[b].get("text") or "") for b in newb)
            if not has_fragment(ctx, src):
                rc.add("➖", cid, "新 context 没引用任何新依据的原话", "必须含连续 8 字")
                continue
        c["basis"] = newb
        if ctx is not None:
            c["context"] = ctx
        c["rewired_at"] = today_iso()
        c["rewired_reason"] = row.get("reason") or ""
        rc.add("🔄", cid, f"换依据 −{len(dele)} +{len(add)}",
               f"{row.get('reason', '')}")


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
SECRETS = [(EMAIL_RE, "[邮箱已遮]"), (PHONE_RE, "[手机号已遮]")]


def redact_transcript(ids, reason, transcript, rc):
    """把一条原话里的邮箱/手机号遮掉，行留着。

    为什么不删整行：删了就没法再证明"你说的每句都处理过"——那是对账单的地基。
    遮掉敏感串、留下 `redacted_at` 和原因，是"我处理过它"的痕迹，比删更诚实。
    """
    by = {t.get("id"): t for t in transcript}
    for i in ids:
        rc.count_input()
        t = by.get(i)
        if not t:
            rc.add("➖", i, "找不到这条原话")
            continue
        text = t.get("text", "")
        hits = sum(len(rx.findall(text)) for rx, _ in SECRETS)
        if not hits:
            rc.add("➖", i, "这条里没有可遮的邮箱或手机号")
            continue
        for rx, mask in SECRETS:
            t["text"] = rx.sub(mask, t["text"])
        t["redacted_at"] = today_iso()
        t["redact_reason"] = reason
        t["redact_hits"] = hits
        rc.add("🕶️", i, f"遮住 {hits} 处", reason)


def take_tension(raw, tensions, claims, rc, drafts, refmap):
    t = dict(raw)
    rc.count_input()
    ids = [refmap.get(x, x) for x in t.get("between", [])]
    alive = {c["id"] for c in claims if c.get("status") == "active"}
    if len(ids) != 2 or any(x not in alive for x in ids):
        return draft(drafts, rc, "tension_draft", raw, "schema", f"between 无效：{ids}")
    if not (5 <= len(t.get("desc", "")) <= 100):
        return draft(drafts, rc, "tension_draft", raw, "desc_length", "desc 应 5–100 字")
    if any(set(x.get("between", [])) == set(ids) for x in tensions):
        return draft(drafts, rc, "tension_draft", raw, "duplicate", "这对已经记录过")
    t["id"] = next_id(tensions, "t", 3)
    t["between"] = ids
    t["since"] = today_iso()
    t["status"] = "open"
    tensions.append(t)
    rc.add("✅", t["id"], "新增张力", clean(t["desc"]))


def exclude_facts(ids, action, reason, facts, rc):
    by = {f["id"]: f for f in facts}
    unknown = []
    for i in ids:
        rc.count_input()
        f = by.get(i)
        if not f:
            unknown.append(i)
            rc.add("➖", i, "找不到这条事实")
            continue
        if f.get("excluded"):
            rc.add("➖", i, "已是排除状态")
            continue
        f["excluded"] = {"action": action, "reason": reason, "date": today_iso()}
        rc.add("🚫", i, f"标 {action}", clean(f.get("text", "")))
    return unknown


def attach_basis(specs, claims, facts, rc):
    """把已有事实挂到已有判断上：这一步以前绕过闸门，现在也走 commit。"""
    by_c = {c["id"]: c for c in claims if c.get("status") == "active"}
    by_f = {f["id"]: f for f in facts if not f.get("excluded")}
    delta = {}          # topic -> 新加的依据条数
    for spec in specs:
        rc.count_input()
        if ":" not in spec:
            rc.add("➖", spec, "格式不对", "应为 c-023:f-0001,f-0002")
            continue
        cid, ids = spec.split(":", 1)
        c = by_c.get(cid)
        if not c:
            rc.add("➖", cid, "不存在或非活跃")
            continue
        add, bad = [], []
        for i in (x.strip() for x in ids.split(",") if x.strip()):
            if i not in by_f:
                bad.append(i)
            elif i not in c["basis"]:
                add.append(i)
        if bad:
            rc.add("➖", cid, "依据无效", f"{bad}（不存在或已排除）")
            continue
        if not add:
            rc.add("➖", cid, "没有新依据")
            continue
        c["basis"] += add
        delta[c["topic"]] = delta.get(c["topic"], 0) + len(add)
        rc.add("✅", cid, f"加依据 +{len(add)}", f"共 {len(c['basis'])} 条")
    return delta


def orphans_of(claims, fid_map, ids):
    """ids 里，有哪些事实在动作之后不再被任何在役判断引用。"""
    cited = {b for c in claims if c.get("status") == "active"
             for b in (c.get("basis") or [])}
    live = {k for k, v in fid_map.items() if not v.get("excluded")}
    return [i for i in ids if i in live and i not in cited]


def retire_claim(cid, reason, claims, tensions, rc, fid_map):
    c = next((x for x in claims if x["id"] == cid), None)
    if not c or c.get("status") != "active":
        rc.add("➖", cid, "不是活跃判断，没法退役")
        return
    open_t = [t["id"] for t in tensions
              if t.get("status") == "open" and cid in (t.get("between") or [])]
    if open_t:
        rc.add("➖", cid, "还在张力的两端", f"先 tension.py --close {open_t[0]} --reason …")
        return
    c["status"] = "retired"
    c["retired_reason"] = reason
    c["retired_at"] = today_iso()
    orph = orphans_of(claims, fid_map, c.get("basis", []))
    note = reason
    if orph:
        note += f"（让 {len(orph)} 条依据没人用了：{'、'.join(orph[:3])}）"
    rc.add("🚪", cid, "退役", note)


def unretire_claim(cid, claims, rc):
    c = next((x for x in claims if x["id"] == cid), None)
    if not c or c.get("status") != "retired":
        rc.add("➖", cid, "不是退役状态")
        return
    c["status"] = "active"
    c.pop("retired_reason", None)
    c.pop("retired_at", None)
    rc.add("🚪", cid, "放回来（重新在役）")


def append_event(events, kind, target, reason, actor="user"):
    """每一次「人的决定」都留一条不可追改的事件。"""
    events.append({"id": next_id(events, "e", 4), "kind": kind,
                   "target": target, "actor": actor, "at": today_iso(),
                   "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "reason": reason})


def confirm_claim(cid, reason, actor, claims, events, rc):
    c = next((x for x in claims if x["id"] == cid and x.get("status") == "active"), None)
    if not c:
        rc.add("➖", cid, "不是在役判断，没法确认")
        return
    c["confidence"] = "confirmed"
    append_event(events, "confirm", cid, reason, actor)
    rc.add("✅", cid, "本人确认", f"{actor}：{reason}")


def veto_claim(cid, reason, actor, claims, events, rc):
    c = next((x for x in claims if x["id"] == cid and x.get("status") == "active"), None)
    if not c:
        rc.add("➖", cid, "不是在役判断，没法否决")
        return
    c["confidence"] = "unconfirmed"
    append_event(events, "veto", cid, reason, actor)
    rc.add("✅", cid, "本人否决（转为未确认）", f"{actor}：{reason}")


def ignore_said(specs, reason, actor, transcript, events, rc):
    by = {t.get("id"): t for t in transcript}
    for sid in specs:
        rc.count_input()
        t = by.get(sid)
        if not t:
            rc.add("➖", sid, "找不到这条原话")
            continue
        if t.get("ignored"):
            rc.add("➖", sid, "已经标过不记")
            continue
        t["ignored"] = reason
        t["ignored_at"] = today_iso()
        t["ignored_by"] = actor
        append_event(events, "ignore", sid, reason, actor)
        rc.add("✅", sid, "显式标为不记", reason)


def capture_said(rows, transcript, events, round_no, actor):
    """只登记原话。这里不写 facts / claims，也不接受「这句不记」的决定。

    限度：同一个 agent 分两次调用，理论上仍可以第一步就少交一句。
    所以这一刀不宣称「独立记录」，只保证交上来的每句都有可见终态、
    且不能在同一次调用里被顺手抹掉。
    """
    for it in rows:
        if not isinstance(it, str):
            raise ValueError("capture 只收纯文本原话；{'ignored': …} 请走 --ignore")
    new = []
    for text in rows:
        if not str(text).strip():
            continue
        new.append({"id": next_id(transcript + new, "s", 4), "date": today_iso(),
                    "round": round_no, "text": str(text)})
    for row in new:
        append_event(events, "capture", row["id"], "本轮原话捕获", actor)
    return new


def migrate_legacy(claims, transcript, events, migrations, rc):
    """把迁移前的自报值固化成豁免名单。幂等：缺什么补什么。"""
    short = [t["id"] for t in transcript
             if len("".join(str(t.get("text", "")).split())) < FRAG
             and not t.get("ignored")]
    legacy = next((m for m in migrations if m.get("kind") == "legacy"), None)
    if legacy is not None:
        rc.count_input()
        if "short_said" not in legacy:
            legacy["short_said"] = short
            reason = "迁移前的短句原话固化进豁免名单：新规则不按字数免检，老数据不追诉"
            append_event(events, "migration", "legacy-short", reason, "system")
            rc.add("✅", "migrate", "补齐短句豁免名单", f"固化 {len(short)} 句短原话")
        else:
            rc.add("➖", "migrate", "已经迁过，不重复写")
        return
    confirmed = [c["id"] for c in claims
                 if c.get("status") == "active" and c.get("confidence") == "confirmed"]
    ignored = [t["id"] for t in transcript if t.get("ignored")]
    dropped = 0
    for c in claims:
        if "review_after" in c:
            c.pop("review_after")
            dropped += 1
        for k in ("renewed_at", "renewed_reason"):
            c.pop(k, None)
    row = {"kind": "legacy", "at": today_iso(),
           "confirmed": confirmed, "ignored": ignored, "short_said": short,
           "reason": "迁移前历史：confirmed / ignored 由提交方自报，无独立事件；此后新写入必须有事件"}
    migrations.append(row)
    append_event(events, "migration", "legacy", row["reason"], "system")
    rc.count_input()
    rc.add("✅", "migrate", "存量迁移",
           f"固化 {len(confirmed)} 条 confirmed / {len(ignored)} 句 ignored / "
           f"{len(short)} 句短原话；清掉 {dropped} 个 review_after")


def retopic_claim(spec, reason, claims, rc):
    """改归档：c-038:work。只改 topic 一个字段，不制造"曾经认为"的版本噪音。

    为什么需要它：`topic` 是完整度的分母，归档写错就是数字错，而 check 抓不住——
    它只验"topic 合法"，不验"topic 对不对"。supersede 会把同一句话写两遍，
    retire 会让还在用的判断进"已退役"，两个都不对，所以单独一个动作。
    """
    cid, sep, topic = spec.partition(":")
    cid, topic = cid.strip(), topic.strip()
    if not sep or not cid or not topic:
        rc.add("➖", spec, "格式应为 c-038:work")
        return
    if not check_mod.valid_topic(topic):
        rc.add("➖", cid, "域非法", topic)
        return
    c = next((x for x in claims if x["id"] == cid), None)
    if not c:
        rc.add("➖", cid, "找不到这条判断")
        return
    old = c.get("topic")
    if old == topic:
        rc.add("➖", cid, "已经在这个域里了", topic)
        return
    c["topic"] = topic
    c["retopied_at"] = today_iso()
    c["retopied_reason"] = reason
    rc.add("📁", cid, f"{old} → {topic}", f"{clean(c.get('text', ''))}｜{reason}")


def narrow_claim(cid, text, context, reason, claims, fid_map, narrows, rc):
    """只收窄措辞：改 active 判断的 text（可选连 context），其余一概不动。

    判断不是证据，是当时的一个解释。改掉它不丢信息（原话还在），但走 supersedes
    会凭空造出一条新判断、「曾经认为」一节、依据掉回消化债、白白多一条历史——
    改两个字要动七处，结果就是没人改。所以单独一个动作：不动 id / status / basis /
    topic / type / since，只把旧文与理由记进 narrows.jsonl（人的决定），
    由 build 渲染进 REVISIONS.md。理由必填 + me/ 每轮 git 留 diff，防静默修改。

    context 里常常也写着说宽了的那句，所以允许一起换；但换完仍要过原来那道校验：
    30–150 字，且必须含依据内容的连续 8 字。
    """
    c = next((x for x in claims if x["id"] == cid), None)
    if not c:
        rc.add("➖", cid, "找不到这条判断")
        return
    if c.get("status") != "active":
        rc.add("➖", cid, "不是在役判断，不能收窄", str(c.get("status")))
        return
    text = (text or "").strip()
    context = (context or "").strip()
    old = c.get("text", "")
    if not text:
        rc.add("➖", cid, "没给新措辞（要 --text）")
        return
    if text == old and (not context or context == c.get("context", "")):
        rc.add("➖", cid, "和原来一样，没什么可改")
        return
    if len(text) > check_mod.TEXT_MAX:
        rc.add("➖", cid, f"新措辞超 {check_mod.TEXT_MAX} 字（{len(text)}）")
        return
    if context:
        if not (check_mod.CTX_MIN <= len(context) <= check_mod.CTX_MAX):
            rc.add("➖", cid, f"新 context {len(context)} 字",
                   f"应 {check_mod.CTX_MIN}–{check_mod.CTX_MAX}")
            return
        srcs = [fid_map[b]["text"] for b in c.get("basis", [])
                if b in fid_map and not fid_map[b].get("excluded")]
        if not any(has_fragment(context, s) for s in srcs):
            rc.add("➖", cid, "新 context 没含依据内容片段（8 字）")
            return
    row = {"id": cid, "date": today_iso(), "old": old,
           "new": text, "reason": reason}
    if context:
        row["old_context"] = c.get("context", "")
        row["new_context"] = context
    narrows.append(row)
    c["text"] = text
    if context:
        c["context"] = context
    what = f"措辞收窄：{old} → {text}"
    if context:
        what += "（连说明一起换）"
    rc.add("✏️", cid, what, reason)


def update_state(round_no, topic):
    p = ME / "state.json"
    st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    st["schema_version"] = st.get("schema_version", "0.1")
    st["round"] = round_no
    if topic:
        st["current_topic"] = topic
    st.setdefault("current_topic", "identity")
    st["last_commit"] = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write(p, json.dumps(st, ensure_ascii=False, indent=2) + "\n")
    return st


def git_commit(msg):
    if not (ME / ".git").is_dir():
        return None
    ident = ["-c", "user.name=self-portrait", "-c", "user.email=portrait@localhost"]
    try:
        subprocess.run(["git"] + ident + ["add", "-A"], cwd=ME, check=True,
                       capture_output=True)
        r = subprocess.run(["git"] + ident + ["commit", "-q", "-m", msg], cwd=ME,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return "无变化"
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ME,
                           capture_output=True, text=True)
        return h.stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        print(f"⚠️  git 提交失败（数据已写入，历史没记上）：{e}")
        return None


def round_stats(facts_before, facts, claims_before, claims, attach_delta, said_n, round_no):
    judged, states, basis = {}, {}, {}
    for c in claims[claims_before:]:
        dom = c["topic"]
        if c.get("type") in JUDGED:
            judged[dom] = judged.get(dom, 0) + 1
        elif c.get("type") == "state":
            states[dom] = states.get(dom, 0) + 1
        basis[dom] = basis.get(dom, 0) + len(c.get("basis", []))
    for dom, n in attach_delta.items():
        basis[dom] = basis.get(dom, 0) + n
    by_topic = {}
    for f in facts[facts_before:]:
        by_topic[f["topic"]] = by_topic.get(f["topic"], 0) + 1
    return {"round": round_no, "date": today_iso(), "facts": by_topic,
            "judged": judged, "states": states, "basis": basis, "said": said_n}


def spinning_domains(rounds, window=3):
    """连续 N 轮聊到某个域，但判断和依据都没增加。"""
    tail = rounds[-window:]
    if len(tail) < window:
        return []
    out = []
    for dom in set().union(*[set(r.get("facts", {})) | set(r.get("judged", {})) |
                             set(r.get("basis", {})) for r in tail]):
        if not any(r.get("facts", {}).get(dom) or r.get("judged", {}).get(dom)
                   or r.get("basis", {}).get(dom) for r in tail):
            continue
        if sum(r.get("judged", {}).get(dom, 0) for r in tail) == 0 and \
           sum(r.get("basis", {}).get(dom, 0) for r in tail) == 0:
            out.append(dom)
    return sorted(out)


def synthesis_stale(claims):
    p = ME / "SYNTHESIS.md"
    if not p.exists():
        return False
    m = FP_RE.search(p.read_text(encoding="utf-8"))
    if not m:
        return True
    return m.group(1) != build_mod.syn_fingerprint(claims)


# ---------------------------------------------------------------- 动作表
# 加一种收场动作：在 ARG_SPECS 里加一行（写明要不要理由）+ 写一个 act_xxx。
# 参数定义、理由校验、执行顺序都由这张表驱动，main() 不再逐个 if。
# ACTIONS 的顺序就是执行顺序：facts 要在 exclude 前，claims 要在 attach 后。


def act_domain(args, e, rc):
    dom = args.skip or args.defer
    kind = "skipped" if args.skip else "deferred"
    if not check_mod.valid_topic(dom):
        print(f"❌ 不认识的域：{dom}")
        return 1
    rc.count_input()
    rows = [x for x in e["topics"] if x.get("domain") != dom]
    row = {"domain": dom, "state": kind, "reason": args.reason, "since": today_iso()}
    if kind == "deferred":
        row["until"] = add_days(today_iso(), args.days)
    rows.append(row)
    e["topics"] = rows
    rc.add("✅", dom, "跳过" if kind == "skipped" else f"暂缓 {args.days} 天", args.reason)


def act_narrow(args, e, rc):
    rc.count_input()
    narrow_claim(args.narrow.strip(), args.text, args.context, args.reason,
                 e["claims"], e["fid_map"], e["narrows"], rc)


def act_retire(args, e, rc):
    for cid in id_list(args.retire):
        rc.count_input()
        retire_claim(cid, args.reason, e["claims"], e["tensions"], rc, e["fid_map"])


def act_unretire(args, e, rc):
    for cid in id_list(args.unretire):
        rc.count_input()
        unretire_claim(cid, e["claims"], rc)


def act_redact(args, e, rc):
    for sid in id_list(args.redact):
        redact_transcript([sid], args.reason, e["transcript"], rc)


def act_retopic(args, e, rc):
    for spec in id_list(args.retopic):
        rc.count_input()
        retopic_claim(spec, args.reason, e["claims"], rc)


def act_ignore(args, e, rc):
    ignore_said(id_list(args.ignore), args.reason, args.by,
                e["transcript"], e["events"], rc)


def act_confirm(args, e, rc):
    rc.count_input()
    confirm_claim(args.confirm.strip(), args.reason, args.by,
                  e["claims"], e["events"], rc)


def act_veto(args, e, rc):
    rc.count_input()
    veto_claim(args.veto.strip(), args.reason, args.by,
               e["claims"], e["events"], rc)


def act_migrate(args, e, rc):
    migrate_legacy(e["claims"], e["transcript"], e["events"], e["migrations"], rc)


def act_facts(args, e, rc):
    for raw in load_any(args.facts):
        take_fact(raw, e["facts"], rc, e["drafts"], e["refmap"])
    for f in e["facts"]:
        e["fid_map"][f["id"]] = f


def act_exclude(args, e, rc):
    unknown = exclude_facts([x.strip() for x in args.exclude.split(",") if x.strip()],
                            args.action, args.reason, e["facts"], rc)
    if unknown:
        print(f"❌ 找不到这些 fact：{unknown} —— 什么都没写")
        return 1


def act_attach(args, e, rc):
    e["attach_delta"] = attach_basis(args.attach, e["claims"], e["facts"], rc)


def act_detach(args, e, rc):
    detach_basis(args.detach, e["claims"], e["facts"], rc)


def act_rewire(args, e, rc):
    rewire_claims(load_any(args.rewire), e["claims"], e["facts"], rc)


def act_claims(args, e, rc):
    for raw in load_any(args.claims):
        take_claim(raw, e["claims"], e["fid_map"], rc, e["drafts"],
                   e["refmap"], e["tensions"])


def act_tensions(args, e, rc):
    for raw in load_any(args.tensions):
        take_tension(raw, e["tensions"], e["claims"], rc, e["drafts"], e["refmap"])


def action_trigger(spec, args):
    """默认按同名参数取值；skip/defer 这种共用处理函数的可以自定义。"""
    if spec.get("trigger"):
        return spec["trigger"](args)
    return getattr(args, spec["arg"].replace("-", "_"), None)


ARG_SPECS = [
    {"arg": "said", "help": "兼容路径：纯文本原话数组"},
    {"arg": "capture", "help": "只登记原话：said.json（不接受 ignored 字段）"},
    {"arg": "drafts", "opts": {"action": "store_true"}, "help": "看草稿区"},
    {"arg": "drop", "label": "关闭草稿", "reason": True, "help": "关闭某条草稿"},

    {"arg": "skip", "label": "跳过域", "reason": True, "handler": act_domain,
     "trigger": lambda a: a.skip or a.defer, "help": "永久跳过某个域"},
    {"arg": "defer", "label": "暂缓域", "reason": True, "help": "暂时不想聊某个域"},
    {"arg": "narrow", "label": "收窄措辞", "reason": True, "handler": act_narrow,
     "help": "收窄措辞：c-069（要 --text 和 --reason）"},
    {"arg": "text", "help": "--narrow 的新措辞"},
    {"arg": "context", "help": "--narrow 可同时换说明"},
    {"arg": "retire", "label": "退役判断", "reason": True, "handler": act_retire,
     "help": "退役判断，逗号分隔可批量"},
    {"arg": "unretire", "handler": act_unretire, "help": "把退役的判断放回来"},
    {"arg": "redact", "label": "遮敏感串", "reason": True, "handler": act_redact,
     "help": "遮住原话里的联系方式"},
    {"arg": "retopic", "label": "改归档", "reason": True, "handler": act_retopic,
     "help": "改归档：c-038:work"},
    {"arg": "ignore", "label": "忽略原话", "reason": True, "handler": act_ignore,
     "help": "显式标为不记：s-0120"},
    {"arg": "confirm", "label": "确认判断", "reason": True, "handler": act_confirm,
     "help": "本人确认判断"},
    {"arg": "veto", "label": "否决判断", "reason": True, "handler": act_veto,
     "help": "本人否决判断"},
    {"arg": "migrate", "opts": {"action": "store_true"}, "handler": act_migrate,
     "help": "一次性存量迁移"},

    {"arg": "facts", "handler": act_facts, "help": "本批事实 JSON"},
    {"arg": "exclude", "label": "排除事实", "reason": True, "handler": act_exclude,
     "help": "逗号分隔的 fact id"},
    {"arg": "attach", "opts": {"action": "append", "default": []},
     "handler": act_attach, "help": "c-023:f-0001，可重复"},
    {"arg": "detach", "opts": {"action": "append", "default": []},
     "handler": act_detach, "help": "撤掉接错的依据"},
    {"arg": "rewire", "handler": act_rewire, "help": "换依据 + 换 context"},
    {"arg": "claims", "handler": act_claims, "help": "本批判断 JSON"},
    {"arg": "tensions", "handler": act_tensions, "help": "本批张力 JSON"},

    {"arg": "from-capture", "opts": {"type": int, "metavar": "ROUND"},
     "help": "本轮分拣对应第几轮 capture"},
    {"arg": "days", "opts": {"type": int, "default": 30}, "help": "暂缓天数"},
    {"arg": "by", "opts": {"default": "user"}, "help": "决定主体"},
    {"arg": "action", "opts": {"choices": ["noise", "archived"], "default": "noise"},
     "help": "排除动作"},
    {"arg": "reason", "help": "人的决定要留的理由"},
    {"arg": "round", "opts": {"type": int}, "help": "指定轮次"},
    {"arg": "topic", "help": "当前域"},
    {"arg": "dry", "opts": {"action": "store_true"}, "help": "只预演，不写"},
    {"arg": "no-build", "opts": {"action": "store_true"}, "help": "不重建视图"},
    {"arg": "no-git", "opts": {"action": "store_true"}, "help": "不提交内层仓"},
]

ACTIONS = [a for a in ARG_SPECS if a.get("handler")]
REASON_ARGS = {a["arg"]: a["label"] for a in ARG_SPECS if a.get("reason")}


def main():
    ap = argparse.ArgumentParser(add_help=True)
    for spec in ARG_SPECS:
        ap.add_argument("--" + spec["arg"], help=spec.get("help", ""),
                        **spec.get("opts", {}))
    args = ap.parse_args()

    if not (ME / "claims.jsonl").exists():
        print("me/ 还没初始化，先跑 python scripts/init.py")
        return 1

    facts, claims = load("facts.jsonl"), load("claims.jsonl")
    tensions, drafts = load("tensions.jsonl"), load("drafts.jsonl")
    transcript, rounds = load("transcript.jsonl"), load("rounds.jsonl")
    topics = load("topics.jsonl")
    narrows = load("narrows.jsonl")
    events = load("events.jsonl")
    migrations = load("migrations.jsonl")

    if args.drafts:
        opened = [d for d in drafts if d.get("status", "open") == "open"]
        if not opened:
            print("✅ 草稿区是空的")
            return 0
        print(f"草稿区 {len(opened)} 条待处理：\n")
        for d in opened:
            print(f"{d['id']} [{d.get('reason')}] {d.get('note', '')}")
            print(f"   payload: {json.dumps(d.get('payload'), ensure_ascii=False)[:200]}")
        print("\n修好后重跑 commit.py；放弃就 --drop <id> --reason \"…\"")
        return 0

    if args.capture:
        st0 = json.loads((ME / "state.json").read_text(encoding="utf-8")) \
            if (ME / "state.json").exists() else {}
        cap_round = int(st0.get("round", 0)) + 1
        try:
            new = capture_said(load_any(args.capture), transcript, events,
                               cap_round, args.by)
        except ValueError as e:
            print(f"❌ {e}")
            return 1
        dump_jsonl(ME / "transcript.jsonl", transcript + new)
        dump_jsonl(ME / "events.jsonl", events)
        print(f"📥 第 {cap_round} 轮原话已登记 {len(new)} 句（本阶段不写事实/判断）")
        for s in new:
            print(f"   {s['id']}  {clean(s['text'], 46)}")
        print("下一步：--facts/--claims 加 --from-capture <轮次> 完成分拣；"
              "不记的句子走 --ignore <id> --reason \"…\"")
        if not args.no_git:
            rev = git_commit(f"第 {cap_round} 轮：捕获 {len(new)} 句原话")
            if rev:
                print(f"git：me/ 已提交 {rev}")
        return 0

    need_reason = [label for arg, label in REASON_ARGS.items()
                   if getattr(args, arg, None)]
    if need_reason and not args.reason:
        print("❌ 这些动作必须带 --reason（人的决定要留痕）：" + "、".join(need_reason))
        return 1

    if args.drop:
        hit = next((d for d in drafts if d["id"] == args.drop), None)
        if not hit:
            print(f"没找到草稿 {args.drop}")
            return 1
        hit["status"] = "closed"
        hit["closed_reason"] = args.reason
        hit["closed_at"] = today_iso()
        dump_jsonl(ME / "drafts.jsonl", drafts)
        print(f"已关闭 {args.drop}：{args.reason}")
        return 0

    st_old = json.loads((ME / "state.json").read_text(encoding="utf-8")) \
        if (ME / "state.json").exists() else {}
    round_no = args.round if args.round is not None else int(st_old.get("round", 0)) + 1

    # 原话登记。--said 保留为兼容路径，但只收纯文本：
    # 「这句不记」不再能在同一次调用里顺手写掉，必须走 --ignore 显式动作。
    new_said = []
    for it in (load_any(args.said) if args.said else []):
        if not isinstance(it, str):
            print("❌ --said 只收纯文本原话；{'text':…, 'ignored':…} 已废弃。"
                  "先用 --capture 登记，再用 --ignore <id> --reason \"…\" 显式标为不记。")
            return 1
        if not it.strip():
            continue
        new_said.append({"id": next_id(transcript + new_said, "s", 4),
                         "date": today_iso(), "round": round_no, "text": it})
    for s in new_said:
        append_event(events, "capture", s["id"], "本轮原话登记", args.by)

    rc = Receipt()
    refmap = {}
    facts_before, claims_before = len(facts), len(claims)
    fid_map = {f["id"]: f for f in facts}

    e = {
        "facts": facts, "claims": claims, "tensions": tensions, "drafts": drafts,
        "transcript": transcript, "topics": topics, "narrows": narrows,
        "events": events, "migrations": migrations,
        "fid_map": fid_map, "refmap": refmap, "attach_delta": {},
    }
    for spec in ACTIONS:
        if not action_trigger(spec, args):
            continue
        ret = spec["handler"](args, e, rc)
        if ret:
            return ret
    topics = e["topics"]
    attach_delta = e["attach_delta"]

    # 整批预检：跨条的问题（超额、重复 id、取代状态），以及
    # confirmed / ignored 有没有对应的独立事件
    errors, warns = check_mod.validate(facts, claims, tensions, drafts,
                                       manifest=None, require_rendered=False,
                                       check_views=False, topics=topics,
                                       events=events, migrations=migrations,
                                       transcript=transcript)
    if errors:
        print("❌ 预检失败，什么都没写：")
        for e in errors:
            print(f"   {e}")
        return 1

    if args.dry:
        rc.print()
        print("（--dry：以上是预演，没有写入）")
        return 0

    # 决定不可静默：本轮交上来的每句原话都要有终态。
    # 终态只有两种：落成事实、被 --ignore 显式标为不记。
    # 不按字数免检：短回答也要有交代，否则漏记一句也报「0 句没落地」。
    fact_texts = [f["text"] for f in facts if not f.get("excluded")]
    ignored_ids = {e.get("target") for e in events if e.get("kind") == "ignore"}
    legacy_ignored = set()
    for m in migrations:
        legacy_ignored.update(m.get("ignored") or [])

    def is_ignored(s):
        return bool(s.get("ignored")) or s.get("id") in ignored_ids \
            or s.get("id") in legacy_ignored

    def unsettled(s):
        if is_ignored(s):
            return False
        return not covered_by(s.get("text", ""), fact_texts)

    target = list(new_said)
    if args.from_capture is not None:
        target += [s for s in transcript if s.get("round") == args.from_capture]
    missing = [s for s in target if unsettled(s)]
    if missing:
        print(f"❌ 本轮有 {len(missing)} 句原话没有终态，什么都没写：")
        for s in missing[:5]:
            print(f"   {s['id']}  {clean(s.get('text', ''), 40)}")
        print('   落成事实，或 --ignore <id> --reason "…"。')
        return 1

    cov = {"landed": 0, "ignored": 0, "missing": []}
    for s in target:
        if is_ignored(s):
            cov["ignored"] += 1
        elif covered_by(s["text"], fact_texts):
            cov["landed"] += 1
        else:
            cov["missing"].append(s)

    dump_jsonl(ME / "facts.jsonl", facts)
    dump_jsonl(ME / "claims.jsonl", claims)
    dump_jsonl(ME / "tensions.jsonl", tensions)
    dump_jsonl(ME / "drafts.jsonl", drafts)
    dump_jsonl(ME / "transcript.jsonl", transcript + new_said)
    dump_jsonl(ME / "topics.jsonl", topics)
    dump_jsonl(ME / "events.jsonl", events)
    dump_jsonl(ME / "migrations.jsonl", migrations)
    if args.narrow:
        dump_jsonl(ME / "narrows.jsonl", narrows)

    stat = round_stats(facts_before, facts, claims_before, claims,
                       attach_delta, len(target), round_no)
    dump_jsonl(ME / "rounds.jsonl", rounds + [stat])

    st = update_state(round_no, args.topic)

    lost = rc.print()
    if target:
        print(f"\n你说 {len(target)} 句 ｜ {cov['landed']} 句落地 ｜ "
              f"{cov['ignored']} 句标为无价值 ｜ "
              f"{len(cov['missing'])} 句没落地")
        for s in cov["missing"][:3]:
            print(f"   ⚠️ 没落地：{clean(s['text'], 40)}")
    elif args.facts or args.claims:
        print("\n⚠️  本轮没交 --said：无法核对「我说的话是不是都记了」")

    active_doms = [d for d in ("work", "methods", "goals", "values", "emotion",
                               "identity", "health", "relations", "leisure", "interface")
                   if stat["judged"].get(d) or stat["basis"].get(d) or stat["facts"].get(d)]
    if active_doms:
        parts = []
        for d in active_doms:
            j, b = stat["judged"].get(d, 0), stat["basis"].get(d, 0)
            tag = "在长" if j else ("在深化" if b else "原地")
            parts.append(f"{d} 判断+{j} 依据+{b}（{tag}）")
        print("📈 " + " ｜ ".join(parts))

    dr = check_mod.debt_report(facts, claims)
    if dr["count"]:
        print(f"\n📒 未消化 {dr['count']} 笔 ｜ 最老 {dr['age']} 天")
        if dr["tier"] == "retry":
            if dr["cands"]:
                print("   现在可能能挂上了：")
                for c in dr["cands"]:
                    print(f"   {c['id']}「{clean(c['text'], 20)}」重合 {c['shared']}"
                          f" → --attach {c['id']}:{dr['oldest']['id']}")
            else:
                print("   还没找到能挂的判断")
        elif dr["tier"] == "ring":
            o = dr["oldest"]
            print(f"🔔 {o['id']}「{clean(o['text'], 30)}」{dr['age']} 天没家")
            print("   三选一：--attach 挂上去 ｜ 写一条新判断 ｜ --exclude 标废")

    print(f"state：第 {st['round']} 轮 · 当前域 {st['current_topic']}")

    if not args.no_build:
        print("")
        if build_mod.main() != 0:
            print("❌ build 失败")
            return 1
        code = check_mod.main()
    else:
        code = 0

    spin = spinning_domains(rounds + [stat])
    for d in spin:
        print(f"❓ {d} 最近 3 轮有聊到，但判断和依据都没增加 —— "
              f"是在重复已经记过的，还是这块差不多了？")

    if synthesis_stale(claims):
        print("⚠️  综述已过期（判断变过）—— 要重写就跑 synth.py --worksheet，写完 --stamp")

    if not args.no_git:
        added = sum(1 for r in rc.rows if r[0] == "✅")
        excluded = sum(1 for r in rc.rows if r[0] == "🚫")
        drafted = sum(1 for r in rc.rows if r[0] == "🔶")
        msg = f"第 {st['round']} 轮：{added} 条入库，{excluded} 条排除，{drafted} 条进草稿"
        rev = git_commit(msg)
        if rev:
            print(f"git：me/ 已提交 {rev}")

    return 0 if lost == 0 and code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


