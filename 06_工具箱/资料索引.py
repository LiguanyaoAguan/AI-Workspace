#!/usr/bin/env python3
"""AI 工坊｜知识库 / 技能库索引维护（第二阶段）

只负责结构一致性，不做语义判断：
- 检查索引与实际文件是否一一对应；
- 登记、更新、删除索引行；
- 写入索引前自动调用 快照.py 保存原样，快照失败则整体停止。

索引路径一律相对工作区根目录，整个工作区可整体迁移。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

TOOL_NAME = "资料索引.py"
TOOL_VERSION = "1.0"
COLUMNS = ["标题", "类型", "适用场景", "主题词", "摘要", "路径", "验证状态", "更新日期"]
LIBS = {"02_知识库": "02_知识库/索引.md", "03_技能库": "03_技能库/索引.md"}
DEFAULT_TYPE = {"02_知识库": "知识", "03_技能库": "技能"}
EXCLUDE_NAMES = {"README.md", "索引.md"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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


def clean_cell(text):
    return str(text).replace("|", "／").replace("\n", " ").strip()


def rel_posix(path, root):
    return Path(path).relative_to(root).as_posix()


def split_row(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_index(text):
    lines = text.splitlines()
    header_at = None
    for index, line in enumerate(lines):
        if line.startswith("|") and split_row(line)[:1] == ["标题"]:
            header_at = index
            break
    if header_at is None:
        raise ToolError("索引文件缺少表头（第一列应为「标题」）")
    separator_at = header_at + 1
    if separator_at >= len(lines) or not lines[separator_at].startswith("|"):
        raise ToolError("索引文件缺少表头分隔线")
    rows = []
    for index in range(separator_at + 1, len(lines)):
        line = lines[index]
        if not line.startswith("|"):
            break
        cells = split_row(line)
        if len(cells) != len(COLUMNS):
            raise ToolError("索引第 {} 行列数不是 {}：{}".format(index + 1, len(COLUMNS), line[:40]))
        rows.append({"line": index, "cells": cells})
    return {"lines": lines, "rows": rows, "last_line": rows[-1]["line"] if rows else separator_at}


def load_index(root, library):
    index_rel = LIBS[library]
    path = root / index_rel
    if not path.is_file():
        raise ToolError("索引文件不存在：" + index_rel)
    return path, parse_index(path.read_text(encoding="utf-8"))


def write_lines(path, lines):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))


def snapshot(root, index_rel, reason):
    """写前 begin：返回操作编号；失败则抛错，调用方不得继续写入。"""
    tool = root / "06_工具箱" / "快照.py"
    if not tool.is_file():
        raise ToolError("找不到快照工具：" + str(tool))
    result = subprocess.run(
        [sys.executable, str(tool), "begin", "--kind", "edit", "--files", index_rel,
         "--reason", reason],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        raise ToolError("写前快照失败，已停止写入：" + index_rel)
    for line in result.stdout.splitlines():
        if line.startswith("操作编号："):
            return line.split("：", 1)[1].strip()
    raise ToolError("未能取得操作编号，已停止写入：" + index_rel)


def finish_op(root, op_id):
    tool = root / "06_工具箱" / "快照.py"
    result = subprocess.run([sys.executable, str(tool), "end", op_id],
                            cwd=str(root), capture_output=True, text=True, encoding="utf-8")
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        print("警告：end 校验失败，索引写入需人工复核。", file=sys.stderr)
        return False
    return True


def library_files(root, library):
    base = root / library
    result = []
    if not base.is_dir():
        return result
    for path in sorted(base.rglob("*")):
        if path.is_dir():
            continue
        if path.name in EXCLUDE_NAMES or path.name.startswith("."):
            continue
        result.append(path)
    return result


def resolve_library_path(root, raw):
    text = str(raw).strip()
    if not text:
        raise ToolError("路径为空")
    if "\\" in text or Path(text).is_absolute():
        raise ToolError("请使用工作区内的相对路径：" + text)
    if any(part == ".." for part in text.split("/")):
        raise ToolError("路径包含 ..，已拒绝：" + text)
    path = (root / text).resolve()
    for library in LIBS:
        base = (root / library).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            continue
        if not path.is_file():
            raise ToolError("文件不存在：" + text)
        return path, library
    raise ToolError("只能登记 02_知识库 或 03_技能库 中的文件：" + text)


def sha256_of(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- 子命令

def cmd_check(root, args):
    issues = []
    for library, index_rel in LIBS.items():
        path, table = load_index(root, library)
        indexed = {}
        for row in table["rows"]:
            cells = row["cells"]
            rel = cells[5]
            if not rel:
                issues.append((index_rel, "第 {} 行缺少路径".format(row["line"] + 1)))
                continue
            if rel in indexed:
                issues.append((index_rel, "路径重复登记：" + rel))
            indexed[rel] = cells
            if any(not cell for cell in cells):
                issues.append((index_rel, "字段不完整：" + rel))
            if not DATE_RE.match(cells[7] or ""):
                issues.append((index_rel, "日期格式错误：{}（{}）".format(rel, cells[7])))
            if not rel.startswith(library + "/"):
                issues.append((index_rel, "路径不在 {} 内：{}".format(library, rel)))
        actual = {rel_posix(p, root): p for p in library_files(root, library)}
        for rel in sorted(actual):
            if rel not in indexed:
                issues.append((library, "未登记：" + rel))
        for rel in sorted(indexed):
            if rel not in actual:
                issues.append((index_rel, "失效条目（文件不存在）：" + rel))
        print("{}：文件 {} 个，索引 {} 行".format(library, len(actual), len(table["rows"])))

    if args.duplicates:
        groups = {}
        skip = {".ai-workspace", "05_归档"}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = rel_posix(path, root)
            if rel.split("/")[0] in skip or path.name.startswith("."):
                continue
            groups.setdefault(sha256_of(path), []).append(rel)
        dup = {k: v for k, v in groups.items() if len(v) > 1}
        if dup:
            print("发现 {} 组内容完全相同的文件：".format(len(dup)))
            for digest, files in sorted(dup.items(), key=lambda kv: kv[1][0]):
                print("  " + " ｜ ".join(files))
        else:
            print("未发现内容完全相同的文件。")

    if issues:
        print("索引检查不通过，共 {} 项：".format(len(issues)))
        for where, why in issues:
            print("  - [{}] {}".format(where, why))
        return 1
    print("索引检查通过。")
    return 0


def cmd_add(root, args):
    path, library = resolve_library_path(root, args.path)
    index_rel = LIBS[library]
    rel = rel_posix(path, root)
    index_path, table = load_index(root, library)
    for row in table["rows"]:
        if row["cells"][5] == rel:
            raise ToolError("该文件已登记，如需修改请使用 update：" + rel)

    cells = [
        clean_cell(args.title), clean_cell(args.type or DEFAULT_TYPE[library]),
        clean_cell(args.scene), clean_cell(args.keywords or "—"),
        clean_cell(args.summary or "—"), rel,
        clean_cell(args.status or "待核验"), args.date or date.today().isoformat(),
    ]
    if not DATE_RE.match(cells[7]):
        raise ToolError("日期格式应为 YYYY-MM-DD：" + cells[7])
    op_id = snapshot(root, index_rel, "登记索引：" + rel)

    lines = table["lines"]
    new_line = "| " + " | ".join(cells) + " |"
    insert_at = table["last_line"] + 1
    lines.insert(insert_at, new_line)
    write_lines(index_path, lines)

    _, verify = load_index(root, library)
    found = [row for row in verify["rows"] if row["cells"][5] == rel]
    if len(found) != 1:
        raise ToolError("写入后校验失败：索引中未能唯一找到 " + rel)
    finish_op(root, op_id)
    print("已登记：{} → {}".format(rel, index_rel))
    return 0


def cmd_update(root, args):
    path, library = resolve_library_path(root, args.path)
    index_rel = LIBS[library]
    rel = rel_posix(path, root)
    index_path, table = load_index(root, library)
    target = None
    for row in table["rows"]:
        if row["cells"][5] == rel:
            target = row
            break
    if target is None:
        raise ToolError("索引中没有该文件，请先使用 add：" + rel)
    op_id = snapshot(root, index_rel, "更新索引：" + rel)
    cells = list(target["cells"])
    if args.title:
        cells[0] = clean_cell(args.title)
    if args.scene:
        cells[2] = clean_cell(args.scene)
    if args.keywords:
        cells[3] = clean_cell(args.keywords)
    if args.summary:
        cells[4] = clean_cell(args.summary)
    if args.status:
        cells[6] = clean_cell(args.status)
    cells[7] = args.date or date.today().isoformat()
    table["lines"][target["line"]] = "| " + " | ".join(cells) + " |"
    write_lines(index_path, table["lines"])
    finish_op(root, op_id)
    print("已更新：{} → {}".format(rel, index_rel))
    return 0


def cmd_remove(root, args):
    path, library = resolve_library_path(root, args.path)
    index_rel = LIBS[library]
    rel = rel_posix(path, root)
    index_path, table = load_index(root, library)
    hit = [row for row in table["rows"] if row["cells"][5] == rel]
    if not hit:
        raise ToolError("索引中没有该文件：" + rel)
    op_id = snapshot(root, index_rel, "移除索引：" + rel)
    for row in reversed(hit):
        del table["lines"][row["line"]]
    write_lines(index_path, table["lines"])
    finish_op(root, op_id)
    print("已移除：{} → {}".format(rel, index_rel))
    return 0


def cmd_list(root, args):
    for library, index_rel in LIBS.items():
        _, table = load_index(root, library)
        print("{}（{} 条）".format(index_rel, len(table["rows"])))
        for row in table["rows"]:
            cells = row["cells"]
            print("  - {}｜{}｜{}".format(cells[0], cells[1], cells[5]))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog=TOOL_NAME, description="知识库 / 技能库索引维护")
    parser.add_argument("--version", action="version", version="{} {}".format(TOOL_NAME, TOOL_VERSION))
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="只读检查索引与文件是否一致")
    p_check.add_argument("--duplicates", action="store_true", help="额外检查内容重复的文件")

    p_add = sub.add_parser("add", help="登记一个文件到索引")
    p_add.add_argument("path")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--scene", required=True, help="适用场景，用自然语言描述")
    p_add.add_argument("--keywords", default="")
    p_add.add_argument("--summary", default="")
    p_add.add_argument("--type", default="")
    p_add.add_argument("--status", default="")
    p_add.add_argument("--date", default="")

    p_update = sub.add_parser("update", help="更新索引条目")
    p_update.add_argument("path")
    p_update.add_argument("--title")
    p_update.add_argument("--scene")
    p_update.add_argument("--keywords")
    p_update.add_argument("--summary")
    p_update.add_argument("--status")
    p_update.add_argument("--date")

    p_remove = sub.add_parser("remove", help="从索引移除条目")
    p_remove.add_argument("path")

    sub.add_parser("list", help="列出索引条目")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        root = find_root(Path(__file__).resolve().parent)
    except ToolError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1
    handlers = {"check": cmd_check, "add": cmd_add, "update": cmd_update,
                "remove": cmd_remove, "list": cmd_list}
    try:
        return handlers[args.command](root, args)
    except ToolError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
