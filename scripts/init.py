#!/usr/bin/env python3
"""初始化 me/ 目录：把 template/ 拷过去，建立断点，并让 me/ 自成一个 git 仓。

为什么要给 me/ 单独一个仓：
    根目录的 .gitignore 把 me/ 排除在外（防止个人数据被推到公开仓），
    所以 me/ 里的数据不会进根仓的历史。没有历史 = 改坏了没法回滚。
    内层仓不受外层 ignore 影响，于是"公开仓干净"和"数据有历史"能同时成立。
"""
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "template"
ME = ROOT / "me"
FILES = ["facts.jsonl", "claims.jsonl", "tensions.jsonl", "drafts.jsonl",
         "transcript.jsonl", "rounds.jsonl", "topics.jsonl", "narrows.jsonl",
         "state.json"]

VIEW_IGNORE = """# 视图由 build.py 生成，不进历史；改它们没有意义
PORTRAIT.md
CONTEXT.md
CHANGES.md
REVISIONS.md
.build.json
*.tmp
"""

GIT_ATTR = """# 数据一律 LF，别让 Windows 的 autocrlf 改坏 jsonl
* text=auto eol=lf
*.jsonl text eol=lf
"""


def has_data() -> bool:
    if not ME.is_dir():
        return False
    return any((ME / f).exists() and (ME / f).stat().st_size > 0
               for f in ("facts.jsonl", "claims.jsonl"))


def git_init():
    if not shutil.which("git"):
        print("⚠️  没找到 git：数据照常能用，但没有历史（无法回滚）")
        return False
    if (ME / ".git").is_dir():
        print("me/ 已经是 git 仓了")
        return False
    (ME / ".gitignore").write_text(VIEW_IGNORE, encoding="utf-8", newline="\n")
    (ME / ".gitattributes").write_text(GIT_ATTR, encoding="utf-8", newline="\n")
    ident = ["-c", "user.name=self-portrait", "-c", "user.email=portrait@localhost"]
    try:
        subprocess.run(["git"] + ident + ["init", "-q"], cwd=ME, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=ME,
                       capture_output=True)
        subprocess.run(["git"] + ident + ["add", "-A"], cwd=ME, check=True,
                       capture_output=True)
        subprocess.run(["git"] + ident + ["commit", "-q", "-m", "初始化画像"], cwd=ME,
                       check=True, capture_output=True)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"⚠️  git 初始化失败：{e}（数据仍在，只是没有历史）")
        return False
    print("✅ me/ 已自成一个 git 仓：每轮 commit 都有历史，改坏了能回滚")
    print("   备份 = 给这个仓加一个私有远端（git remote add origin …）")
    return True


def main() -> int:
    args = sys.argv[1:]
    force = "--force" in args
    git_only = "--git-only" in args

    if git_only:
        ME.mkdir(exist_ok=True)
        git_init()
        return 0

    if has_data() and not force:
        print("me/ 里已经有数据了。")
        print("  继续用 → 说『继续画像』")
        print("  补历史 → python scripts/init.py --git-only")
        print("  重建   → python scripts/init.py --force（会先备份）")
        return 1

    if force and ME.is_dir():
        bak = ROOT / "me.bak"
        if bak.exists():
            shutil.rmtree(bak)
        shutil.copytree(ME, bak)
        print(f"已备份到 {bak.name}/")

    ME.mkdir(exist_ok=True)
    for name in FILES:
        dst = ME / name
        if not dst.exists() or force:
            shutil.copyfile(TPL / name, dst)

    print("✅ 已初始化 me/")
    git_init()
    print("下一步：说『构建画像』，我会按 PROTOCOL.md 开始问。")
    return 0


if __name__ == "__main__":
    sys.exit(main())


