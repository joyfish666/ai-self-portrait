# 我的自画像

> 这是 `me/` 目录：**你的**数据。
> 它被根目录 `.gitignore` 排除（不会被推到公开仓），但**它自己是一个 git 仓**——每轮自动提交，改坏了能回滚。

## 里面有什么

| 文件 | 是什么 | 谁写 |
|---|---|---|
| `transcript.jsonl` | **你说过的每一句**（包括决定不记的） | `commit.py` |
| `facts.jsonl` | 从原话里摘出来的事实（只追加，不改写） | `commit.py` |
| `claims.jsonl` | 从事实里长出来的判断（判断 ≤60 / 状态 ≤40） | `commit.py`（代理提议，你确认） |
| `tensions.jsonl` | 还没解开的矛盾（≤10） | `commit.py` / `tension.py` |
| `drafts.jsonl` | 校验没过、暂存的东西（**不丢**） | `commit.py` |
| `rounds.jsonl` | 每轮长了多少：新增事实/判断/依据，按域 | `commit.py` |
| `topics.jsonl` | 你**跳过**或**暂缓**的域（为什么跳） | `commit.py` |
| `state.json` | 断点：第几轮、下一域是什么 | `commit.py` |
| `SYNTHESIS.md` | 综述：一段段人像，每段带 `[c-xxx]` 引用 | 代理写，`synth.py --stamp` 校验 |
| `PORTRAIT.md` | **人读版**画像（生成） | `build.py` |
| `CONTEXT.md` | **AI 读版**上下文（生成） | `build.py` |
| `CHANGES.md` / `REVISIONS.md` | 变化与修订（生成） | `build.py` |

## 怎么用

```
继续画像      → 接着上次的地方问
补充画像      → 只补最空的域
重建视图      → 改了 claims 之后重新渲染
记一下        → 立刻存档 + 出对账单
看草稿        → 看看哪些输入没通过校验
```

## 备份

```bash
cd me
git remote add origin <你的私有仓>
git push -u origin main
```

**别在根仓里 `git add me/`**——它被 ignore 了，加不进去。

## 别做

- 别手改 `*.jsonl`——写入只走 `python scripts/commit.py`（手编的 id 会撞，手写的日期会错）
- 别手改 `PORTRAIT.md` / `CONTEXT.md`——会被覆盖，而且 `check` 会报「视图被手改」
- 别把 `me/` 推到**公开**仓库
- 别主动追问别人的隐私；你自己说到的，照实记就行（只存本地）
