#!/usr/bin/env python3
"""AI 工坊｜只读体检（第二阶段）

汇总检查：结构、项目总表、项目三件套、知识/技能索引、进度摘要一致性。
无异常时只输出一行；有异常时列出明细。本脚本不修改任何文件。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

TOOL_NAME = "检查工作区.py"
TOOL_VERSION = "2.0"
COLUMNS = ["项目名称", "状态", "项目路径", "关键词·别名", "下一步", "备注", "最近更新", "创建日期"]
STATUSES = ("规划中", "进行中", "暂停", "已完成", "已归档")
REQUIRED_DOCS = ("README.md", "任务与进度.md", "交接记录.md")
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


def split_row(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def table_rows(root):
    path = root / "01_项目库" / "项目总表.md"
    if not path.is_file():
        raise ToolError("项目总表不存在")
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
    rows = []
    for index in range(header_at + 2, len(lines)):
        line = lines[index]
        if not line.startswith("|"):
            break
        cells = split_row(line)
        if len(cells) != len(COLUMNS):
            raise ToolError("总表第 {} 行列数为 {}".format(index + 1, len(cells)))
        rows.append(cells)
    return rows


def count_library(root, name):
    base = root / name
    if not base.is_dir():
        return 0
    return sum(1 for path in base.rglob("*")
               if path.is_file() and path.name not in ("README.md", "索引.md")
               and not path.name.startswith("."))


def run_tool(root, name, *extra):
    tool = root / "06_工具箱" / name
    if not tool.is_file():
        return 1, "缺少工具：" + name
    result = subprocess.run([sys.executable, str(tool), *extra],
                            cwd=str(root), capture_output=True, text=True, encoding="utf-8")
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(prog=TOOL_NAME, description="只读体检")
    parser.add_argument("--verbose", action="store_true", help="即使通过也显示明细")
    args = parser.parse_args(argv)

    problems = []
    details = []
    try:
        root = find_root(Path(__file__).resolve().parent)
    except ToolError as exc:
        print("工作区体检：失败 — " + str(exc))
        return 1

    try:
        rows = table_rows(root)
        registered = set()
        for cells in rows:
            name, status, project_path = cells[0], cells[1], cells[2]
            if not name:
                problems.append("总表存在空项目名")
                continue
            if status not in STATUSES:
                problems.append("「{}」状态不合法：{}".format(name, status))
            if project_path:
                registered.add(project_path.strip("/"))
            base = root / project_path if project_path else None
            if not base or not base.is_dir():
                problems.append("「{}」目录不存在：{}".format(name, project_path or "（空）"))
                continue
            for doc in REQUIRED_DOCS:
                if not (base / doc).is_file():
                    problems.append("「{}」缺少 {}".format(name, doc))
        projects_dir = root / "01_项目库"
        if projects_dir.is_dir():
            for child in sorted(projects_dir.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                if child.name not in {path.split("/")[-1] for path in registered}:
                    problems.append("目录未登记到项目总表：01_项目库/" + child.name)
    except (ToolError, OSError) as exc:
        problems.append(str(exc))
        rows = []

    for tool, extra in (("资料索引.py", ("check",)), ("进度同步.py", ("check",)),
                        ("快照.py", ("audit", "--strict"))):
        code, output = run_tool(root, tool, *extra)
        if code != 0:
            problems.append("{} {} 未通过".format(tool, " ".join(extra)))
            details.append((tool + " " + " ".join(extra), output))
        elif args.verbose and output:
            print(output)

    if rows:
        print("工作区体检：共 {} 个项目".format(len(rows)), end="")
    else:
        print("工作区体检：暂未登记项目", end="")
    print("（知识 {} ｜ 技能 {}）".format(count_library(root, "02_知识库"),
                                       count_library(root, "03_技能库")))

    if not problems:
        if not args.verbose:
            print("结果：通过。")
        else:
            print("结果：通过。")
        return 0
    print("结果：发现 {} 个问题：".format(len(problems)))
    for item in problems:
        for line in str(item).splitlines():
            print("  - " + line)
    for tool, output in details:
        print("  ── {} 明细 ──".format(tool))
        for line in output.splitlines():
            print("     " + line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
