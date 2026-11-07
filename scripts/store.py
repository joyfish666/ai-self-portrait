"""共用 I/O 与机械工具：原子写、JSONL 读写、id 分配、片段校验。

这里只放"确定性"的东西——没有语义判断，没有 LLM。
"""
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ME = ROOT / "me"

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FRAG = 8


def load(name):
    """读 me/<name>，坏行报错由 check 负责；这里坏行直接抛。"""
    p = ME / name
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def load_any(path):
    """读待写入的东西：JSON 数组 / JSONL / 单个对象，都接受。"""
    p = Path(path)
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    if raw.startswith("["):
        return json.loads(raw)
    if raw.startswith("{"):
        # 可能是单行 JSON，也可能是多行 JSONL
        try:
            return [json.loads(raw)]
        except json.JSONDecodeError:
            return [json.loads(l) for l in raw.splitlines() if l.strip()]
    raise ValueError(f"{path} 既不是 JSON 数组也不是 JSONL")


def atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def dump_jsonl(path, rows):
    atomic_write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def today_iso():
    return date.today().isoformat()


def to_date(s):
    try:
        return date.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


def add_days(iso, days):
    d = to_date(iso)
    return (d + timedelta(days=days)).isoformat() if d else None


def next_id(rows, prefix, width=4):
    n = 0
    pat = re.compile(re.escape(prefix) + r"-(\d+)$")
    for r in rows:
        m = pat.match(str(r.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}-{n + 1:0{width}d}"


def has_fragment(text, src, frag=FRAG):
    if not text or not src:
        return False
    for i in range(0, max(1, len(src) - frag + 1)):
        if src[i:i + frag] in text:
            return True
    return False


def age_days(iso, today=None):
    d = to_date(iso)
    t = to_date(today or today_iso())
    if not d or not t:
        return None
    return (t - d).days


def age_text(days):
    """把天数说成人话：今天 / 3天前 / 5月前 / 2年前。"""
    if days is None:
        return ""
    if days <= 0:
        return "今天"
    if days < 30:
        return f"{days}天前"
    if days < 365:
        return f"{days // 30}月前"
    return f"{days // 365}年前"


def covered_by(text, fact_texts, min_len=FRAG):
    """这句话有没有落到某条事实上：短的一方要够长，且互为子串。"""
    t = "".join(str(text).split())
    if len(t) < min_len:
        return True          # 太短，本来就不是内容
    for ft in fact_texts:
        f = "".join(str(ft).split())
        if len(f) >= min_len and (f in t or t in f):
            return True
    return False


STOP = set("的了是我你他她在和就不都也很还要会能对这个那个什么怎么因为所以但是而且如果就是一样")
_PUNCT = re.compile(r"[\s，。；：、？！“”‘’（）()\-—…|·]+")


def bigrams(text):
    t = _PUNCT.sub("", str(text))
    t = "".join(ch for ch in t if ch not in STOP)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def overlap(a, b):
    """字面重合：返回 (Jaccard 分值, 共享 bigram 数)。只做召回，不做判断。"""
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0, 0
    shared = len(A & B)
    return shared / len(A | B), shared


def file_hash(path):
    import hashlib
    p = Path(path)
    if not p.exists():
        return None
    return hashlib.md5(p.read_bytes()).hexdigest()[:8]
