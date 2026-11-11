#!/usr/bin/env python3
"""自测：把 SPEC §8 的验收变成能跑的断言。

用法
    python scripts/selftest.py          # 两层全跑，全绿返回 0（规矩不变）
    python scripts/selftest.py --unit   # 只跑纯函数层：秒级，改代码时先跑这个
    python scripts/selftest.py --e2e    # 只跑端到端层
    python scripts/selftest.py --keep   # 保留临时目录，方便看哪里坏了

两层：
    unit —— 直接 import 纯函数，不起子进程、不建临时仓、不碰 me/。
    e2e  —— 每个 case 在临时目录里从零建仓（只拷 scripts/ 和 template/），
            用 _fixtures.py 的虚构数据跑完整条命令链。

unit 直接 import 生产函数，0 子进程、约 0.1 秒出结果；
e2e 只留 12 个必须跑完整命令链的（整批预检、视图哈希、git 提交、两阶段捕获等）。
其余命令链用例已转成内存态单测：少起约 110 次子进程，全量目标约 1 分钟。
代价是跨脚本串联的覆盖变薄，这是明确的取舍，不是白捡的速度。
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import _fixtures as fx  # noqa: E402

TODAY = date.today().isoformat()
TMP = []
CASES = []
UNIT = []
CALLS = [0]


def case(fn):
    CASES.append(fn)
    return fn


def unit(fn):
    UNIT.append(fn)
    return fn


def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def sandbox():
    d = Path(tempfile.mkdtemp(prefix="sp_st_"))
    TMP.append(d)
    shutil.copytree(ROOT / "scripts", d / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "template", d / "template")
    code, out = run(d, "init.py")
    assert code == 0, f"init 失败：\n{out}"
    return d


def run(d, script, *args):
    CALLS[0] += 1
    r = subprocess.run([sys.executable, str(Path(d) / "scripts" / script), *args],
                       cwd=str(d), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def put(d, name, obj):
    (Path(d) / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def jsonl(d, name):
    p = Path(d) / "me" / name
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def seed(d, facts=None, claims=None):
    """用一轮 commit 把基础数据灌进去。"""
    put(d, "f.json", facts if facts is not None else fx.base_facts(TODAY))
    args = ["commit.py", "--facts", "f.json"]
    if claims is not None:
        put(d, "c.json", claims)
        args += ["--claims", "c.json"]
    code, out = run(d, *args)
    assert code == 0, f"seed 失败：\n{out}"
    return out


# ---------------------------------------------------------------- 纯函数层（秒级，不起子进程）

@unit
def unit_对账单_丢失由未认领输入算出():
    """加一种收场动作不该再需要改减法：只要它 add 过一次，这条输入就有终态。"""
    import contextlib
    import io
    from commit import Receipt
    r = Receipt()
    r.count_input(3)
    r.add("✅", "f-0001", "入库")
    r.add("🔶", "d-001", "进草稿")
    r.add("🪄", "x-001", "一种全新的收场动作")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lost = r.print()
    assert r.inputs == 3 and lost == 0, (r.inputs, lost, buf.getvalue())


@unit
def unit_对账单_少一个终态就算丢失并点名():
    import contextlib
    import io
    from commit import Receipt
    r = Receipt()
    r.count_input(2)
    r.add("✅", "f-0001", "入库")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lost = r.print()
    assert lost == 1 and "#2" in buf.getvalue(), buf.getvalue()


@unit
def unit_对账单_警告不能冒充终态():
    from commit import Receipt
    r = Receipt()
    r.count_input(1)
    r.add("⚠️", "c-001", "像 80%", claim=False)
    assert len(r.unclaimed()) == 1
    r.add("✅", "c-001", "新增判断")
    assert len(r.unclaimed()) == 0


@unit
def unit_动作表与理由表一致():
    """加动作只改一张表：参数、理由校验、执行顺序都从它派生，不靠人记着同步。"""
    import commit
    names = [a["arg"] for a in commit.ARG_SPECS]
    assert len(names) == len(set(names)), "参数表里不能有重名"
    for arg, label in commit.REASON_ARGS.items():
        assert arg in names, f"{arg} 要理由却没登记参数"
        assert label, f"{arg} 没写人话标签"
    for arg in ("retire", "narrow", "retopic", "exclude", "drop"):
        assert arg in commit.REASON_ARGS, f"{arg} 没登记进理由表"
    for a in commit.ACTIONS:
        assert callable(a["handler"]), f"{a['arg']} 没有处理函数"
        assert a["arg"] in names
    order = [a["arg"] for a in commit.ACTIONS]
    assert order.index("facts") < order.index("exclude") < order.index("claims"), \
        "facts 必须在 exclude 前、claims 必须在 attach 后"


@unit
def unit_三处判重共用一把尺子():
    """校验层以前裸比原文，短句会虚高；现在和写入/坏味道一样走 contain()。"""
    from check import contain, duplicate_pairs
    short = [{"id": "c-001", "status": "active", "text": "想歇一下"},
             {"id": "c-002", "status": "active", "text": "想歇一会"}]
    assert contain(short[0]["text"], short[1]["text"]) == 0.0, "不足 6 字不参与判重"
    assert duplicate_pairs(short) == [], "短句不该被校验层报成重复"
    long = [{"id": "c-001", "status": "active", "text": "每天凌晨四点到店里先发面再烤第一批"},
            {"id": "c-002", "status": "active", "text": "每天凌晨四点到店里先发面再烤第二批"}]
    assert len(duplicate_pairs(long)) == 1, "明显同义的长句该被报出来"


@unit
def unit_片段校验_必须真含连续八字():
    from store import covered_by, has_fragment
    assert has_fragment("他说每天凌晨四点到店里先发面", "每天凌晨四点到店里，先发面再烤第一批")
    assert not has_fragment("完全不相干的一句话", "每天凌晨四点到店里，先发面再烤第一批")
    assert covered_by("嗯", [])                    # 少于 8 字，本来就不追究
    assert not covered_by("这句话真的完全没有落到任何一条事实上面", ["别的原话"])


@unit
def unit_选项编号不能当事实正文():
    from store import is_choice_code
    for bad in ("2B", "5D", "4 BD", "6 E", "C", "A/D", "BC"):
        assert is_choice_code(bad), f"{bad} 应当被认成选项编号"
    for good in ("先发面再烤第一批", "骑车去河边坐着", "C++",
                 "翻文档", "周末去超市买了点菜"):
        assert not is_choice_code(good), f"{good} 是正常回答，不该被拦"


@unit
def unit_短句不会被判成重复():
    from check import contain
    assert contain("干活靠手感不看表", "干活靠手感不看表") == 1.0
    assert contain("短", "短") == 0.0            # 不足 6 字不参与判重
    assert contain("干活靠手感不看表", "想开店但没算过账") < 0.75


@unit
def unit_空仓校验零错误():
    from check import validate
    e, _ = validate([], [], [], [], manifest=None,
                    require_rendered=False, check_views=False, topics=[])
    assert e == [], e


# ---------------------------------------------------------------- 内存态单测（从命令链用例转来，0 子进程）
# 这些以前每个都要从零建仓、跑完整条命令链；逻辑本身是纯函数，直接 import 验更快，
# 也不用靠视图里的字符串反推「数据到底变成什么样」。


def _mem():
    """造一个内存里的小仓库：3 条事实 + 3 条判断，全是虚构的面包店数据。"""
    from commit import Receipt
    F = [dict(f, id=f"f-{i+1:04d}") for i, f in enumerate(fx.base_facts(TODAY))]
    C = []
    for i, c in enumerate(fx.base_claims()):
        d = dict(c)
        d.update(id=f"c-{i+1:03d}", status="active", basis=[f"f-{i+1:04d}"], since=TODAY)
        C.append(d)
    return F, C, {f["id"]: f for f in F}, Receipt()


@unit
def unit_事实缺asked或写编号都进草稿():
    from commit import take_fact
    F, _, _, rc = _mem()
    drafts, refmap = [], []
    base = {"date": TODAY, "kind": "自述", "topic": "work", "source": "user"}
    take_fact(dict(base, text="这句没写当时问的是什么。"), F, rc, drafts, refmap)
    take_fact(dict(base, text="2B", asked="今天先做什么"), F, rc, drafts, refmap)
    assert len(F) == 3, "两条都不该进事实层"
    assert len(drafts) == 2, drafts
    assert {d["reason"] for d in drafts} == {"missing_asked", "choice_code"}, drafts


@unit
def unit_判断自报confirmed会被降级():
    from commit import take_claim
    F, C, fid, rc = _mem()
    drafts, refmap, tensions = [], {}, []
    raw = {"topic": "work", "type": "disposition", "text": "一句全新的判断",
           "context": "他说每天凌晨四点到店里，先发面再烤第一批——这是另外一条与前面都不相同的判断，用来测试自报确认会被降级。",
           "source": "user", "confidence": "confirmed", "basis": ["f-0001"]}
    take_claim(dict(raw), C, fid, rc, drafts, refmap, tensions)
    assert not drafts, drafts
    assert len(C) == 4 and C[-1]["confidence"] != "confirmed", C[-1]


@unit
def unit_收窄措辞只动text且留痕():
    from commit import narrow_claim
    F, C, fid, rc = _mem()
    narrows, before = [], dict(C[0])
    rc.count_input()
    narrow_claim("c-001", "干活靠手感", "", "原话没提表", C, fid, narrows, rc)
    assert C[0]["text"] == "干活靠手感" and len(C) == 3
    assert {k for k in before if C[0].get(k) != before[k]} == {"text"}
    assert narrows[0]["old"] == before["text"] and narrows[0]["reason"] == "原话没提表"
    assert rc.unclaimed() == []


@unit
def unit_收窄措辞超长与不在役会被拒():
    from commit import narrow_claim
    F, C, fid, rc = _mem()
    narrows = []
    narrow_claim("c-001", "很长" * 30, "", "太长", C, fid, narrows, rc)
    assert C[0]["text"] == "干活靠手感，不看表" and not narrows
    C[0]["status"] = "retired"
    narrow_claim("c-001", "换个说法", "", "退役了", C, fid, narrows, rc)
    assert C[0]["text"] == "干活靠手感，不看表" and not narrows


@unit
def unit_收窄说明必须含依据片段():
    from commit import narrow_claim
    F, C, fid, rc = _mem()
    narrows = []
    bad = "这段说明完全没有引用任何依据内容，纯粹是随便写的一句话凑够三十个字以上吧。"
    narrow_claim("c-001", "换个说法", bad, "试一下", C, fid, narrows, rc)
    assert C[0]["text"] == "干活靠手感，不看表" and not narrows
    good = "他说每天凌晨四点到店里，先发面再烤第一批——把说明也一起换成带原话的版本。"
    narrow_claim("c-001", "换个说法", good, "一起换", C, fid, narrows, rc)
    assert C[0]["text"] == "换个说法" and C[0]["context"] == good


@unit
def unit_退役与放回_理由与状态():
    from commit import retire_claim, unretire_claim
    F, C, fid, rc = _mem()
    retire_claim("c-001", "这条不用了", C, [], rc, fid)
    assert C[0]["status"] == "retired" and C[0]["retired_reason"] == "这条不用了"
    unretire_claim("c-001", C, rc)
    assert C[0]["status"] == "active" and "retired_reason" not in C[0]


@unit
def unit_批量退役不波及没点名的():
    from commit import retire_claim
    F, C, fid, rc = _mem()
    for cid in ("c-001", "c-002"):
        rc.count_input()
        retire_claim(cid, "这两条都不认了", C, [], rc, fid)
    assert [c["status"] for c in C] == ["retired", "retired", "active"]
    assert all(c["retired_reason"] == "这两条都不认了" for c in C[:2])


@unit
def unit_改归档只改topic不造版本():
    from commit import retopic_claim
    F, C, fid, rc = _mem()
    before = dict(C[1])
    retopic_claim("c-002:values", "归档错了", C, rc)
    assert C[1]["topic"] == "values" and len(C) == 3
    assert C[1]["status"] == "active" and not C[1].get("supersedes")
    assert {k for k in before if C[1].get(k) != before[k]} <= {"topic", "retopied_at", "retopied_reason"}


@unit
def unit_换依据要连context一起换():
    from commit import rewire_claims
    F, C, fid, rc = _mem()
    good = "他说想自己开一家小店，但一直没算过账——这条换成第二条依据了，说明也要跟着换。"
    rewire_claims([{"id": "c-001", "add": ["f-0002"], "del": ["f-0001"],
                    "context": good, "reason": "接错了"}], C, F, rc)
    assert C[0]["basis"] == ["f-0002"] and C[0]["context"] == good
    bad = "这段说明完全没有引用任何依据内容，纯粹是随便写的一句话凑够三十个字以上吧。"
    rewire_claims([{"id": "c-001", "add": ["f-0003"], "del": [],
                    "context": bad, "reason": "试一下"}], C, F, rc)
    assert C[0]["basis"] == ["f-0002"] and C[0]["context"] == good


@unit
def unit_撤依据不能把判断掏空():
    from commit import attach_basis, detach_basis
    F, C, fid, rc = _mem()
    attach_basis(["c-001:f-0002,f-0003"], C, F, rc)
    detach_basis(["c-001:f-0002,f-0003"], C, F, rc)
    assert C[0]["basis"] == ["f-0001"]
    detach_basis(["c-001:f-0001"], C, F, rc)
    assert C[0]["basis"] == ["f-0001"], "撤到最后一条必须拒绝"


@unit
def unit_迁移幂等不重复写事件():
    from commit import migrate_legacy
    F, C, fid, rc = _mem()
    transcript, events, migrations = [], [], []
    migrate_legacy(C, transcript, events, migrations, rc)
    migrate_legacy(C, transcript, events, migrations, rc)
    assert len(migrations) == 1 and migrations[0]["kind"] == "legacy"
    assert len(events) == 1, "第二次不该再写一条迁移事件"


@unit
def unit_消化债按年龄分层():
    from check import debt_report
    F, C, fid, rc = _mem()
    for days, tier in ((25, "retry"), (35, "ring")):
        old = (date.today() - timedelta(days=days)).isoformat()
        rows = [dict(f) for f in F] + [
            {"id": "f-0009", "date": old, "kind": "自述", "asked": "周末怎么过",
             "text": "周末骑车去河边坐一下午，不做计划。", "topic": "leisure", "source": "user"}]
        d = debt_report(rows, C)
        assert d["count"] >= 1 and d["age"] >= days - 1, d
        assert d["tier"] == tier, (days, d["tier"])


@unit
def unit_原话遮敏感串但行不删():
    from commit import redact_transcript
    T = [{"id": "s-0001", "text": "我的邮箱是 someone@example.com，别传上去"}]
    rc = _mem()[3]
    redact_transcript(["s-0001"], "测试：遮掉邮箱", T, rc)
    assert len(T) == 1 and "[邮箱已遮]" in T[0]["text"]
    assert "someone@example.com" not in T[0]["text"]
    assert T[0]["redact_reason"] == "测试：遮掉邮箱"


@unit
def unit_校验层判重也走同样算法():
    from check import validate, duplicate_pairs
    short = [{"id": "c-001", "status": "active", "text": "想歇一下"},
             {"id": "c-002", "status": "active", "text": "想歇一会"}]
    assert duplicate_pairs(short) == []
    F = [{"id": "f-0001", "date": TODAY, "kind": "自述", "asked": "最近在忙什么",
          "text": "每天凌晨四点到店里，先发面再烤第一批。", "topic": "work", "source": "user"}]
    rows = [{"id": "c-001", "topic": "work", "type": "disposition",
             "text": "每天凌晨四点到店里先发面再烤第一批", "context":
             "他说每天凌晨四点到店里，先发面再烤第一批——每天都这个点。",
             "source": "ai", "confidence": "inferred", "since": TODAY,
             "basis": ["f-0001"], "status": "active"},
            {"id": "c-002", "topic": "work", "type": "disposition",
             "text": "每天凌晨四点到店里先发面再烤第二批", "context":
             "他说每天凌晨四点到店里，先发面再烤第一批——第二批也差不多。",
             "source": "ai", "confidence": "inferred", "since": TODAY,
             "basis": ["f-0001"], "status": "active"}]
    _, warns = validate(F, rows, [], [], require_rendered=False,
                        check_views=False, topics=[], events=[], migrations=[],
                        transcript=[])
    assert any("疑似重复" in w for w in warns), warns


@unit
def unit_体检在百条量级不退化为平方级():
    import time
    from check import validate
    rows = []
    for i, r in enumerate(fx.bulk_claims(120)):
        d = dict(r)
        d.update(id=f"c-{i+1:04d}", since=TODAY, supersedes=[], status="active")
        rows.append(d)
    t0 = time.time()
    validate([], rows, [], [], require_rendered=False, check_views=False, topics=[])
    assert time.time() - t0 < 3.0, "体检明显变慢了"


# ---------------------------------------------------------------- 承诺

@case
def g3_空仓也能出画像():
    d = sandbox()
    code, out = run(d, "build.py")
    assert code == 0, out
    p = (d / "me" / "PORTRAIT.md").read_text(encoding="utf-8")
    assert len(p.splitlines()) >= 10, f"空画像太短：{p!r}"
    code, out = run(d, "check.py")
    assert code == 0, out


@case
def g1_依据悬空进草稿():
    d = sandbox()
    seed(d, claims=[])
    put(d, "bad.json", [{"topic": "work", "type": "state",
                         "text": "占位判断",
                         "context": "他说每天凌晨四点到店里，先发面再烤第一批，这条的依据是编的。",
                         "source": "ai", "confidence": "inferred", "basis": ["f-9999"]}])
    code, out = run(d, "commit.py", "--claims", "bad.json")
    assert code == 0, out
    assert "进草稿" in out, out
    assert jsonl(d, "claims.jsonl") == [], "悬空依据不该进 claims"
    assert len(jsonl(d, "drafts.jsonl")) == 1, "应该有一条草稿"


@case
def g2_手改视图会被抓():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    code, _ = run(d, "build.py")
    assert code == 0
    code, out = run(d, "check.py")
    assert code == 0, out
    p = d / "me" / "PORTRAIT.md"
    p.write_text(p.read_text(encoding="utf-8") + "\n手改一行\n", encoding="utf-8")
    code, out = run(d, "check.py")
    assert code == 1 and "被手改" in out, out
    run(d, "build.py")
    code, out = run(d, "check.py")
    assert code == 0, out


@case
def g6_过软线不拦_过安全网才拦():
    d = sandbox()
    seed(d, facts=fx.base_facts(TODAY)[:1], claims=[])
    put(d, "bulk.json", fx.bulk_claims(60))
    code, out = run(d, "commit.py", "--claims", "bulk.json")
    assert code == 0, out
    assert len(jsonl(d, "claims.jsonl")) == 60
    # 第 61 条不再整批拒绝：60 是软线，只提示
    put(d, "one.json", fx.bulk_claims(1, start=60))
    code, out = run(d, "commit.py", "--claims", "one.json")
    assert code == 0, f"过了软线不该拦：\n{out}"
    assert len(jsonl(d, "claims.jsonl")) == 61
    # 但安全网还在：150 条之后必须拒绝
    rows = fx.bulk_claims(150)
    for i, r in enumerate(rows):
        r["id"] = f"c-{i + 1:03d}"
        r["since"] = TODAY
        r["supersedes"] = []
        r["status"] = "active"
    (d / "me" / "claims.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    put(d, "one2.json", fx.bulk_claims(1, start=999))
    code, out = run(d, "commit.py", "--claims", "one2.json")
    assert code == 1 and "安全网" in out, out


@case
def 和已有判断几乎一样会打回草稿():
    d = sandbox()
    seed(d, claims=fx.base_claims())          # c-001「干活靠手感，不看表」
    put(d, "c.json", [{"topic": "work", "type": "disposition",
                       "text": "干活靠手感，不看钟表",
                       "context": "他说每天凌晨四点到店里，先发面再烤第一批——看手感，不看时间。",
                       "source": "ai", "confidence": "inferred", "basis": ["f-0001"]}])
    code, out = run(d, "commit.py", "--claims", "c.json")
    assert code == 0, out
    assert "1 进草稿" in out, f"和已有判断一模一样的，该回草稿：\n{out}"
    assert "重合" in out, out


@case
def g8_没终态的原话整批拒绝():
    d = sandbox()
    put(d, "said.json", fx.said(3, 2))
    put(d, "f.json", fx.base_facts(TODAY))
    put(d, "c.json", fx.base_claims())
    code, out = run(d, "commit.py", "--said", "said.json",
                    "--facts", "f.json", "--claims", "c.json")
    assert code == 1, out
    assert "没有终态" in out, out
    assert jsonl(d, "facts.jsonl") == [], "有句子没终态时什么都不该写"
    assert jsonl(d, "transcript.jsonl") == [], "原话也不该先落盘"


# ---------------------------------------------------------------- 收场

@case
def 退役_不进正文但留痕():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    code, out = run(d, "commit.py", "--retire", "c-001", "--reason", "以后不开面包店了")
    assert code == 0, out
    run(d, "build.py")
    p = (d / "me" / "PORTRAIT.md").read_text(encoding="utf-8")
    assert "已退役 1 条" in p, p
    assert "### 干活靠手感，不看表" not in p, "退役的判断不该出现在正文"
    r = (d / "me" / "REVISIONS.md").read_text(encoding="utf-8")
    assert "已退役" in r and "以后不开面包店了" in r, r


@case
def 日历复核已废_新判断不带复核期():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    rows = jsonl(d, "claims.jsonl")
    assert rows and all("review_after" not in c for c in rows), rows
    code, out = run(d, "check.py")
    assert code == 0, out
    assert "到期" not in out, "不该再报到期的雪崩：\n" + out


# ---------------------------------------------------------------- 消化债

# ---------------------------------------------------------------- 一批收场

@case
def 事件_confirmed必须有确认事件():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    rows = {c["id"]: c for c in jsonl(d, "claims.jsonl")}
    assert all(c["confidence"] != "confirmed" for c in rows.values()), \
        "新判断不许自报 confirmed"
    code, out = run(d, "commit.py", "--confirm", "c-001",
                    "--reason", "本人看过，认这条", "--by", "user")
    assert code == 0, out
    rows = {c["id"]: c for c in jsonl(d, "claims.jsonl")}
    assert rows["c-001"]["confidence"] == "confirmed", rows
    ev = jsonl(d, "events.jsonl")
    assert any(e["kind"] == "confirm" and e["target"] == "c-001" for e in ev), ev
    code, out = run(d, "check.py")
    assert code == 0, out


@case
def 事件_capture禁ignored_没终态整批拒绝():
    d = sandbox()
    put(d, "bad.json", [{"text": "这句想顺手标掉", "ignored": "懒得记"}])
    code, out = run(d, "commit.py", "--capture", "bad.json")
    assert code == 1 and "只收纯文本原话" in out, out
    put(d, "said.json", ["每天凌晨四点到店里，先发面再烤第一批。",
                         "想自己开一家小店，但一直没算过账。"])
    code, out = run(d, "commit.py", "--capture", "said.json")
    assert code == 0 and "s-0001" in out, out
    put(d, "f.json", [{"date": TODAY, "kind": "自述", "asked": "最近在忙什么",
                       "text": "每天凌晨四点到店里，先发面再烤第一批。",
                       "topic": "work", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "f.json", "--from-capture", "1")
    assert code == 1 and "没有终态" in out, out
    assert jsonl(d, "facts.jsonl") == [], "有一句没落地就不该写"
    code, out = run(d, "commit.py", "--ignore", "s-0002", "--reason", "这句暂时不记")
    assert code == 0, out
    code, out = run(d, "commit.py", "--facts", "f.json", "--from-capture", "1")
    assert code == 0, out
    assert len(jsonl(d, "facts.jsonl")) == 1


# ---------------------------------------------------------------- 归档

# ---------------------------------------------------------------- 域

# ---------------------------------------------------------------- 性能

# ---------------------------------------------------------------- 措辞修正

@case
def 事实_选项编号会被挡进草稿():
    d = sandbox()
    put(d, "f.json", [{"date": TODAY, "kind": "自述",
                       "asked": "上周店里最忙那天先做哪件事", "text": "2B",
                       "topic": "work", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "f.json")
    assert code == 0, out
    assert "进草稿" in out and "选项编号" in out, out
    assert jsonl(d, "facts.jsonl") == [], "编号不该入库"
    assert len(jsonl(d, "drafts.jsonl")) == 1


@case
def 两阶段_对账单会打正面凭据():
    d = sandbox()
    put(d, "said.json", ["先发面再烤第一批", "骑车去河边坐着不做计划"])
    code, out = run(d, "commit.py", "--capture", "said.json")
    assert code == 0, out
    put(d, "f.json", [
        {"date": TODAY, "kind": "自述", "asked": "上周店里最忙那天先做哪件事",
         "text": "先发面再烤第一批", "topic": "work", "source": "user"},
        {"date": TODAY, "kind": "自述", "asked": "空下来的一下午怎么过",
         "text": "骑车去河边坐着不做计划", "topic": "leisure", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "f.json", "--from-capture", "1")
    assert code == 0, out
    assert "你说 2 句" in out and "2 句落地" in out, out
    assert "本轮没交 --said" not in out, f"两阶段不该报假警报：\n{out}"


def main():
    keep = "--keep" in sys.argv
    only_unit = "--unit" in sys.argv
    only_e2e = "--e2e" in sys.argv
    groups = []
    if not only_e2e:
        groups.append(("unit", UNIT))
    if not only_unit:
        groups.append(("e2e", CASES))
    started = time.time()
    ok, bad = 0, []
    for label, cases in groups:
        print(f"\n── {label}（{len(cases)} 例）──")
        for fn in cases:
            name = fn.__name__
            t0 = time.time()
            try:
                fn()
                print(f"✅ {name}  ({time.time() - t0:.2f}s)")
                ok += 1
            except AssertionError as e:
                print(f"❌ {name}  ({time.time() - t0:.2f}s)\n   {e}")
                bad.append(name)
            except Exception as e:                     # noqa: BLE001
                print(f"💥 {name}  ({time.time() - t0:.2f}s)\n   {type(e).__name__}: {e}")
                bad.append(name)
    total = sum(len(c) for _, c in groups)
    print(f"\n{ok} / {total} 通过 ｜ 总耗时 {time.time() - started:.1f}s"
          f" ｜ 起了 {CALLS[0]} 次子进程")
    if bad:
        print("失败：" + "、".join(bad))
    if not keep:
        for d in TMP:
            shutil.rmtree(d, ignore_errors=True)
    else:
        print("临时目录：" + "、".join(str(d) for d in TMP))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())

