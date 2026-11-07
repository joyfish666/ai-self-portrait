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
    python scripts/commit.py --due                 # 复核期到了的，今天先摆 3 条
    python scripts/commit.py --due 5               # 摆 5 条
    python scripts/commit.py --renew c-013,c-014 --reason "还是一样"   # 一批
    python scripts/commit.py --dry --facts ...     # 只预演，不写

输入格式
    原话：["我上个月报了个班，去了两次就没去了", {"text":"嗯", "ignored":"语气词"}]
    事实：{"ref":"a","date":"…","kind":"…","asked":"…","text":"…","topic":"…","source":"…"}
    判断：{"topic":"…","type":"…","text":"…","context":"…","source":"…",
           "confidence":"…","since":"…","basis":["f-0012","a"],"supersedes":[],"reason":null}
    矛盾：{"between":["c-0100","c-0098"],"desc":"…"}

闸门（不合格就不进正式层）
    - 校验不过 → 进 me/drafts.jsonl，不丢
    - 超上限（判断 60 / 状态 40 / 张力 10）→ 整批拒绝，什么都不写
"""
import argparse
import json
import re
import subprocess
import sys
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
from store import (add_days, age_days, atomic_write, covered_by, dump_jsonl,  # noqa: E402
                   has_fragment, load, load_any, next_id, today_iso, to_date)

JUDGED = ("disposition", "inference")
TYPES = {"constant", "state", "disposition", "inference"}
CONFS = {"confirmed", "inferred", "unsure", "unconfirmed"}
KINDS = {"自述", "行为", "他人转述", "AI观察"}
SOURCES = {"user", "ai", "external"}
REVIEW_DAYS = {"state": 90, "disposition": 365}
FP_RE = re.compile(r"<!--\s*fp:([0-9a-f]{8})\s*-->")


class Receipt:
    def __init__(self):
        self.rows = []
        self.inputs = 0

    def add(self, icon, ident, what, detail=""):
        self.rows.append((icon, ident, what, detail))

    def count_input(self, n=1):
        self.inputs += n

    def print(self):
        stored = sum(1 for r in self.rows if r[0] == "✅")
        excluded = sum(1 for r in self.rows if r[0] == "🚫")
        doors = sum(1 for r in self.rows if r[0] == "🚪")
        retopied = sum(1 for r in self.rows if r[0] == "📁")
        drafted = sum(1 for r in self.rows if r[0] == "🔶")
        skipped = sum(1 for r in self.rows if r[0] == "➖")
        lost = self.inputs - stored - excluded - doors - retopied - drafted - skipped
        print(f"\n本轮对账（输入 {self.inputs} 条）")
        for icon, ident, what, detail in self.rows:
            tail = f"  {detail}" if detail else ""
            print(f"{icon} {ident} {what}{tail}")
        parts = [f"{stored} 入库", f"{excluded} 排除"]
        if doors:
            parts.append(f"{doors} 退役/续期")
        if retopied:
            parts.append(f"{retopied} 改归档")
        parts += [f"{drafted} 进草稿", f"{skipped} 跳过", f"{lost} 丢失"]
        print("\n📊 " + " ｜ ".join(parts))
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
    c.setdefault("confidence", "inferred")
    if c["confidence"] not in CONFS:
        return draft(drafts, rc, "claim_draft", raw, "schema", f"confidence 非法：{c['confidence']}")
    downgraded = False
    if c["source"] == "ai" and c["confidence"] == "confirmed":
        c["confidence"] = "inferred"
        downgraded = True

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
                     "context 没有引用依据原话（至少 8 个字连续）")

    c.setdefault("since", today_iso())
    if not to_date(c["since"]):
        return draft(drafts, rc, "claim_draft", raw, "bad_date", f"since 非法：{c['since']}")

    want = None
    if c["type"] in REVIEW_DAYS:
        want = add_days(c["since"], REVIEW_DAYS[c["type"]])
    given = c.get("review_after")
    if given is not None and want and to_date(given):
        lo = add_days(c["since"], {"state": 60, "disposition": 300}[c["type"]])
        hi = add_days(c["since"], {"state": 200, "disposition": 400}[c["type"]])
        c["review_after"] = given if lo <= given <= hi else want
    else:
        c["review_after"] = want

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
        note += "（ai 不能 confirmed，已降为 inferred）"
    rc.add("✅", c["id"], "新增判断", note)

    # 话别说得比证据远：依据全来自"干活现场"（kind=行为），判断却落在生活域
    if c.get("topic") not in ("work", "interface", "misc"):
        kinds = [fid_map[b].get("kind") for b in c.get("basis", []) if b in fid_map]
        if kinds and all(k == "行为" for k in kinds):
            rc.add("⚠️", c["id"], f"落在 {c['topic']}，但依据全来自干活现场",
                   "这条是不是写高了？拿掉这个场合，还有别的场合也符合吗")


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


def renew_claim(cid, reason, claims, rc):
    c = next((x for x in claims if x["id"] == cid), None)
    if not c or c.get("status") != "active":
        rc.add("➖", cid, "不是活跃判断，没法续期")
        return
    if c.get("type") not in REVIEW_DAYS:
        rc.add("➖", cid, "这个类型没有复核期，不用续", str(c.get("type")))
        return
    c["review_after"] = add_days(today_iso(), REVIEW_DAYS[c["type"]])
    c["renewed_at"] = today_iso()
    c["renewed_reason"] = reason
    rc.add("🚪", cid, f"续期到 {c['review_after']}", reason)


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


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--said")
    ap.add_argument("--facts")
    ap.add_argument("--claims")
    ap.add_argument("--tensions")
    ap.add_argument("--attach", action="append", default=[],
                    help="c-023:f-0001,f-0002，可重复")
    ap.add_argument("--exclude", help="逗号分隔的 fact id")
    ap.add_argument("--skip", help="永久跳过某个域（要 --reason）")
    ap.add_argument("--defer", help="暂时不想聊某个域（要 --reason，默认 30 天）")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--retire", help="退役判断，逗号分隔可批量（要 --reason）")
    ap.add_argument("--unretire", help="把退役的判断放回来，逗号分隔可批量")
    ap.add_argument("--renew", help="续期：复核期往后推，逗号分隔可批量")
    ap.add_argument("--retopic", help="改归档：c-038:work,c-060:work（要 --reason）")
    ap.add_argument("--due", nargs="?", type=int, const=3, default=None, metavar="N",
                    help="摆出复核期到了的判断，今天先看 N 条（默认 3）")
    ap.add_argument("--action", choices=["noise", "archived"], default="noise")
    ap.add_argument("--reason")
    ap.add_argument("--round", type=int)
    ap.add_argument("--topic")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--drafts", action="store_true")
    ap.add_argument("--drop", help="关闭某条草稿")
    args = ap.parse_args()

    if not (ME / "claims.jsonl").exists():
        print("me/ 还没初始化，先跑 python scripts/init.py")
        return 1

    facts, claims = load("facts.jsonl"), load("claims.jsonl")
    tensions, drafts = load("tensions.jsonl"), load("drafts.jsonl")
    transcript, rounds = load("transcript.jsonl"), load("rounds.jsonl")
    topics = load("topics.jsonl")

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

    if args.drop:
        if not args.reason:
            print("关闭草稿必须写 --reason")
            return 1
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

    if args.due is not None:
        n = max(1, args.due)
        today = today_iso()
        due = [c for c in claims if c.get("status") == "active"
               and c.get("review_after") and c["review_after"] < today]
        due.sort(key=lambda c: (c.get("review_after") or "", c["id"]))
        if not due:
            print("✅ 没有到期的判断")
            return 0
        show = due[:n]
        rest = len(due) - len(show)
        print(f"⏳ {len(due)} 条复核期到了 ｜ 今天先看 {len(show)} 条"
              + (f"，剩下 {rest} 条排着（排队，不是欠债）" if rest else ""))
        print("")
        for i, c in enumerate(show, 1):
            late = age_days(c.get("review_after"), today) or 0
            print(f"  {i}. {c['id']}  {c.get('type')} · {c.get('topic')}"
                  f" · 逾期 {late} 天 · 依据 {len(c.get('basis') or [])} 条")
            print(f"     「{clean(c.get('text'), 34)}」")
        print("")
        print("每条四种收场，挑一个（四步判定的结论决定挑哪个）：")
        print("  还成立，这轮有新证据 → --attach <id>:f-xxxx")
        print('  还成立，没新证据     → --renew <id> --reason "…"')
        print("  不成立，有新结论     → 写新 claim + supersedes + reason")
        print('  不成立，写不出替代   → --retire <id> --reason "…"')
        if len(show) > 1:
            print(f'  同一批同一个理由     → --renew {",".join(c["id"] for c in show)} --reason "…"')
        return 0

    st_old = json.loads((ME / "state.json").read_text(encoding="utf-8")) \
        if (ME / "state.json").exists() else {}
    round_no = args.round if args.round is not None else int(st_old.get("round", 0)) + 1

    # 原话先落地：连"没记的那些"也留下
    new_said = []
    for it in (load_any(args.said) if args.said else []):
        if isinstance(it, str):
            text, ignored = it, None
        else:
            text, ignored = it.get("text", ""), it.get("ignored")
        if not text.strip():
            continue
        row = {"id": next_id(transcript + new_said, "s", 4), "date": today_iso(),
               "round": round_no, "text": text}
        if ignored:
            row["ignored"] = ignored
        new_said.append(row)

    rc = Receipt()
    refmap = {}
    facts_before, claims_before = len(facts), len(claims)
    fid_map = {f["id"]: f for f in facts}

    if args.skip or args.defer:
        dom = args.skip or args.defer
        kind = "skipped" if args.skip else "deferred"
        if not args.reason:
            print("--skip / --defer 必须带 --reason（这是人的决定）")
            return 1
        if not check_mod.valid_topic(dom):
            print(f"❌ 不认识的域：{dom}")
            return 1
        rc.count_input()
        topics = [t for t in topics if t.get("domain") != dom]
        row = {"domain": dom, "state": kind, "reason": args.reason, "since": today_iso()}
        if kind == "deferred":
            row["until"] = add_days(today_iso(), args.days)
        topics.append(row)
        rc.add("✅", dom, "跳过" if kind == "skipped" else f"暂缓 {args.days} 天", args.reason)

    if args.retire or args.unretire or args.renew or args.retopic:
        if args.retire:
            if not args.reason:
                print("--retire 必须带 --reason（否则就是静默删除）")
                return 1
            for cid in id_list(args.retire):
                rc.count_input()
                retire_claim(cid, args.reason, claims, tensions, rc, fid_map)
        if args.unretire:
            for cid in id_list(args.unretire):
                rc.count_input()
                unretire_claim(cid, claims, rc)
        if args.renew:
            if not args.reason:
                print("--renew 必须带 --reason（记下你为什么还认它）")
                return 1
            for cid in id_list(args.renew):
                rc.count_input()
                renew_claim(cid, args.reason, claims, rc)
        if args.retopic:
            if not args.reason:
                print("--retopic 必须带 --reason（归档错了也要留痕：为什么它不在那儿）")
                return 1
            for spec in id_list(args.retopic):
                rc.count_input()
                retopic_claim(spec, args.reason, claims, rc)

    for raw in (load_any(args.facts) if args.facts else []):
        take_fact(raw, facts, rc, drafts, refmap)
    for f in facts:
        fid_map[f["id"]] = f

    if args.exclude:
        if not args.reason:
            print("--exclude 必须带 --reason（这是人的决定，要留痕）")
            return 1
        unknown = exclude_facts([x.strip() for x in args.exclude.split(",") if x.strip()],
                                args.action, args.reason, facts, rc)
        if unknown:
            print(f"❌ 找不到这些 fact：{unknown} —— 什么都没写")
            return 1

    attach_delta = attach_basis(args.attach, claims, facts, rc)

    for raw in (load_any(args.claims) if args.claims else []):
        take_claim(raw, claims, fid_map, rc, drafts, refmap, tensions)

    for raw in (load_any(args.tensions) if args.tensions else []):
        take_tension(raw, tensions, claims, rc, drafts, refmap)

    # 整批预检：这一步只该抓到"跨条"的问题（超额、重复 id、取代状态不一致）
    errors, warns = check_mod.validate(facts, claims, tensions, drafts,
                                       manifest=None, require_rendered=False,
                                       check_views=False, topics=topics)
    if errors:
        print("❌ 预检失败，什么都没写：")
        for e in errors:
            print(f"   {e}")
        return 1

    if args.dry:
        rc.print()
        print("（--dry：以上是预演，没有写入）")
        return 0

    # 原话覆盖：这一轮你说的，有没有落到事实上
    fact_texts = [f["text"] for f in facts if not f.get("excluded")]
    cov = {"landed": 0, "ignored": 0, "short": 0, "missing": []}
    for s in new_said:
        if s.get("ignored"):
            cov["ignored"] += 1
        elif len("".join(s["text"].split())) < 8:
            cov["short"] += 1
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

    stat = round_stats(facts_before, facts, claims_before, claims,
                       attach_delta, len(new_said), round_no)
    dump_jsonl(ME / "rounds.jsonl", rounds + [stat])

    st = update_state(round_no, args.topic)

    lost = rc.print()
    if new_said:
        print(f"\n你说 {len(new_said)} 句 ｜ {cov['landed']} 句落地 ｜ "
              f"{cov['ignored']} 句标为无价值 ｜ {cov['short']} 句太短 ｜ "
              f"{len(cov['missing'])} 句没落地")
        for s in cov["missing"][:3]:
            print(f"   ⚠️ 没落地：{clean(s['text'], 40)}")
    else:
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


