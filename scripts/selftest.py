#!/usr/bin/env python3
"""自测：把 SPEC §8 的验收变成能跑的断言。

用法
    python scripts/selftest.py          # 跑一遍，全绿返回 0
    python scripts/selftest.py --keep   # 保留临时目录，方便看哪里坏了

每个 case 在**临时目录**里从零建一个仓（只拷 scripts/ 和 template/），
用 scripts/_fixtures.py 里的**虚构数据**跑，不碰 me/，也不需要任何个人数据。
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
CALLS = [0]


def case(fn):
    CASES.append(fn)
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
def g1_缺_asked_进草稿():
    d = sandbox()
    put(d, "f.json", [{"date": TODAY, "kind": "自述", "text": "这句没写当时问的是什么。",
                       "topic": "work", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "f.json")
    assert code == 0, out
    assert "缺 asked" in out, out
    assert jsonl(d, "facts.jsonl") == []


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
def g6_第61条整批拒绝():
    d = sandbox()
    seed(d, facts=fx.base_facts(TODAY)[:1], claims=[])
    put(d, "bulk.json", fx.bulk_claims(60))
    code, out = run(d, "commit.py", "--claims", "bulk.json")
    assert code == 0, out
    assert len(jsonl(d, "claims.jsonl")) == 60
    put(d, "one.json", fx.bulk_claims(1, start=60))
    code, out = run(d, "commit.py", "--claims", "one.json")
    assert code == 1 and "超过上限" in out, out
    assert len(jsonl(d, "claims.jsonl")) == 60, "拒绝时不该写入任何东西"


@case
def g4_过期会标出等待符():
    d = sandbox()
    old = days_ago(400)
    put(d, "f.json", [{"date": old, "kind": "自述", "asked": "最近在忙什么",
                       "text": "那时候每天凌晨四点到店里，先发面再烤第一批。",
                       "topic": "work", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "f.json")
    assert code == 0, out
    put(d, "c.json", [{"topic": "work", "type": "disposition",
                       "text": "靠手感判断发面",
                       "context": "他说那时候每天凌晨四点到店里，先发面再烤第一批——发到什么时候看手感。",
                       "source": "ai", "confidence": "inferred",
                       "since": old, "basis": ["f-0001"]}])
    code, out = run(d, "commit.py", "--claims", "c.json")
    assert code == 0, out
    p = (d / "me" / "PORTRAIT.md").read_text(encoding="utf-8")
    assert "⏳" in p, "过期的判断应该带 ⏳"


@case
def g8_对账单与漏记():
    d = sandbox()
    put(d, "said.json", fx.said(3, 2))
    put(d, "f.json", fx.base_facts(TODAY))
    put(d, "c.json", fx.base_claims())
    code, out = run(d, "commit.py", "--said", "said.json",
                    "--facts", "f.json", "--claims", "c.json")
    assert code == 0, out
    assert "0 丢失" in out, out
    assert "你说 5 句" in out and "2 句没落地" in out, out
    assert len(jsonl(d, "transcript.jsonl")) == 5


@case
def g8_剔除悬空依据不会算成负数():
    d = sandbox()
    seed(d, claims=[])
    put(d, "c.json", [{"topic": "work", "type": "state",
                       "text": "早上四点到店",
                       "context": "他说每天凌晨四点到店里，先发面再烤第一批，所以作息一直是早班，晚上很早就睡。",
                       "source": "ai", "confidence": "inferred",
                       "basis": ["f-0001", "f-9999"]}])
    code, out = run(d, "commit.py", "--claims", "c.json")
    assert code == 0, out
    assert "0 丢失" in out, f"不该出现负数丢失：\n{out}"
    assert "剔除了悬空依据" in out, out


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
def 续期_把复核期往后推():
    d = sandbox()
    old = days_ago(60)
    put(d, "f.json", [{"date": old, "kind": "自述", "asked": "周末一般怎么过",
                       "text": "周末骑车去河边坐一下午，不做计划。",
                       "topic": "leisure", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "f.json")
    assert code == 0, out
    put(d, "c.json", [{"topic": "leisure", "type": "state",
                       "text": "周末骑车去河边，不做计划",
                       "context": "他说周末骑车去河边坐一下午，不做计划——休息也是随手安排的。",
                       "source": "ai", "confidence": "inferred",
                       "since": old, "basis": ["f-0001"]}])
    code, out = run(d, "commit.py", "--claims", "c.json")
    assert code == 0, out
    before = {c["id"]: c.get("review_after") for c in jsonl(d, "claims.jsonl")}
    code, out = run(d, "commit.py", "--renew", "c-001", "--reason", "现在还是这样")
    assert code == 0, out
    after = {c["id"]: c.get("review_after") for c in jsonl(d, "claims.jsonl")}
    assert after["c-001"] != before["c-001"], "续期应当改变复核期"
    assert any(c.get("renewed_reason") for c in jsonl(d, "claims.jsonl"))


@case
def 张力没关不能取代():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    code, out = run(d, "tension.py", "--add", "c-001", "c-002", "两条都真，但方向相反")
    assert code == 0, out
    merge = [{"topic": "work", "type": "disposition",
              "text": "靠手感，也怕算账",
              "context": "他说每天凌晨四点到店里，先发面再烤第一批，同时想自己开一家小店但没算过账。",
              "source": "ai", "confidence": "inferred",
              "basis": ["f-0001", "f-0002"],
              "supersedes": ["c-001"], "reason": "合并成一句"}]
    put(d, "m.json", merge)
    code, out = run(d, "commit.py", "--claims", "m.json")
    assert "先 tension.py --close" in out, out
    code, out = run(d, "tension.py", "--close", "t-001", "--reason", "想过之后不冲突")
    assert code == 0, out
    code, out = run(d, "commit.py", "--claims", "m.json")
    assert code == 0, out
    p = (d / "me" / "PORTRAIT.md").read_text(encoding="utf-8")
    assert "曾经认为" in p, p


# ---------------------------------------------------------------- 消化债

@case
def 债_25天会给出候选():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    put(d, "old.json", [{"date": days_ago(25), "kind": "自述", "asked": "最近在忙什么",
                         "text": "那阵子也是每天凌晨四点到店里干活。",
                         "topic": "work", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "old.json")
    assert code == 0, out
    assert "最老 25 天" in out, out
    assert "可能能挂上" in out, out


@case
def 债_35天会点名():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    put(d, "old.json", [{"date": days_ago(35), "kind": "自述", "asked": "最近在忙什么",
                         "text": "上个月店里换了新的烤箱，温度不太稳。",
                         "topic": "work", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "old.json")
    assert code == 0, out
    assert "🔔" in out and "三选一" in out, out



@case
def 退役后依据会掉回债里():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    code, out = run(d, "commit.py", "--retire", "c-001", "--reason", "这条不用了")
    assert code == 0, out
    assert "没人用了" in out, out
    code, out = run(d, "check.py")
    assert "未消化 1" in out, out


@case
def 取代会报出被丢下的依据():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    put(d, "c.json", [{"topic": "work", "type": "disposition",
                       "text": "靠手感，也怕算账",
                       "context": "他说想自己开一家小店，但一直没算过账——手艺是有的，卡在算账这一步，不是不会做面包。",
                       "source": "ai", "confidence": "inferred",
                       "basis": ["f-0002"], "supersedes": ["c-001"],
                       "reason": "合并成一句更准的"}])
    code, out = run(d, "commit.py", "--claims", "c.json")
    assert code == 0, out
    assert "没人用了" in out, out
    assert "f-0001" in out, out


# ---------------------------------------------------------------- 一批收场

@case
def 批量续期只出一份对账单():
    d = sandbox()
    seed(d)                       # 先有事实，否则判断会因依据悬空进草稿
    old = days_ago(150)           # 复核期已经过了 60 天，续期才看得出变化
    put(d, "c.json", [{"topic": "work", "type": "state",
                       "text": f"占位状态第{i}条",
                       "context": f"他说每天凌晨四点到店里，先发面再烤第一批——第{i}条占位说明。",
                       "source": "ai", "confidence": "inferred",
                       "since": old, "basis": ["f-0001"]} for i in range(1, 4)])
    code, out = run(d, "commit.py", "--claims", "c.json")
    assert code == 0, out
    before = {c["id"]: c.get("review_after") for c in jsonl(d, "claims.jsonl")}
    assert len(before) == 3 and all(v < TODAY for v in before.values()), before
    code, out = run(d, "commit.py", "--renew", "c-001,c-002,c-003", "--reason", "现在还是这样")
    assert code == 0, out
    assert out.count("续期到") == 3, f"三条应各有一行续期：\n{out}"
    after = {c["id"]: c.get("review_after") for c in jsonl(d, "claims.jsonl")}
    for cid in ("c-001", "c-002", "c-003"):
        assert after[cid] != before[cid], f"{cid} 的复核期应当往后推"
    assert all(c.get("renewed_reason") == "现在还是这样" for c in jsonl(d, "claims.jsonl"))


@case
def 批量退役一个理由带走一批():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    code, out = run(d, "commit.py", "--retire", "c-001,c-002", "--reason", "这两条都不认了")
    assert code == 0, out
    rows = {c["id"]: c for c in jsonl(d, "claims.jsonl")}
    assert rows["c-001"]["status"] == "retired" and rows["c-002"]["status"] == "retired", rows
    assert rows["c-003"]["status"] == "active", "没点名的不该被动"
    assert rows["c-001"]["retired_reason"] == "这两条都不认了"


@case
def 到期只摆N条剩下排队():
    d = sandbox()
    seed(d)                      # 先有事实，否则依据悬空会全进草稿
    old = days_ago(150)          # state +90 天 → 逾期 60 天
    put(d, "c.json", [{"topic": "work", "type": "state",
                       "text": f"占位状态第{i}条",
                       "context": f"他说每天凌晨四点到店里，先发面再烤第一批——第{i}条占位说明。",
                       "source": "ai", "confidence": "inferred",
                       "since": old, "basis": ["f-0001"]} for i in range(1, 5)]
                 + [{"topic": "identity", "type": "constant",
                     "text": "占位常量：虚构生日",
                     "context": "他说每天凌晨四点到店里，先发面再烤第一批——这条只是背景。",
                     "source": "ai", "confidence": "inferred", "basis": ["f-0001"]}])
    code, out = run(d, "commit.py", "--claims", "c.json")
    assert code == 0, out
    code, out = run(d, "commit.py", "--due", "2")
    assert code == 0, out
    assert "constant" not in out, f"常量没有复核期，不该出现在到期里：\n{out}"
    assert "今天先看 2 条" in out, out
    assert "剩下 2 条排着" in out, f"该说清还有多少没轮到：\n{out}"
    assert out.count("逾期 60 天") == 2, out
    assert "--renew c-001,c-002" in out, f"该教批量写法：\n{out}"
    code, out = run(d, "commit.py", "--due")
    assert "今天先看 3 条" in out and "剩下 1 条排着" in out, f"默认摆 3 条：\n{out}"


@case
def 债攒多了不会闭嘴():
    d = sandbox()
    seed(d)                      # 3 条事实，没有判断
    old = days_ago(35)
    put(d, "more.json", [{"date": old, "kind": "自述", "asked": "最近在忙什么",
                          "text": f"虚构第 {i} 条：店里那台旧烤箱又响了一声。",
                          "topic": "work", "source": "user"} for i in range(30)])
    code, out = run(d, "commit.py", "--facts", "more.json")
    assert code == 0, out
    code, out = run(d, "check.py")
    assert "攒得有点多" in out, f"33 条债该说一句：\n{out}"
    assert "三选一" in out, f"最老那条该点名：\n{out}"


# ---------------------------------------------------------------- 归档

@case
def 改归档只动一个字段不制造版本噪音():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    before = jsonl(d, "claims.jsonl")
    code, out = run(d, "commit.py", "--retopic", "c-002:values",
                    "--reason", "这是一句价值判断，不该放在目标里")
    assert code == 0, out
    assert "goals → values" in out, out
    assert "1 改归档" in out, f"改归档要进对账单的计数，不能算成丢失：\n{out}"
    assert "1 丢失" not in out, out
    rows = {c["id"]: c for c in jsonl(d, "claims.jsonl")}
    assert rows["c-002"]["topic"] == "values", rows["c-002"]
    assert rows["c-002"]["status"] == "active", "改归档不该让判断退出在役"
    assert rows["c-002"]["retopied_reason"] == "这是一句价值判断，不该放在目标里"
    assert len(rows) == len(before), f"改归档不该新增判断：{len(before)} → {len(rows)}"
    assert not any(c.get("supersedes") for c in rows.values()), "不该出现取代关系"
    # 画像要跟着搬
    run(d, "build.py")
    p = (d / "me" / "PORTRAIT.md").read_text(encoding="utf-8")
    assert "想开店，但没算过账" in p, "搬完还在画像里"


@case
def 改归档必须写理由():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    code, out = run(d, "commit.py", "--retopic", "c-002:values")
    assert code != 0, "没写理由应当拒绝"
    assert "必须带 --reason" in out, out


@case
def 依据全来自干活现场会问一句():
    d = sandbox()
    seed(d)                                   # 先有基准事实
    put(d, "f2.json", [{"ref": "x", "date": TODAY, "kind": "行为",
                        "asked": "我提了三种做法让他挑",
                        "text": "你别给我选项，直接说你推荐哪个。",
                        "topic": "work", "source": "user"}])
    code, out = run(d, "commit.py", "--facts", "f2.json")
    assert code == 0, out
    fid = [f for f in jsonl(d, "facts.jsonl") if f["text"].startswith("你别给我选项")][0]["id"]
    # 同一条依据，落在 work 里不该问（工作场合的行为撑得起工作判断）
    put(d, "c1.json", [{"topic": "work", "type": "disposition",
                        "text": "要他表态，不要一堆选项",
                        "context": "他说你别给我选项，直接说你推荐哪个——选项摆出来还不够，还要有人给个倾向。",
                        "source": "ai", "confidence": "inferred", "basis": [fid]}])
    code, out = run(d, "commit.py", "--claims", "c1.json")
    assert code == 0, out
    assert "1 进草稿" not in out, f"这条本身该合格：\n{out}"
    assert "写高了" not in out, f"work 不该触发这条提示：\n{out}"
    # 同一句话硬提到 values，就该问一句
    put(d, "c2.json", [{"topic": "values", "type": "disposition",
                        "text": "要人承担判断，不接受推回来",
                        "context": "他说你别给我选项，直接说你推荐哪个——从这里推到他要人承担判断，跳了一步。",
                        "source": "ai", "confidence": "inferred", "basis": [fid]}])
    code, out = run(d, "commit.py", "--claims", "c2.json")
    assert code == 0, out
    assert "1 进草稿" not in out, f"这条本身该合格：\n{out}"
    assert "写高了" in out, f"依据全来自干活现场却落在 values，应当问一句：\n{out}"
    assert "还有别的场合" in out, out


# ---------------------------------------------------------------- 域

@case
def 采集约定进CONTEXT不进画像():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    put(d, "c.json", [{"topic": "interface", "type": "disposition",
                       "text": "别问家里的事",
                       "context": "他说每天凌晨四点到店里，先发面再烤第一批——家里的事不想聊。",
                       "source": "ai", "confidence": "inferred", "basis": ["f-0001"]}])
    code, out = run(d, "commit.py", "--claims", "c.json")
    assert code == 0, out
    portrait = (d / "me" / "PORTRAIT.md").read_text(encoding="utf-8")
    context = (d / "me" / "CONTEXT.md").read_text(encoding="utf-8")
    assert "别问家里的事" not in portrait, "interface 不该进画像正文"
    assert "别问家里的事" in context, "interface 必须进 CONTEXT，否则下次接着建的人不知道"
    # 也不能算进完整度
    assert "不计入完整度" in portrait, portrait[:400]


@case
def 新域自动进完整度分母():
    d = sandbox()
    code, out = run(d, "build.py")
    assert code == 0, out
    p = (d / "me" / "PORTRAIT.md").read_text(encoding="utf-8")
    assert "覆盖 0/9" in p, f"分母应当跟着 DOMAINS 走，不该写死：\n{p[:300]}"


@case
def 撤依据不能把判断掏空():
    d = sandbox()
    seed(d, claims=fx.base_claims())      # c-001 原本只有 f-0001 一条依据
    code, out = run(d, "commit.py", "--attach", "c-001:f-0002,f-0003")
    assert code == 0, out
    code, out = run(d, "commit.py", "--detach", "c-001:f-0002,f-0003")
    assert code == 0, out
    rows = {c["id"]: c for c in jsonl(d, "claims.jsonl")}
    assert rows["c-001"]["basis"] == ["f-0001"], rows["c-001"]["basis"]
    assert "撤依据" in out, out
    # 撤掉最后一条：不是接错线，是这条判断没证据了 —— 必须拒绝
    code, out = run(d, "commit.py", "--detach", "c-001:f-0001")
    rows = {c["id"]: c for c in jsonl(d, "claims.jsonl")}
    assert rows["c-001"]["basis"] == ["f-0001"], f"撤空被拒后依据不该动：{rows['c-001']['basis']}"
    assert "一条依据都不剩" in out, out


@case
def 换依据要连context一起换():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    put(d, "r.json", [{"id": "c-001", "add": ["f-0002"], "del": ["f-0001"],
                       "context": "他说想自己开一家小店，但一直没算过账——所以先看钱，不看手感。",
                       "reason": "原来的依据接错了"}])
    code, out = run(d, "commit.py", "--rewire", "r.json")
    assert code == 0, out
    rows = {c["id"]: c for c in jsonl(d, "claims.jsonl")}
    assert rows["c-001"]["basis"] == ["f-0002"], rows["c-001"]["basis"]
    assert rows["c-001"]["rewired_reason"] == "原来的依据接错了"
    assert "换依据" in out, out


@case
def 换依据时context必须引用新依据():
    d = sandbox()
    seed(d, claims=fx.base_claims())
    bad = "这句话跟两条依据都没关系，纯粹是一段用来测试校验的说明文字。"
    put(d, "r.json", [{"id": "c-001", "add": ["f-0002"], "del": [], "context": bad, "reason": "测"}])
    code, out = run(d, "commit.py", "--rewire", "r.json")
    rows = {c["id"]: c for c in jsonl(d, "claims.jsonl")}
    assert code == 0, out
    assert rows["c-001"]["context"] != bad, "不合格的 context 不该被写进去"
    assert "没引用任何新依据的原话" in out, out


@case
def 观察也要写asked():
    d = sandbox()
    seed(d)
    put(d, "f2.json", [{"ref": "x", "date": TODAY, "kind": "AI观察",
                        "text": "他当天交出了一页纸。", "topic": "work", "source": "ai"}])
    code, out = run(d, "commit.py", "--facts", "f2.json")
    assert code == 0, out
    assert "1 进草稿" in out, f"没写 asked 的观察该进草稿：\n{out}"
    assert not [f for f in jsonl(d, "facts.jsonl") if f.get("kind") == "AI观察"], \
        "不该有任何 AI观察 落库"


@case
def 依据全是我观察到的会报一句():
    d = sandbox()
    seed(d)
    put(d, "f2.json", [{"ref": "o", "date": TODAY, "kind": "AI观察",
                        "asked": "我在看他做的东西", "text": "他当天交出了一页纸。",
                        "topic": "work", "source": "ai"}])
    code, out = run(d, "commit.py", "--facts", "f2.json")
    assert code == 0, out
    fid = [f for f in jsonl(d, "facts.jsonl") if f.get("kind") == "AI观察"][0]["id"]
    put(d, "c.json", [{"topic": "work", "type": "disposition",
                       "text": "他做事要一页纸那么大",
                       "context": "他当天交出了一页纸，从背景、难点、设计一直写到复盘——尺寸决定他动不动手。",
                       "source": "ai", "confidence": "inferred", "basis": [fid]}])
    code, out = run(d, "commit.py", "--claims", "c.json")
    assert code == 0, out
    code, out = run(d, "check.py")
    assert "没有一句他自己的话" in out, f"只有观察当依据，该报一句：\n{out}"


@case
def 观察配上原话就不再报():
    d = sandbox()
    seed(d)
    put(d, "f2.json", [{"ref": "o", "date": TODAY, "kind": "AI观察",
                        "asked": "我在看他做的东西", "text": "他当天交出了一页纸。",
                        "topic": "work", "source": "ai"}])
    code, out = run(d, "commit.py", "--facts", "f2.json")
    assert code == 0, out
    fid = [f for f in jsonl(d, "facts.jsonl") if f.get("kind") == "AI观察"][0]["id"]
    put(d, "c.json", [{"topic": "work", "type": "disposition",
                       "text": "他做事要一页纸那么大",
                       "context": "他当天交出了一页纸，还说先发面再烤第一批——尺寸决定他动不动手。",
                       "source": "ai", "confidence": "inferred", "basis": [fid, "f-0001"]}])
    code, out = run(d, "commit.py", "--claims", "c.json")
    assert code == 0, out
    code, out = run(d, "check.py")
    assert "没有一句他自己的话" not in out, f"有他的原话就不该报：\n{out}"


# ---------------------------------------------------------------- 性能

@case
def g7_120条不会退化成分钟级():
    d = sandbox()
    put(d, "f.json", fx.base_facts(TODAY)[:1])
    code, out = run(d, "commit.py", "--facts", "f.json")
    assert code == 0, out
    # 性能测试要超过 60 条，只能绕过闸门直接写盘（这里测的是 check 的速度，不是上限）
    rows = fx.bulk_claims(120)
    for i, r in enumerate(rows):
        r["id"] = f"c-{i + 1:03d}"
        r["since"] = TODAY
        r["review_after"] = (date.today() + timedelta(days=365)).isoformat()
        r["supersedes"] = []
        r["status"] = "active"
    (d / "me" / "claims.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")
    run(d, "build.py")
    t0 = time.time()
    run(d, "check.py")
    cost = time.time() - t0
    # 这个数含了解释器启动（Windows 上 0.3–0.6s），所以它不是基准测试。
    # 3 秒只抓一件事：check 别退化成"每条判断扫一遍全部事实"那种平方级实现。
    assert cost < 3.0, f"check 用了 {cost:.2f}s，怀疑退化成了平方级"


def main():
    keep = "--keep" in sys.argv
    started = time.time()
    ok, bad = 0, []
    for fn in CASES:
        name = fn.__name__
        t0 = time.time()
        try:
            fn()
            print(f"✅ {name}  ({time.time() - t0:.1f}s)")
            ok += 1
        except AssertionError as e:
            print(f"❌ {name}  ({time.time() - t0:.1f}s)\n   {e}")
            bad.append(name)
        except Exception as e:                     # noqa: BLE001
            print(f"💥 {name}  ({time.time() - t0:.1f}s)\n   {type(e).__name__}: {e}")
            bad.append(name)
    print(f"\n{ok} / {len(CASES)} 通过 ｜ 总耗时 {time.time() - started:.1f}s"
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

