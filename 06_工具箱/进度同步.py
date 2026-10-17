#!/usr/bin/env python3
"""AI 工坊｜项目进度同步（第二阶段）

单一来源原则：
- `任务与进度.md` 是待办与下一步的唯一权威；
- 项目总表的「下一步」「最近更新」由本工具从它派生，不允许手工编辑；
- 项目总表是「状态」的权威；
- 项目 README 只记录定位与基本状态。

check 只读比对，sync 需 --confirm，写入前自动快照项目总表。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

TOOL_NAME = "进度同步.py"
TOOL_VERSION = "1.0"
COLUMNS = ["项目名称", "状态", "项目路径", "关键词·别名", "下一步", "备注", "最近更新", "创建日期"]
STATUSES = ("规划中", "进行中", "暂停", "已完成", "已归档")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UPDATED_RE = re.compile(r"最近更新[:：]\s*(\d{4}-\d{2}-\d{2})")
TASK_FILE = "任务与进度.md"
REQUIRED_DOCS = ("README.md", "任务与进度.md", "交接记录.md")
INDEX_REL = "01_项目库/项目总表.md"

HARD_MARKERS = ("00_前台", "01_项目库", "01_项目库/项目总表.md", "06_工具箱")
SOFT_MARKERS = ("02_知识库", "03_技能库", "04_素材库", "05_归档",
                "AGENTS.md", "README.md", "06_工具箱/模板")


class ToolError(Exception):
    pass


def find_root(start):
    start = Path(start).resolve()
    for candidate in [start, *start.parents]:
        if not candidate.is_dir():
            continue
        if not all((candidate / m).exists() for m in HARD_MARKERS):
            continue
        if sum(1 for m in SOFT_MARKERS if (candidate / m).exists()) >= 2:
            return candidate
    raise ToolError("无法定位工作区根目录：未发现完整结构")


def clean(text, limit=60):
    text = str(text).replace("|", "／").replace("\n", " ").strip()
    return text[:limit] if len(text) > limit else text


def split_row(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def load_table(root):
    path = root / INDEX_REL
    if not path.is_file():
        raise ToolError("项目总表不存在：" + INDEX_REL)
    lines = path.read_text(encoding="utf-8").splitlines()
    header_at = None
    for index, line in enumerate(lines):
        if line.startswith("|") and split_row(line)[:1] == ["项目名称"]:
            header_at = index
            break
    if header_at is None:
        raise ToolError("项目总表缺少表头")
    if len(split_row(lines[header_at])) != len(COLUMNS):
        raise ToolError("项目总表表头不是 {} 列".format(len(COLUMNS)))
    separator_at = header_at + 1
    if separator_at >= len(lines) or not lines[separator_at].startswith("|"):
        raise ToolError("项目总表缺少表头分隔线")
    rows = []
    for index in range(separator_at + 1, len(lines)):
        line = lines[index]
        if not line.startswith("|"):
            break
        cells = split_row(line)
        if len(cells) != len(COLUMNS):
            raise ToolError("总表第 {} 行列数为 {}，应为 {}：{}".format(
                index + 1, len(cells), len(COLUMNS), line[:40]))
        rows.append({"line": index, "cells": cells})
    return {"path": path, "lines": lines, "rows": rows,
            "last_line": rows[-1]["line"] if rows else separator_at}


def parse_next(text):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() in ("## 下一步", "# 下一步"):
            for candidate in lines[index + 1:]:
                value = candidate.strip()
                if not value:
                    continue
                if value.startswith("#"):
                    break
                value = re.sub(r"^[-*>]\s*", "", value)
                value = re.sub(r"^\[\s?\]\s*", "", value)
                value = clean(value).rstrip("。．.；;")
                return value or "待明确"
            break
    return "待明确"


def parse_updated(text):
    match = UPDATED_RE.search(text)
    return match.group(1) if match else ""


def project_facts(root, project_path):
    base = root / project_path
    facts = {"exists": base.is_dir(), "missing": [], "next": "待明确", "updated": ""}
    if not facts["exists"]:
        return facts
    for name in REQUIRED_DOCS:
        if not (base / name).is_file():
            facts["missing"].append(name)
    task_file = base / TASK_FILE
    if task_file.is_file():
        text = task_file.read_text(encoding="utf-8")
        facts["next"] = parse_next(text)
        facts["updated"] = parse_updated(text)
    return facts


def collect_issues(root, table):
    issues = []
    for row in table["rows"]:
        cells = row["cells"]
        name, status, project_path = cells[0], cells[1], cells[2]
        where = "总表第 {} 行（{}）".format(row["line"] + 1, name)
        if not name:
            issues.append((where, "项目名称为空"))
            continue
        if status not in STATUSES:
            issues.append((where, "状态不合法：" + status))
        if not project_path:
            issues.append((where, "项目路径为空"))
            continue
        facts = project_facts(root, project_path)
        if not facts["exists"]:
            issues.append((where, "项目目录不存在：" + project_path))
            continue
        if facts["missing"]:
            issues.append((where, "缺少项目文档：" + "、".join(facts["missing"])))
        if cells[4] != facts["next"]:
            issues.append((where, "「下一步」与 {} 不一致（总表：{} / 实际：{}）".format(
                TASK_FILE, cells[4] or "空", facts["next"])))
        if facts["updated"] and cells[6] != facts["updated"]:
            issues.append((where, "「最近更新」不一致（总表：{} / 实际：{}）".format(
                cells[6] or "空", facts["updated"])))
        readme = root / project_path / "README.md"
        if readme.is_file():
            match = re.search(r"状态[:：]\s*(\S+)", readme.read_text(encoding="utf-8"))
            if match and match.group(1) != status:
                issues.append((where, "README 状态（{}）与总表（{}）不一致".format(
                    match.group(1), status)))
    return issues


def cmd_check(root, args):
    table = load_table(root)
    print("项目总表：{} 个项目".format(len(table["rows"])))
    issues = collect_issues(root, table)
    if not issues:
        print("进度同步检查通过。")
        return 0
    print("进度同步检查不通过，共 {} 项：".format(len(issues)))
    for where, why in issues:
        print("  - {}：{}".format(where, why))
    return 1


def snapshot_index(root, reason):
    """写前 begin：返回操作编号；失败则抛错，调用方不得继续写入。"""
    tool = root / "06_工具箱" / "快照.py"
    if not tool.is_file():
        raise ToolError("找不到快照工具：" + str(tool))
    result = subprocess.run(
        [sys.executable, str(tool), "begin", "--kind", "edit", "--files", INDEX_REL,
         "--reason", reason],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        raise ToolError("写前快照失败，已停止写入项目总表")
    for line in result.stdout.splitlines():
        if line.startswith("操作编号："):
            return line.split("：", 1)[1].strip()
    raise ToolError("未能取得操作编号，已停止写入项目总表")


def finish_op(root, op_id):
    tool = root / "06_工具箱" / "快照.py"
    result = subprocess.run([sys.executable, str(tool), "end", op_id],
                            cwd=str(root), capture_output=True, text=True, encoding="utf-8")
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        print("警告：end 校验失败，总表写入需人工复核。", file=sys.stderr)
        return False
    return True


def cmd_sync(root, args):
    table = load_table(root)
    pending = []
    for row in table["rows"]:
        cells = row["cells"]
        facts = project_facts(root, cells[2]) if cells[2] else None
        if not facts or not facts["exists"]:
            continue
        if cells[4] != facts["next"] or (facts["updated"] and cells[6] != facts["updated"]):
            pending.append((row, facts))
    if not pending:
        print("总表已是最新，无需同步。")
        return 0
    print("将更新 {} 个项目行：".format(len(pending)))
    for row, facts in pending:
        print("  - {}：下一步「{}」，最近更新「{}」".format(
            row["cells"][0], facts["next"], facts["updated"] or row["cells"][6]))
    if not args.confirm:
        print("仅预览，未修改总表。确认后加 --confirm。")
        return 0
    op_id = snapshot_index(root, "进度同步：按任务与进度.md 更新总表摘要")
    for row, facts in pending:
        cells = row["cells"]
        cells[4] = clean(facts["next"])
        if facts["updated"]:
            cells[6] = facts["updated"]
        table["lines"][row["line"]] = "| " + " | ".join(cells) + " |"
    write_table(table)
    verify = load_table(root)
    bad = [row for row in verify["rows"] if row["cells"][4] != project_facts(root, row["cells"][2])["next"]]
    if bad:
        raise ToolError("同步后校验失败：{} 行仍不一致".format(len(bad)))
    finish_op(root, op_id)
    print("同步完成，共更新 {} 行。".format(len(pending)))
    return 0


def write_table(table):
    temporary = table["path"].with_name(table["path"].name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(table["lines"]) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(table["path"]))


def build_parser():
    parser = argparse.ArgumentParser(prog=TOOL_NAME, description="项目进度同步（总表摘要派生）")
    parser.add_argument("--version", action="version", version="{} {}".format(TOOL_NAME, TOOL_VERSION))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="只读比对总表与任务与进度.md")
    p_sync = sub.add_parser("sync", help="把任务与进度.md 的摘要写回总表")
    p_sync.add_argument("--confirm", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        root = find_root(Path(__file__).resolve().parent)
        if args.command == "check":
            return cmd_check(root, args)
        return cmd_sync(root, args)
    except ToolError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
