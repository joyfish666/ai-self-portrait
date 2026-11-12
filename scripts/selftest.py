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

unit 直接 import 生产函数，0 子进程、约 1 秒出结果；
e2e 只留必须跑完整命令链的那些（整批预检、重渲染比对、git 提交、两阶段捕获、
写盘中断回滚、示例目录能否过体检）。
其余命令链用例已转成内存态单测：少起约 110 次子进程，全量约 2 分钟。
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
    assert not covered_by("这句话真的完全没有落到任何一条事实上面", ["别的原话"])


@unit
def unit_短答不再免检():
    """有内容的短回答漏记时，必须能被看出来。全是虚构数据。"""
    from store import covered_by
    assert not covered_by("在家补觉", []), "短句没落地不能再算已交代"
    assert not covered_by("在家补觉", ["骑车去河边坐着"])
    assert covered_by("在家补觉", ["在家补觉"]), "事实正文同样短也要能匹配上"
    assert covered_by("在家补觉", ["今天在家补觉，什么也没安排"])
    assert covered_by("", []), "空文本没有内容可追"


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
def unit_未来日期进草稿():
    """日期写成明年、十年后，都不是「已发生的事实」，必须挡在门外。"""
    from commit import take_fact
    F, _, _, rc = _mem()
    drafts, refmap = [], []
    take_fact({"date": "2099-01-01", "kind": "自述", "asked": "什么时候的事",
               "text": "一条日期写在未来的虚构事实", "topic": "work", "source": "user"},
              F, rc, drafts, refmap)
    assert len(F) == 3, "未来日期不该入库"
    assert drafts and drafts[0]["reason"] == "future_date", drafts


@unit
def unit_坏行报出文件与行号():
    """手改 JSON 漏引号，要看到「哪个文件第几行」，不是一页 traceback。"""
    import store
    d = Path(tempfile.mkdtemp(prefix="sp_bad_"))
    (d / "x.jsonl").write_text('{"a": 1}\n{broken\n', encoding="utf-8")
    old = store.ME
    store.ME = d
    try:
        try:
            store.load("x.jsonl")
        except ValueError as e:
            assert "x.jsonl" in str(e) and "第 2 行" in str(e), str(e)
        else:
            raise AssertionError("坏行没报错")
    finally:
        store.ME = old
        shutil.rmtree(d, ignore_errors=True)


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
def unit_冲突候选只筛同域判断():
    """筛范围只做四件事：同域、都是判断类、没登记过、不是重复。"""
    import tension
    base = [
        {"id": "c-001", "topic": "work", "type": "disposition",
         "text": "干活靠手感，不看表", "status": "active"},
        {"id": "c-002", "topic": "work", "type": "disposition",
         "text": "该定死时间的事绝不含糊", "status": "active"},
        {"id": "c-003", "topic": "work", "type": "state",
         "text": "最近在带两个新人", "status": "active"},
        {"id": "c-004", "topic": "leisure", "type": "disposition",
         "text": "周末不做任何计划", "status": "active"},
    ]
    pairs = {(a["id"], b["id"]) for a, b in tension.candidates(base, [])}
    assert pairs == {("c-001", "c-002")}, f"跨域和状态都不该进候选：{pairs}"

    assert not list(tension.candidates(base, [{"between": ["c-001", "c-002"]}])), \
        "登记过的对不该再冒出来"

    retired = [dict(c) for c in base]
    retired[0]["status"] = "retired"
    assert not list(tension.candidates(retired, [])), "退役的判断不参与"

    dup = [{"id": "c-005", "topic": "goals", "type": "disposition",
            "text": "想开店但一直没算过账", "status": "active"},
           {"id": "c-006", "topic": "goals", "type": "disposition",
            "text": "想开店，但一直没算过账。", "status": "active"}]
    assert not list(tension.candidates(dup, [])), "几乎一样的是重复，不是张力"

    assert len(list(tension.candidates(base, [], "work"))) == 1, "单域过滤"
    assert not list(tension.candidates(base, [], "leisure")), "一条判断凑不出对"


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
def 短答_没终态整批拒绝():
    """短回答不再免检：漏记一句就得整批停下来。全是虚构数据。"""
    d = sandbox()
    put(d, "said.json", ["在家补觉"])
    code, out = run(d, "commit.py", "--capture", "said.json")
    assert code == 0, out
    code, out = run(d, "commit.py", "--from-capture", "1")
    assert code == 1, out
    assert "没有终态" in out, out
    assert jsonl(d, "facts.jsonl") == [], "短答漏记时什么都不该写"


@case
def 短答_落了事实就算有交代():
    d = sandbox()
    put(d, "said.json", ["在家补觉"])
    code, out = run(d, "commit.py", "--capture", "said.json")
    assert code == 0, out
    put(d, "f.json", [{"date": TODAY, "kind": "自述",
                       "asked": "空下来的一下午怎么过", "text": "在家补觉",
                       "topic": "leisure", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "f.json", "--from-capture", "1")
    assert code == 0, out
    assert "你说 1 句" in out and "1 句落地" in out, out
    assert "太短" not in out, out
    assert len(jsonl(d, "facts.jsonl")) == 1


@case
def 短答_显式不记也算终态():
    d = sandbox()
    put(d, "said.json", ["在家补觉"])
    code, out = run(d, "commit.py", "--capture", "said.json")
    assert code == 0, out
    sid = jsonl(d, "transcript.jsonl")[0]["id"]
    code, out = run(d, "commit.py", "--ignore", sid,
                    "--reason", "纯日常，没有判断价值")
    assert code == 0, out
    code, out = run(d, "commit.py", "--from-capture", "1")
    assert code == 0, out
    assert "1 句标为无价值" in out and "0 句没落地" in out, out


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


@case
def 冲突候选只读不写():
    """筛范围是只读动作：列出同域判断的两两组合，不碰任何数据文件。"""
    d = sandbox()
    seed(d, claims=fx.base_claims())
    put(d, "c2.json", [{"topic": "work", "type": "disposition",
                        "text": "该定死的时间一个都不能赶",
                        "context": "他说每天凌晨四点到店里，先发面再烤第一批——几点进炉是有表的，一步不赶。",
                        "source": "ai", "basis": ["f-0001"]}])
    code, out = run(d, "commit.py", "--claims", "c2.json", "--no-git")
    assert code == 0, out
    before = (d / "me" / "tensions.jsonl").read_text(encoding="utf-8")
    code, out = run(d, "tension.py", "--candidates")
    assert code == 0, out
    assert "候选" in out and "×" in out, out
    assert (d / "me" / "tensions.jsonl").read_text(encoding="utf-8") == before, \
        "候选筛选不该写盘"
    code, out = run(d, "tension.py", "--candidates", "leisure")
    assert code == 0 and "没有待判" in out, out


@case
def 写盘中断会被拦住并能回滚():
    """十个文件顺序写，没有跨文件事务：中断后不能装作没事继续跑。"""
    d = sandbox()
    seed(d)
    old = (d / "me" / "facts.jsonl").read_text(encoding="utf-8")
    put(d, "f2.json", [{"date": TODAY, "kind": "自述", "asked": "第二问",
                        "text": "又一条虚构事实内容", "topic": "work", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "f2.json", "--no-git")
    assert code == 0, out
    # 手工摆一份「写到一半」的残留快照：只记 facts 的旧内容
    (d / "me" / ".pending.json").write_text(json.dumps(
        {"at": "2026-01-01T00:00:00+08:00", "files": {"facts.jsonl": old}},
        ensure_ascii=False), encoding="utf-8")
    code, out = run(d, "commit.py", "--facts", "f2.json", "--no-git")
    assert code == 1 and "pending" in out, out
    code, out = run(d, "check.py")
    assert code == 1 and "pending" in out, out
    code, out = run(d, "commit.py", "--recover")
    assert code == 0, out
    assert (d / "me" / "facts.jsonl").read_text(encoding="utf-8") == old
    assert not (d / "me" / ".pending.json").exists()
    code, out = run(d, "commit.py", "--facts", "f2.json", "--no-git")
    assert code == 0, out


@case
def 手机号会被体检盯上():
    """原话里留个手机号，check 不能一声不响。"""
    d = sandbox()
    put(d, "a.json", ["我电话是13800138000，下周联系我"])
    code, out = run(d, "commit.py", "--capture", "a.json", "--no-git")
    assert code == 0, out
    code, out = run(d, "check.py")
    assert "手机号" in out and "13800138000" in out, out


@case
def 补依据会重建视图():
    """suggest --apply 以前直接改 claims.jsonl，视图停在上一次 build。"""
    d = sandbox()
    seed(d, claims=fx.base_claims())
    code, out = run(d, "suggest.py", "--apply", "c-001", "f-0002")
    assert code == 0, out
    assert "加依据" in out, out
    code, out = run(d, "check.py")
    assert code == 0, f"补完依据视图没跟上：\n{out}"
    port = (d / "me" / "PORTRAIT.md").read_text(encoding="utf-8")
    assert "依据 2 条" in port, "视图里的依据条数没更新"


@case
def 示例目录能过体检():
    """examples/minimal 是公开示例，它自己必须过不了自己的体检就不配当示例。"""
    d = Path(tempfile.mkdtemp(prefix="sp_ex_"))
    TMP.append(d)
    shutil.copytree(ROOT / "scripts", d / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "examples" / "minimal", d / "me")
    code, out = run(d, "check.py")
    assert code == 0, f"示例自己过不了体检：\n{out}"


@case
def 改数据不重建会被体检抓到():
    """G2 不能只防手改视图：数据变了没 build，视图就是旧的，check 必须报。"""
    d = sandbox()
    seed(d)
    code, out = run(d, "commit.py", "--attach", "c-001:f-0002",
                    "--no-git", "--no-build")
    assert code == 0, out
    code, out = run(d, "check.py")
    assert code == 1, f"数据改了没重建视图，check 却说通过：\n{out}"
    assert "不一致" in out, out
    code, out = run(d, "build.py")
    assert code == 0, out
    code, out = run(d, "check.py")
    assert code == 0, f"build 之后仍不通过：\n{out}"


@case
def 两次捕获进同一轮():
    """capture 不推进 round：同一轮补交原话，不该被算成下一轮。"""
    d = sandbox()
    put(d, "a.json", ["先发面再烤第一批"])
    code, out = run(d, "commit.py", "--capture", "a.json", "--no-git")
    assert code == 0, out
    put(d, "b.json", ["骑车去河边坐着不做计划"])
    code, out = run(d, "commit.py", "--capture", "b.json", "--no-git")
    assert code == 0, out
    tr = jsonl(d, "transcript.jsonl")
    assert [t["round"] for t in tr] == [1, 1], f"两次捕获被算成两轮：{[t['round'] for t in tr]}"
    st = json.loads((d / "me" / "state.json").read_text(encoding="utf-8"))
    assert st.get("round") == 0 and st.get("pending_round") == 1, st
    put(d, "f.json", [
        {"date": TODAY, "kind": "自述", "asked": "上周店里最忙那天先做哪件事",
         "text": "先发面再烤第一批", "topic": "work", "source": "user"},
        {"date": TODAY, "kind": "自述", "asked": "空下来的一下午怎么过",
         "text": "骑车去河边坐着不做计划", "topic": "leisure", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "f.json", "--from-capture", "1",
                    "--no-git")
    assert code == 0, out
    assert "你说 2 句" in out and "2 句落地" in out, out
    st = json.loads((d / "me" / "state.json").read_text(encoding="utf-8"))
    assert st.get("round") == 1 and "pending_round" not in st, st


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

