"""测试用的虚构数据。

这里没有一行来自真实用户：人物、事件、日期全是编的。
日期相对"今天"生成，所以测试不会随时间过期。

人物：一个在面包店当学徒的虚构人物。
"""


def base_facts(today):
    return [
        {"ref": "a", "date": today, "kind": "自述", "asked": "最近在忙什么",
         "text": "每天凌晨四点到店里，先发面再烤第一批。", "topic": "work", "source": "user"},
        {"ref": "b", "date": today, "kind": "自述", "asked": "最想做的一件事是什么",
         "text": "想自己开一家小店，但一直没算过账。", "topic": "goals", "source": "user"},
        {"ref": "c", "date": today, "kind": "自述", "asked": "周末一般怎么过",
         "text": "周末骑车去河边坐一下午，不做计划。", "topic": "leisure", "source": "user"},
    ]


def base_claims():
    return [
        {"ref": "c1", "topic": "work", "type": "disposition",
         "text": "干活靠手感，不看表",
         "context": "他说每天凌晨四点到店里，先发面再烤第一批——发到什么时候看手感，不看时间。",
         "source": "ai", "confidence": "inferred", "basis": ["a"]},
        {"ref": "c2", "topic": "goals", "type": "disposition",
         "text": "想开店，但没算过账",
         "context": "他说想自己开一家小店，但一直没算过账——卡在账本那一步，不是不会做面包。",
         "source": "ai", "confidence": "inferred", "basis": ["b"]},
        {"ref": "c3", "topic": "leisure", "type": "state",
         "text": "周末骑车去河边，不做计划",
         "context": "他说周末骑车去河边坐一下午，不做计划——休息也是随手安排的，不提前想。",
         "source": "ai", "confidence": "inferred", "basis": ["c"]},
    ]


def bulk_claims(n, start=0, fact_text="每天凌晨四点到店里"):
    """n 条互不相同的占位判断：用来测上限和耗时，不参与语义断言。"""
    out = []
    for i in range(start, start + n):
        out.append({
            "topic": "work", "type": "disposition",
            "text": f"占位判断第{i}号，讲工作里的第{i}种情形",
            "context": f"他说{fact_text}，先发面再烤第一批——这是第{i}条占位说明，只为凑数量和耗时。",
            "source": "ai", "confidence": "inferred", "basis": ["f-0001"],
        })
    return out


def said(n_landed=3, n_extra=2):
    """几句虚构原话：前 n_landed 句会落到事实，多出来的故意不落。"""
    lines = [
        "每天凌晨四点到店里，先发面再烤第一批。",
        "想自己开一家小店，但一直没算过账。",
        "周末骑车去河边坐一下午，不做计划。",
        "店里上个月换过一次烤箱。",
        "隔壁那家店的面包卖得比我们贵。",
        "师傅说我再练两年才谈得上开店。",
    ]
    return lines[:n_landed + n_extra]
