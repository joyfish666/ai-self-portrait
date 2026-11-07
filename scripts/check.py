#!/usr/bin/env python3
"""一秒体检：检查结构不变量。纯代码，不调 LLM，不做语义判断。

退出码：0 通过（可以有警告）｜1 有错误

check 只"检测"，不"修"。写入前的那道闸门在 commit.py 里。
"""
import difflib
import json
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ME = ROOT / "me"
sys.path.insert(0, str(ROOT / "scripts"))
from store import (ISO_RE, age_days, covered_by, has_fragment,  # noqa: E402
                   load, overlap, to_date)

DOMAINS = ["identity", "work", "methods", "goals", "values", "emotion",
           "health", "relations", "leisure"]
DEBT_RETRY, DEBT_RING, DEBT_CROWD = 20, 30, 30   # 天：开始重试 / 点名 / 攒太多
JUDGED = ("disposition", "inference")
TOPICS = set(DOMAINS) | {"interface", "misc"}
TYPES = {"constant", "state", "disposition", "inference"}
CONFS = {"confirmed", "inferred", "unsure", "unconfirmed"}
KINDS = {"自述", "行为", "他人转述", "AI观察"}
SOURCES = {"user", "ai", "external"}
FORBIDDEN = ["已纠正", "作废", "之前说", "待填写"]
MAX_DISP, MAX_STATE, MAX_TENSIONS = 60, 40, 10
TEXT_MAX, CTX_MIN, CTX_MAX, FACT_MAX = 40, 30, 150, 1000
FRAG = 8
# 复核期用区间而不是固定偏移：抓"复制粘贴写错"，不卡合理的半年期状态
REVIEW_DAYS = {"state": (60, 200), "disposition": (300, 400)}
VIEWS = ("PORTRAIT.md", "CONTEXT.md")


def _is_iso(v):
    return isinstance(v, str) and bool(ISO_RE.match(v)) and to_date(v) is not None


EXT_RE = re.compile(r"^[a-z][a-z0-9_]{1,20}$")


def valid_topic(t):
    """8 个域 + interface/misc，外加允许的扩展域（caregiving / sideproject …）。"""
    return t in TOPICS or (isinstance(t, str) and bool(EXT_RE.match(t)))


def validate(facts, claims, tensions, drafts, *, manifest=None,
             require_rendered=True, check_views=True, today=None, topics=None):
    """返回 (errors, warns)。commit.py 用 require_rendered=False 预检。"""
    errors, warns = [], []
    today = today or date.today().isoformat()

    def err(m):
        errors.append(m)

    def warn(m):
        warns.append(m)

    # ---------- topics（域的处置：跳过 / 暂缓）----------
    seen_dom = set()
    for t in (topics or []):
        d = t.get("domain")
        if not valid_topic(d):
            err(f"topic 记录里的域非法：{d}")
        if d in seen_dom:
            err(f"topic {d} 有两条记录（应该只留最新一条）")
        seen_dom.add(d)
        if t.get("state") not in ("skipped", "deferred"):
            err(f"topic {d} state 非法：{t.get('state')}")
        if not t.get("reason"):
            err(f"topic {d} 缺 reason")
        if not _is_iso(t.get("since")):
            err(f"topic {d} since 不是 YYYY-MM-DD")
        if t.get("state") == "deferred" and not _is_iso(t.get("until")):
            err(f"topic {d} 是暂缓，但 until 不是日期")

    # ---------- facts ----------
    fid = {}
    for f in facts:
        i = f.get("id", "?")
        if i in fid:
            err(f"fact id 重复：{i}")
        for k in ("id", "date", "kind", "text", "topic"):
            if k not in f:
                err(f"fact {i} 缺字段 {k}")
        if not _is_iso(f.get("date")):
            err(f"fact {i} date 不是 YYYY-MM-DD：{f.get('date')!r}")
        if f.get("kind") not in KINDS:
            err(f"fact {i} kind 非法：{f.get('kind')}")
        if "triage" in f:
            err(f"fact {i} 仍有 triage —— 派生字段不入库，改用 excluded")
        ex = f.get("excluded")
        if ex is not None:
            if not isinstance(ex, dict) or ex.get("action") not in ("noise", "archived"):
                err(f"fact {i} excluded.action 非法：{ex}")
            elif not ex.get("reason"):
                err(f"fact {i} excluded 缺 reason")
        if not valid_topic(f.get("topic")):
            err(f"fact {i} topic 非法：{f.get('topic')}")
        if f.get("source") not in SOURCES:
            err(f"fact {i} source 非法：{f.get('source')}")
        if len(f.get("text", "")) > FACT_MAX:
            err(f"fact {i} 正文超 {FACT_MAX} 字（{len(f['text'])}）")
        fid[i] = f

    # ---------- claims ----------
    cid = {}
    for c in claims:
        i = c.get("id", "?")
        if i in cid:
            err(f"claim id 重复：{i} —— 两条判断会互相覆盖，必须改号")
        cid[i] = c
        for k in ("id", "topic", "type", "text", "context", "source",
                  "confidence", "since", "basis", "status"):
            if k not in c:
                err(f"claim {i} 缺字段 {k}")
        if c.get("type") not in TYPES:
            err(f"claim {i} type 非法：{c.get('type')}")
        if c.get("confidence") not in CONFS:
            err(f"claim {i} confidence 非法：{c.get('confidence')}")
        if not valid_topic(c.get("topic")):
            err(f"claim {i} topic 非法：{c.get('topic')}")
        if c.get("status") not in ("active", "superseded", "retired"):
            err(f"claim {i} status 非法：{c.get('status')}")
        if c.get("status") == "retired":
            if not c.get("retired_reason"):
                err(f"claim {i} 退役了但没写 retired_reason —— 那就是静默删除")
            if not _is_iso(c.get("retired_at")):
                err(f"claim {i} retired_at 不是 YYYY-MM-DD")
        if c.get("renewed_at") is not None:
            if not _is_iso(c.get("renewed_at")):
                err(f"claim {i} renewed_at 不是 YYYY-MM-DD")
            if not c.get("renewed_reason"):
                err(f"claim {i} 续期了但没写 renewed_reason")
        if c.get("source") not in SOURCES:
            err(f"claim {i} source 非法：{c.get('source')}")
        if len(c.get("text", "")) > TEXT_MAX:
            err(f"claim {i} text 超 {TEXT_MAX} 字（{len(c['text'])}）")
        n = len(c.get("context", ""))
        if not (CTX_MIN <= n <= CTX_MAX):
            err(f"claim {i} context 长度 {n}，应在 {CTX_MIN}–{CTX_MAX}")

        basis = c.get("basis") or []
        if not basis:
            err(f"claim {i} basis 为空")
        for b in basis:
            if b not in fid:
                err(f"claim {i} basis 悬空：{b}")
        if c.get("source") == "ai" and c.get("confidence") == "confirmed":
            err(f"claim {i} 红线：source=ai 不得 confidence=confirmed")

        # 复核期：日期格式 + type 决定的区间
        if not _is_iso(c.get("since")):
            err(f"claim {i} since 不是 YYYY-MM-DD：{c.get('since')!r}")
        ra = c.get("review_after")
        t = c.get("type")
        if ra is not None and not _is_iso(ra):
            err(f"claim {i} review_after 不是 YYYY-MM-DD：{ra!r}")
        elif ra is None:
            if t in ("state", "disposition"):
                err(f"claim {i} type={t} 必须带 review_after")
        else:
            if t in ("constant", "inference"):
                err(f"claim {i} type={t} 的 review_after 必须为 null")
            elif _is_iso(c.get("since")):
                # 续期把时钟拨到"我还认它的那天"：从 renewed_at 起算，否则老判断
                # 续一次就必然越界（since 越老，跨度越大），等于不许续期。
                anchor = c.get("renewed_at") if _is_iso(c.get("renewed_at")) else c["since"]
                days = (to_date(ra) - to_date(anchor)).days
                lo, hi = REVIEW_DAYS[t]
                if not (lo <= days <= hi):
                    err(f"claim {i} 复核期 {days} 天，type={t} 应在 {lo}–{hi} 天")

        # 依据有效性：被引用为 noise 是错误；只引用了已排除的事实是警告
        src_text = "".join(fid[b]["text"] for b in basis if b in fid)
        live_basis = [b for b in basis if b in fid and not fid[b].get("excluded")]
        if basis and not live_basis:
            warn(f"claim {i} 的依据全部被排除（{'、'.join(basis[:3])}）—— 该判断需要重新推导")
        elif live_basis and len(live_basis) < len(basis):
            warn(f"claim {i} 有依据已被排除：{sorted(set(basis) - set(live_basis))[:3]}")
        if basis and src_text and not has_fragment(c.get("context", ""), src_text, FRAG):
            err(f"claim {i} context 未包含任何 basis 原话片段（{FRAG} 字）")
        # 观察可以是证据，不能是唯一的证据。
        # 一格"我看到的"如果单独撑起一条判断，就成了用我的眼睛代替他的嘴——
        # 历史上那 14 条就是这么出事的：没有原话，就往 AI观察 里倒结论。
        if live_basis and all(fid[b].get("source") == "ai" for b in live_basis):
            warn(f"claim {i} 的依据全是我观察到的，没有一句他自己的话"
                 f"（{'、'.join(live_basis[:3])}）—— 补一条他的原话，或者这条还不该写成判断")

        # 推翻必须写理由
        sup = c.get("supersedes") or []
        if sup:
            if not c.get("reason"):
                err(f"claim {i} supersedes 非空但没写 reason")
            for x in sup:
                if x not in cid and not any(o.get("id") == x for o in claims):
                    err(f"claim {i} supersedes 指向不存在的 {x}")

        for w in FORBIDDEN:
            if w in c.get("text", "") or w in c.get("context", ""):
                err(f"claim {i} 出现禁词「{w}」")

    # superseded 的目标必须真的被标成 superseded
    claimed_by = {x for c in claims for x in (c.get("supersedes") or [])}
    for c in claims:
        if c.get("id") in claimed_by and c.get("status") != "superseded":
            err(f"claim {c['id']} 被别的判断取代，但 status 还是 {c.get('status')}")
        if c.get("status") == "superseded" and c.get("id") not in claimed_by:
            warn(f"claim {c['id']} 标了 superseded，但没有判断声明取代它")

    # ---------- tensions ----------
    active_ids = {c["id"] for c in claims if c.get("status") == "active"}
    seen_pairs = set()
    for t in tensions:
        i = t.get("id", "?")
        ids = t.get("between", [])
        if len(ids) != 2:
            err(f"tension {i} between 必须是 2 个 id")
            continue
        if t.get("status") == "open":
            pair = frozenset(ids)
            if pair in seen_pairs:
                err(f"tension {i} 与前面重复（同一对判断）")
            seen_pairs.add(pair)
            for x in ids:
                if x not in active_ids:
                    err(f"tension {i} 指向不存在或非活跃的 {x}")
        if not (5 <= len(t.get("desc", "")) <= 100):
            err(f"tension {i} desc 应在 5–100 字")
        if t.get("status") not in ("open", "closed"):
            err(f"tension {i} status 非法：{t.get('status')}")
        if t.get("status") == "closed" and not t.get("closed_reason"):
            err(f"tension {i} 已关闭但没写 closed_reason")
        if not _is_iso(t.get("since")):
            err(f"tension {i} since 不是 YYYY-MM-DD：{t.get('since')!r}")

    # ---------- 数量 ----------
    active = [c for c in claims if c.get("status") == "active"]
    disp = [c for c in active if c.get("type") in JUDGED]
    state = [c for c in active if c.get("type") == "state"]
    if len(disp) > MAX_DISP:
        err(f"判断有 {len(disp)} 条，超过上限 {MAX_DISP}。两条路："
            f"① 合并（写一条新的 supersedes 掉 2–3 条，模型更紧）；"
            f"② 退役（--retire c-xxx --reason \"…\"，模型会变松）")
    if len(state) > MAX_STATE:
        err(f"状态有 {len(state)} 条，超过上限 {MAX_STATE}。两条路："
            f"① 合并；② 退役（--retire c-xxx --reason \"…\"）")
    if len([t for t in tensions if t.get("status") == "open"]) > MAX_TENSIONS:
        err(f"tensions 超过上限 {MAX_TENSIONS}")

    # ---------- 孤儿 / 视图 ----------
    if manifest is not None:
        rendered = set(manifest.get("claims", []))
        miss = {c["id"] for c in active} - rendered
        if require_rendered and miss:
            err(f"有 {len(miss)} 条 active claim 不在上次 build 的视图里："
                f"{sorted(miss)[:5]} …（先 build 再 check）")
        if check_views:
            for name in VIEWS:
                want = (manifest.get("views") or {}).get(name)
                got = _hash(ME / name)
                if want and got != want:
                    err(f"{name} 和 build 产物不一致（被手改过？）—— 跑 build.py 覆盖")

    # ---------- 警告级 ----------
    # 注意：`asked` 覆盖率不在这里查——它要"只盯接入之后的事实"，需要 rounds 数据，
    # 由 main() 处理（旧数据不追诉，新数据 commit.py 写入时就硬拦）。

    # 过期不再是"警告"：时间到了不等于该更新，用的时候看年龄（CONTEXT 里带 ·3月前）

    dup = [(a["id"], b["id"], round(difflib.SequenceMatcher(None, a["text"], b["text"]).ratio(), 2))
           for i, a in enumerate(active) for b in active[i + 1:]
           if difflib.SequenceMatcher(None, a["text"], b["text"]).ratio() > 0.8]
    if dup:
        warn(f"{len(dup)} 组判断疑似重复，建议合并：{dup[:3]}")

    if drafts:
        for d in drafts:
            for k in ("id", "kind", "reason"):
                if not d.get(k):
                    err(f"draft {d.get('id', '?')} 缺字段 {k}")
        open_drafts = [d for d in drafts if d.get("status", "open") == "open"]
        if open_drafts:
            warn(f"草稿区还有 {len(open_drafts)} 条待处理（commit.py --drafts 查看）")

    cited = {b for c in claims for b in (c.get("basis") or []) if b in fid}
    bad_noise = [i for i in cited if (fid[i].get("excluded") or {}).get("action") == "noise"]
    if bad_noise:
        err(f"{len(bad_noise)} 条事实被引用却被标 noise：{bad_noise[:3]}")
    live = [f for f in facts if not f.get("excluded")]
    pending = [f for f in live if f["id"] not in cited]
    if live and len(pending) / len(live) > 0.8:
        warn(f"未消化率 {100*len(pending)/len(live):.0f}%（{len(pending)}/{len(live)}）"
             f"—— 跑 digest.py 消化一批")

    SUSPECT = ("判断：", "关键：", "修正：", "评估：", "洞察：", "推论：")
    mixed = [f["id"] for f in live
             if f.get("kind") == "AI观察" and f["text"].startswith(SUSPECT)]
    if mixed:
        warn(f"{len(mixed)} 条事实疑似“判断混进事实层”：{mixed[:3]} …（应升为 claim 或标 excluded）")

    empty = [d for d in DOMAINS if not any(c.get("topic") == d and c.get("type") in JUDGED
                                           for c in active)]
    if empty:
        warn(f"{len(empty)} 个域没有任何判断：{empty}")
    if len(disp) >= 10 and not [t for t in tensions if t.get("status") == "open"]:
        warn("已有 ≥10 条判断但 tensions 为 0——可能漏了矛盾检测")

    return errors, warns


def _hash(path):
    import hashlib
    p = Path(path)
    if not p.exists():
        return None
    return hashlib.md5(p.read_bytes()).hexdigest()[:8]


def cited_by_active(claims):
    """只算在役判断引用了哪些事实——退役/被取代的判断不再“占着”依据。"""
    return {b for c in claims if c.get("status") == "active"
            for b in (c.get("basis") or [])}


def debt_report(facts, claims, today=None):
    """最老的一笔债：欠了几天、现在能不能挂上、该不该点名。"""
    today = today or date.today().isoformat()
    cited = cited_by_active(claims)
    live = [f for f in facts if not f.get("excluded")]
    pending = [f for f in live if f["id"] not in cited]
    out = {"count": len(pending), "oldest": None, "age": 0,
           "tier": "none", "cands": [], "crowded": len(pending) >= DEBT_CROWD}
    if not pending:
        return out
    oldest = min(pending, key=lambda f: (f.get("date", ""), f["id"]))
    age = age_days(oldest.get("date"), today) or 0
    cands = []
    for c in claims:
        if c.get("status") != "active" or c.get("topic") != oldest.get("topic"):
            continue
        s, shared = overlap(oldest.get("text", ""), c["text"] + c.get("context", ""))
        if shared >= 3:
            cands.append({"id": c["id"], "text": c["text"],
                          "shared": shared, "score": round(s, 2)})
    cands.sort(key=lambda x: (-x["score"], x["id"]))
    tier = "ring" if age >= DEBT_RING else ("retry" if age >= DEBT_RETRY else "quiet")
    out.update({"oldest": oldest, "age": age, "tier": tier, "cands": cands[:2]})
    return out


def main():
    if not ME.is_dir():
        print("me/ 不存在。先跑 python scripts/init.py")
        return 1

    facts, claims = load("facts.jsonl"), load("claims.jsonl")
    tensions, drafts = load("tensions.jsonl"), load("drafts.jsonl")
    transcript, rounds = load("transcript.jsonl"), load("rounds.jsonl")
    topics = load("topics.jsonl")
    bj = ME / ".build.json"
    manifest = json.loads(bj.read_text(encoding="utf-8")) if bj.exists() else None

    errors, warns = validate(facts, claims, tensions, drafts, manifest=manifest,
                             topics=topics)

    # 当前域被跳过/暂缓了，但 state 还指着它
    cur = None
    sp = ME / "state.json"
    if sp.exists():
        try:
            cur = json.loads(sp.read_text(encoding="utf-8")).get("current_topic")
        except json.JSONDecodeError:
            cur = None
    for t in topics:
        if t.get("domain") != cur:
            continue
        if t.get("state") == "skipped":
            warns.append(f"当前域 {cur} 已被跳过，但 state 还指着它 —— 换下一域")
        elif (t.get("until") or "") > date.today().isoformat():
            warns.append(f"当前域 {cur} 在暂缓期（到 {t.get('until')}）—— 换下一域")
        break

    # 原话覆盖：你说过的话，有没有一句没落到事实上
    live_texts = [f["text"] for f in facts if not f.get("excluded")]
    missing = [s for s in transcript
               if not s.get("ignored") and not covered_by(s.get("text", ""), live_texts)]
    if missing:
        warns.append(f"{len(missing)} 句你说的话没落到任何事实（{missing[0].get('id')} 起）"
                     f"—— 看看是漏记，还是该标 ignored")

    # asked 覆盖率：只盯"接入新规则之后"的事实，老数据不追诉
    cutoff = rounds[0].get("date") if rounds else date.today().isoformat()
    recent = [f for f in facts
              if not f.get("excluded") and (f.get("date") or "") >= cutoff]
    no_asked = [f["id"] for f in recent if not f.get("asked")]
    if no_asked:
        warns.append(f"接入之后有 {len(no_asked)} 条事实没带 asked（{no_asked[:3]}）"
                     f"—— 四步判定的第 2 步会失效")

    # 原地打转：连续 3 轮聊到同一个域，判断和依据都没涨
    if len(rounds) >= 3:
        tail = rounds[-3:]
        doms = set().union(*[set(r.get("facts", {})) | set(r.get("judged", {})) |
                             set(r.get("basis", {})) for r in tail])
        for d in sorted(doms):
            if not any(r.get("facts", {}).get(d) or r.get("judged", {}).get(d)
                       or r.get("basis", {}).get(d) for r in tail):
                continue
            if all(r.get("judged", {}).get(d, 0) == 0 and r.get("basis", {}).get(d, 0) == 0
                   for r in tail):
                warns.append(f"「{d}」最近 3 轮有聊到，但判断和依据都没增加——可能只是在重复")

    # 债的年龄：静默 → 重试 → 点名
    dr = debt_report(facts, claims)
    if dr["tier"] == "retry":
        tip = "、".join(f"{c['id']}（重合 {c['shared']}）" for c in dr["cands"])
        warns.append(f"最老的一笔债 {dr['oldest']['id']} 欠了 {dr['age']} 天"
                     + (f"，现在可能能挂上：{tip}" if tip else "，没找到能挂的判断"))
    elif dr["tier"] == "ring":
        warns.append(f"{dr['oldest']['id']} 已经 {dr['age']} 天没家 —— "
                     f"挂上去 / 立一条新判断 / 标废，三选一")
    if dr["crowded"]:
        warns.append(f"未消化 {dr['count']} 条，一次攒得有点多 —— 跑 digest.py 消化一批")

    live = [f for f in facts if not f.get("excluded")]
    active = [c for c in claims if c.get("status") == "active"]
    disp = [c for c in active if c.get("type") in JUDGED]
    state = [c for c in active if c.get("type") == "state"]
    cited = cited_by_active(claims)
    pending = [f for f in live if f["id"] not in cited]

    print(f"facts {len(live)} 活跃 / {len(facts)-len(live)} 排除 ｜ 未消化 {len(pending)}"
          f" ｜ 判断 {len(disp)} / 状态 {len(state)} / 背景 {len(active)-len(disp)-len(state)}"
          f" ｜ tensions {len(tensions)}"
          f" ｜ drafts {sum(1 for d in drafts if d.get('status','open')=='open')}")
    for w in warns:
        print(f"⚠️  {w}")
    for e in errors:
        print(f"❌ {e}")
    if errors:
        print(f"\n未通过：{len(errors)} 个错误，{len(warns)} 个警告")
        return 1
    print(f"\n✅ 通过：0 错误，{len(warns)} 个警告")
    return 0


if __name__ == "__main__":
    sys.exit(main())



