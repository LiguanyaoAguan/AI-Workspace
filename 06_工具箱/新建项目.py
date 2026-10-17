#!/usr/bin/env python3
"""在 AI 工坊中新建项目。默认仅预览，--confirm 才写入。

写入前会先调用 快照.py 保存项目总表；快照失败则整体停止，不创建任何文件。
"""
from __future__ import annotations

import argparse
import functools
import subprocess
import sys
from datetime import date
from pathlib import Path

BACKSLASH = chr(92)
NEWLINE = chr(10)

HARD_MARKERS = ("00_前台", "01_项目库", "01_项目库/项目总表.md", "06_工具箱")
SOFT_MARKERS = ("02_知识库", "03_技能库", "04_素材库", "05_归档",
                "AGENTS.md", "README.md", "06_工具箱/模板")


def find_root(start):
    start = Path(start).resolve()
    for candidate in [start, *start.parents]:
        if not candidate.is_dir():
            continue
        if not all((candidate / marker).exists() for marker in HARD_MARKERS):
            continue
        if sum(1 for marker in SOFT_MARKERS if (candidate / marker).exists()) >= 2:
            return candidate
    print("停止：无法定位工作区根目录（结构不完整）", file=sys.stderr)
    raise SystemExit(1)


ROOT = find_root(Path(__file__).resolve().parent)
PROJECTS = ROOT / "01_项目库"
TEMPLATES = ROOT / "06_工具箱" / "模板"
SNAPSHOT_TOOL = ROOT / "06_工具箱" / "快照.py"
INDEX = PROJECTS / "项目总表.md"


def valid_name(name):
    return bool(name and name not in (".", "..") and not name.startswith(".")
                and "/" not in name and BACKSLASH not in name and "|" not in name
                and all(ord(c) >= 32 and ord(c) != 127 for c in name))


def normalize_keywords(text):
    """规范化关键词：支持中文逗号/顿号/分号，去重，禁止表格分隔符。"""
    if not text:
        return ""
    text = str(text).replace("，", ",").replace("、", ",")
    text = text.replace("；", ",").replace(";", ",")
    parts = []
    for part in text.split(","):
        value = part.strip().replace("|", "／")
        if value and value not in parts:
            parts.append(value)
    return ", ".join(parts)[:80]


def snapshot_index(name):
    """写前 begin：返回操作编号；失败返回 None，此时尚未写入任何文件。"""
    if not SNAPSHOT_TOOL.is_file():
        print("停止：找不到快照工具 " + str(SNAPSHOT_TOOL), file=sys.stderr)
        return None
    try:
        result = subprocess.run(
            [sys.executable, str(SNAPSHOT_TOOL), "begin", "--kind", "edit",
             "--files", "01_项目库/项目总表.md",
             "--reason", "新建项目前置快照：" + name],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        )
    except OSError as exc:
        print("停止：调用快照工具失败：" + str(exc), file=sys.stderr)
        return None
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        print("停止：写前快照失败，未创建任何文件、未修改项目总表。", file=sys.stderr)
        return None
    for line in result.stdout.splitlines():
        if line.startswith("操作编号："):
            return line.split("：", 1)[1].strip()
    print("停止：未能取得操作编号，未创建任何文件。", file=sys.stderr)
    return None


def finish_op(op_id):
    """写后 end：校验并推进基线。"""
    try:
        result = subprocess.run([sys.executable, str(SNAPSHOT_TOOL), "end", op_id],
                                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    except OSError as exc:
        print("警告：调用 end 失败：" + str(exc), file=sys.stderr)
        return False
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        print("警告：end 校验失败，总表写入需人工复核。", file=sys.stderr)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="创建项目（默认只预览）")
    parser.add_argument("name")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--keywords", default="", help="逗号分隔的关键词，可省略")
    args = parser.parse_args()
    name = args.name.strip()
    if not valid_name(name):
        parser.error("项目名称为空或包含不允许的字符")
    keywords = normalize_keywords(args.keywords) or "待确认"

    target = PROJECTS / name
    try:
        target.resolve().relative_to(PROJECTS.resolve())
    except (OSError, ValueError):
        print("停止：项目路径超出项目库范围", file=sys.stderr)
        return 1
    if target.exists() or target.is_symlink():
        print("停止：目标已存在：" + str(target), file=sys.stderr)
        return 1
    if not INDEX.is_file():
        print("停止：项目总表不存在", file=sys.stderr)
        return 1

    mapping = {"README.md": "项目README模板.md",
               "任务与进度.md": "任务与进度模板.md",
               "交接记录.md": "交接记录模板.md"}
    try:
        rendered = {out: (TEMPLATES / src).read_text(encoding="utf-8") for out, src in mapping.items()}
    except OSError as exc:
        print("停止：读取模板失败：" + str(exc), file=sys.stderr)
        return 1

    today = date.today().isoformat()
    rel = "01_项目库/" + name
    values = {
        "{{项目名称}}": name,
        "{{创建日期}}": today,
        "{{项目路径}}": rel,
        "{{项目目标}}": "待明确",
        "{{项目背景}}": "待补充",
        "{{已确认决策}}": "待补充",
        "{{下一步}}": "待明确",
        "{{关键词}}": keywords,
    }
    rendered = {f: functools.reduce(lambda s, kv: s.replace(*kv), values.items(), text)
                for f, text in rendered.items()}

    print("拟创建：" + str(target))
    for filename in rendered:
        print("  - " + filename)
    print("关键词：" + keywords)
    print("拟追加项目总表：" + str(INDEX))
    if not args.confirm:
        print("仅预览。确认后再加 --confirm 执行。")
        return 0

    op_id = snapshot_index(name)
    if not op_id:
        return 1

    created = []
    try:
        target.mkdir(parents=False, exist_ok=False)
        created.append(target)
        for filename, content in rendered.items():
            path = target / filename
            with path.open("x", encoding="utf-8", newline="") as handle:
                handle.write(content)
            created.append(path)
        old = INDEX.read_text(encoding="utf-8")
        row = "| {} | 规划中 | {} | {} | 待明确 | 待补充 | {} | {} |{}".format(
            name, rel, keywords, today, today, NEWLINE)
        with INDEX.open("a", encoding="utf-8", newline="") as handle:
            if not old.endswith(NEWLINE):
                handle.write(NEWLINE)
            handle.write(row)
    except OSError as exc:
        print("执行部分失败：" + str(exc), file=sys.stderr)
        print("已创建且保留（未自动删除）：", file=sys.stderr)
        for path in created:
            print("  " + str(path), file=sys.stderr)
        print("请人工检查项目目录和总表；索引可能尚未更新。", file=sys.stderr)
        return 1
    finish_op(op_id)
    print("项目创建完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
