#!/usr/bin/env python3
"""AI 工坊｜快照、受控写入与审计（安全机制修订版，仅用于临时测试副本）

三个阶段：
  begin  写前：复制本次将修改的全部文件，记录写前哈希，生成唯一操作编号
  write  写入：可选，带写前一致性校验的受控写入
  end    写后：校验结果，推进基线
  audit  审计：只报告"当前未处理"的安全异常

旧功能（snapshot / delete / restore / verify / list / show / log / prune）保留。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

SCHEMA = 1
STATE_VERSION = 1
TOOL_NAME = "快照.py"
TOOL_VERSION = "2.0"
STORE_DIR = ".ai-workspace"
EXCLUDED_TOP = (STORE_DIR, ".git")
BACKUP_DIR = "backup"
OPS_DIR = "ops"
LOG_FILE = "changes.log"
STATE_FILE = "state.json"
MANIFEST = "manifest.json"

HARD_MARKERS = ("00_前台", "01_项目库", "01_项目库/项目总表.md", "06_工具箱")
SOFT_MARKERS = ("02_知识库", "03_技能库", "04_素材库", "05_归档",
                "AGENTS.md", "README.md", "06_工具箱/模板")
METADATA_NAMES = (".DS_Store",)


def is_metadata_path(path_or_rel):
    name = str(path_or_rel).rstrip("/").rsplit("/", 1)[-1]
    return name in METADATA_NAMES


OPS = ("edit", "overwrite", "delete", "move", "rename", "create",
       "pre-restore", "auto-index", "manual", "pre-write")
KINDS = ("edit", "delete", "move", "rename", "create")


class ToolError(Exception):
    pass


class LoggedError(Exception):
    pass


# ---------------------------------------------------------------- 基础

def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def within(child, parent):
    try:
        Path(child).relative_to(parent)
        return True
    except ValueError:
        return False


def sha256_of(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def structure_ok(candidate):
    candidate = Path(candidate)
    if not candidate.is_dir():
        return False
    if not all((candidate / marker).exists() for marker in HARD_MARKERS):
        return False
    return sum(1 for marker in SOFT_MARKERS if (candidate / marker).exists()) >= 2


def find_root(start):
    start = Path(start).resolve()
    for candidate in [start, *start.parents]:
        if structure_ok(candidate):
            return candidate
    raise ToolError("无法定位工作区根目录：未发现完整结构")


def store_dir(root):
    return Path(root) / STORE_DIR


def backup_dir(root):
    return store_dir(root) / BACKUP_DIR


def ops_dir(root):
    return store_dir(root) / OPS_DIR


def log_path(root):
    return store_dir(root) / LOG_FILE


def state_path(root):
    return store_dir(root) / STATE_FILE


def rel_of(path, root):
    return Path(path).relative_to(root).as_posix()


def safe_rel(rel):
    if not rel or rel.startswith("/") or "\\" in rel:
        raise ToolError("路径不合法：" + str(rel))
    if any(part in ("..", ".", "") for part in rel.split("/")):
        raise ToolError("路径不合法：" + str(rel))
    return rel


def append_log(root, **entry):
    path = log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": now_iso()}
    record.update(entry)
    line = json.dumps(record, ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def append_log_safe(root, **entry):
    try:
        append_log(root, **entry)
    except OSError as exc:
        print("警告：变更日志写入失败：" + str(exc), file=sys.stderr)


# ---------------------------------------------------------------- 路径校验

def resolve_target(root, raw):
    text = str(raw).strip()
    if not text:
        raise ToolError("路径为空")
    if "\\" in text:
        raise ToolError("不接受反斜杠路径：" + text)
    if Path(text).is_absolute():
        raise ToolError("请使用工作区内的相对路径，不接受绝对路径：" + text)
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in text.split("/")):
        raise ToolError("路径包含 ..，已拒绝：" + text)
    if not parts:
        raise ToolError("路径为空：" + text)
    candidate = Path(root)
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ToolError("路径经过符号链接，已拒绝：" + text)
    resolved = candidate.resolve()
    if not within(resolved, root):
        raise ToolError("路径越出工作区，已拒绝：" + text)
    if resolved == root:
        raise ToolError("不能对工作区根目录本身操作")
    if within(resolved, store_dir(root)):
        raise ToolError("禁止对备份区本身操作：" + text)
    if within(resolved, root / ".git") or resolved == root / ".git":
        raise ToolError("禁止对 .git 目录操作：" + text)
    return resolved


def unique_targets(root, raws):
    seen, result = set(), []
    for raw in raws:
        path = resolve_target(root, raw)
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


# ---------------------------------------------------------------- 状态与操作

def load_state(root):
    path = state_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError("state.json 无法解析：{}（请检查或重新 init --force）".format(exc))
    if not isinstance(data.get("files"), dict):
        raise ToolError("state.json 格式不正确")
    return data


def save_state(root, data):
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["version"] = STATE_VERSION
    data["updated_at"] = now_iso()
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))


def scan_workspace(root):
    files = {}
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        rel = rel_of(path, root)
        if rel.split("/")[0] in EXCLUDED_TOP:
            continue
        if is_metadata_path(rel):
            continue
        files[rel] = sha256_of(path)
    return files


def op_path(root, op_id):
    if "/" in op_id or "\\" in op_id or op_id in ("", ".", ".."):
        raise ToolError("非法操作编号：" + op_id)
    return ops_dir(root) / (op_id + ".json")


def load_op(root, op_id):
    path = op_path(root, op_id)
    if not path.is_file():
        raise ToolError("找不到操作记录：" + op_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError("操作记录损坏：{}（{}）".format(op_id, exc))


def save_op(root, op):
    path = op_path(root, op["id"])
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(op, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))


def create_op(root, op):
    directory = ops_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y%m%d-%H%M%S")
    for index in range(1, 1000):
        name = stem if index == 1 else "{}-{}".format(stem, index)
        path = directory / (name + ".json")
        try:
            handle = path.open("x", encoding="utf-8")
        except FileExistsError:
            continue
        op["id"] = name
        with handle:
            json.dump(op, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return op
    raise ToolError("操作编号分配失败：同一秒内操作过多")


def all_ops(root):
    directory = ops_dir(root)
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.glob("*.json")):
        try:
            result.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            result.append({"id": path.stem, "status": "corrupt",
                           "error": "操作记录损坏", "declared": [], "pre_sha256": {}})
    return result


def migrate_metadata(root, state):
    """从基线移除系统元数据文件（仅 .DS_Store），其他条目不动。"""
    removed = [rel for rel in state.get("files", {})
               if is_metadata_path(rel) or rel == ".git" or rel.startswith(".git/")]
    for rel in removed:
        state["files"].pop(rel, None)
    if removed:
        save_state(root, state)
        print("已从基线移除系统元数据：{}（其余 {} 个文件保持不变）".format(
            "、".join(removed), len(state["files"])))
    return removed


def advance_state(root, op):
    state = load_state(root)
    if state is None:
        state = {"version": STATE_VERSION, "files": {}}
    kind = op["kind"]
    stamp = now_iso()
    if kind == "delete":
        for rel in op["declared"]:
            state["files"].pop(rel, None)
    elif kind in ("move", "rename"):
        old, new = op.get("from"), op.get("to")
        state["files"].pop(old, None)
        if new:
            state["files"][new] = {"sha256": op["post_sha256"][new], "op": op["id"], "ts": stamp}
    else:
        for rel, digest in op["post_sha256"].items():
            if is_metadata_path(rel):
                continue
            state["files"][rel] = {"sha256": digest, "op": op["id"], "ts": stamp}
    state["last_op"] = op["id"]
    save_state(root, state)


# ---------------------------------------------------------------- 快照

def claim_snapshot_dir(root):
    base = backup_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y%m%d-%H%M%S")
    for index in range(1, 1000):
        name = stem if index == 1 else "{}-{}".format(stem, index)
        candidate = base / name
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ToolError("无法创建快照目录（{}）：{}".format(candidate.name, exc))
        return candidate
    raise ToolError("快照编号分配失败：同一秒内快照数量超过上限")


def write_manifest(snap_dir, data):
    temporary = Path(snap_dir) / (MANIFEST + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(Path(snap_dir) / MANIFEST))


def take_snapshot(root, targets, operation, reason, source):
    targets = list(targets)
    if not targets:
        raise ToolError("没有需要快照的文件")
    for path in targets:
        if not path.exists():
            raise ToolError("文件不存在，无法快照：" + rel_of(path, root))
        if not path.is_file():
            raise ToolError("只支持普通文件：" + rel_of(path, root))
    snap_dir = claim_snapshot_dir(root)
    snap_id = snap_dir.name
    files, copied = [], []
    try:
        for path in targets:
            rel = rel_of(path, root)
            destination = snap_dir / "files" / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(path), str(destination))
            digest = sha256_of(destination)
            if digest != sha256_of(path):
                raise ToolError("复制后校验不一致：" + rel)
            files.append({"path": rel, "existed": True,
                          "size": destination.stat().st_size, "sha256": digest})
            copied.append(rel)
        manifest = {"schema": SCHEMA, "id": snap_id, "created_at": now_iso(),
                    "operation": operation, "reason": reason, "source": source, "files": files}
        write_manifest(snap_dir, manifest)
    except Exception as exc:
        append_log_safe(root, id=snap_id, action="snapshot",
                        status="partial" if copied else "failed",
                        operation=operation, reason=reason, source=source,
                        files=[rel_of(path, root) for path in targets],
                        completed=copied, error=str(exc))
        raise LoggedError("快照失败（{}）：{}；已复制 {} 个，未自动清理".format(snap_id, exc, len(copied)))
    append_log_safe(root, id=snap_id, action="snapshot", status="ok",
                    operation=operation, reason=reason, source=source,
                    files=[rel_of(path, root) for path in targets],
                    completed=copied, error=None)
    return snap_id, manifest


def load_manifest(root, snap_id):
    if "/" in snap_id or "\\" in snap_id or snap_id in ("", ".", ".."):
        raise ToolError("非法快照编号：" + snap_id)
    snap_dir = backup_dir(root) / snap_id
    if not within(snap_dir.resolve(), backup_dir(root).resolve()):
        raise ToolError("快照路径越界：" + snap_id)
    manifest_file = snap_dir / MANIFEST
    if not manifest_file.is_file():
        raise ToolError("找不到快照：" + snap_id)
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError("快照清单损坏：{}（{}）".format(snap_id, exc))
    if not isinstance(data.get("files"), list):
        raise ToolError("快照清单格式不正确：" + snap_id)
    for item in data["files"]:
        safe_rel(item.get("path", ""))
    return data, snap_dir


def snapshot_dirs(root):
    base = backup_dir(root)
    if not base.is_dir():
        return []
    return sorted((path for path in base.iterdir() if path.is_dir()), key=lambda p: p.name)


# ---------------------------------------------------------------- 新命令

def cmd_init(root, args):
    state = load_state(root)
    if state is not None and not args.force:
        print("基线已存在（{} 个文件），未修改；需要重建请加 --force。".format(len(state["files"])))
        return 0
    files = scan_workspace(root)
    state = {"version": STATE_VERSION, "updated_at": now_iso(), "last_op": None,
             "files": {rel: {"sha256": digest, "op": "init", "ts": now_iso()}
                       for rel, digest in files.items()}}
    save_state(root, state)
    print("基线已建立：{} 个文件".format(len(files)))
    return 0


def expand_begin_targets(root, args):
    kind = args.kind
    if kind in ("move", "rename"):
        if not args.from_path or not args.to_path:
            raise ToolError("move/rename 需要 --from 与 --to")
        old = resolve_target(root, args.from_path)
        new = resolve_target(root, args.to_path)
        if not old.is_file():
            raise ToolError("源文件不存在：" + args.from_path)
        if new.exists():
            raise ToolError("目标已存在：" + args.to_path)
        return [old], old, new

    targets = []
    if args.project:
        base = root / "01_项目库" / args.project
        if not base.is_dir():
            raise ToolError("项目不存在：" + args.project)
        targets.extend(sorted(p for p in base.rglob("*")
                               if p.is_file() and not is_metadata_path(p.name)))
    if args.files:
        for path in unique_targets(root, args.files):
            if is_metadata_path(path.name):
                raise ToolError("系统元数据文件不纳入受控写入：" + rel_of(path, root))
            targets.append(path)

    seen, unique = set(), []
    for path in targets:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    if not unique:
        raise ToolError("必须用 --files 或 --project 指定要保护的文件")
    return unique, None, None


def cmd_begin(root, args):
    kind = args.kind
    targets, old, new = expand_begin_targets(root, args)

    if kind == "create":
        existing = [rel_of(p, root) for p in targets if p.exists()]
        if existing:
            raise ToolError("create 的文件已存在：" + "、".join(existing))
        snap_id, pre = None, {}
    else:
        missing = [rel_of(p, root) for p in targets if not p.is_file()]
        if missing:
            raise ToolError("目标不存在：" + "、".join(missing))
        snap_id, manifest = take_snapshot(root, targets, "pre-write", args.reason, TOOL_NAME)
        pre = {item["path"]: item["sha256"] for item in manifest["files"]}
        declared = {rel_of(p, root) for p in targets}
        if set(pre) != declared:
            raise ToolError("快照覆盖不完整，已停止")

    op = {
        "schema": SCHEMA, "id": "", "ts": now_iso(), "kind": kind,
        "reason": args.reason, "snapshot": snap_id,
        "declared": [rel_of(p, root) for p in targets],
        "pre_sha256": pre, "post_sha256": {}, "status": "open", "error": None,
        "from": rel_of(old, root) if old else None,
        "to": rel_of(new, root) if new else None,
    }
    op = create_op(root, op)
    append_log_safe(root, id=op["id"], action="begin", status="ok", operation=kind,
                    reason=args.reason, source=TOOL_NAME, files=op["declared"],
                    completed=[], error=None)
    print("操作编号：" + op["id"])
    print("类型：" + kind + "｜文件 {} 个".format(len(op["declared"])))
    if snap_id:
        print("写前快照：" + snap_id)
    print("下一步：修改后用 end {} 收尾；或用 write 做带一致性校验的写入。".format(op["id"]))
    return 0


def evaluate_post(op, root):
    kind = op["kind"]
    post, errors = {}, []
    if kind == "edit":
        for rel in op["declared"]:
            path = root / rel
            if not path.is_file():
                errors.append("文件缺失：" + rel)
            else:
                post[rel] = sha256_of(path)
    elif kind == "delete":
        for rel in op["declared"]:
            if (root / rel).exists():
                errors.append("文件仍存在：" + rel)
    elif kind in ("move", "rename"):
        old, new = op.get("from"), op.get("to")
        if old and (root / old).exists():
            errors.append("旧路径仍存在：" + old)
        if not new or not (root / new).is_file():
            errors.append("新路径不存在：" + str(new))
        else:
            post[new] = sha256_of(root / new)
            expected = op["pre_sha256"].get(old)
            if expected and post[new] != expected:
                errors.append("移动后内容发生变化：" + new)
    elif kind == "create":
        for rel in op["declared"]:
            path = root / rel
            if not path.is_file():
                errors.append("新建文件不存在：" + rel)
            else:
                post[rel] = sha256_of(path)
    return post, errors


def cmd_end(root, args):
    op = load_op(root, args.op_id)
    if op.get("status") != "open":
        raise ToolError("操作不是 open 状态：{}（{}）".format(op["id"], op.get("status")))
    post, errors = evaluate_post(op, root)
    if errors:
        op["status"] = "failed"
        op["error"] = "；".join(errors)
        save_op(root, op)
        append_log_safe(root, id=op["id"], action="end", status="failed",
                        operation=op["kind"], reason=op["reason"], source=TOOL_NAME,
                        files=op["declared"], completed=[], error=op["error"])
        print("end 校验失败：" + op["error"])
        print("基线未推进；快照仍可用于恢复。")
        return 1
    op["status"] = "verified"
    op["post_sha256"] = post
    save_op(root, op)
    advance_state(root, op)
    append_log_safe(root, id=op["id"], action="end", status="ok", operation=op["kind"],
                    reason=op["reason"], source=TOOL_NAME, files=op["declared"],
                    completed=list(post.keys()), error=None)
    print("操作已完成并推进基线：" + op["id"])
    return 0


def cmd_resolve(root, args):
    """处理 failed 操作：保留原始记录与处理历史，不改 verified，不推进基线。"""
    op = load_op(root, args.op_id)
    if op.get("status") != "failed":
        raise ToolError("只能处理 failed 状态的操作，当前为：{}".format(op.get("status")))
    state = load_state(root)
    baseline = state["files"] if state else {}
    checks, anomalies = [], []
    for rel in op.get("declared", []):
        path = root / rel
        current = sha256_of(path) if path.is_file() else None
        base = baseline.get(rel, {}).get("sha256")
        if current is None:
            if base is None:
                verdict = "已不存在，且基线无此文件"
            else:
                verdict = "文件缺失，与基线不符"
                anomalies.append(rel)
        elif base is None:
            verdict = "基线中无此文件（按新建处理）"
        elif current == base:
            verdict = "与基线一致"
        else:
            verdict = "与基线不一致，仍属未受保护的变化"
            anomalies.append(rel)
        checks.append({"path": rel, "current_sha256": current,
                       "baseline_sha256": base, "verdict": verdict})

    history = op.get("history", [])
    history.append({"ts": now_iso(), "action": "resolve", "from": "failed",
                    "to": "resolved", "reason": args.reason,
                    "note": args.note or "", "files": checks})
    op["history"] = history
    op["status"] = "resolved"
    save_op(root, op)
    append_log_safe(root, id=op["id"], action="resolve", status="ok",
                    operation=op.get("kind"), reason=args.reason, source=TOOL_NAME,
                    files=op.get("declared", []), completed=[], error=None)
    print("已处理失败操作：" + op["id"])
    print("原因：" + args.reason)
    for item in checks:
        print("  - {}：{}".format(item["path"], item["verdict"]))
    print("基线未推进；原始快照与写前哈希保持不变。")
    if anomalies:
        print("仍有 {} 个文件与基线不符，审计会继续报错：".format(len(anomalies)))
        for rel in anomalies:
            print("  - " + rel)
        return 1
    print("相关文件当前与基线一致，审计不再报该操作。")
    return 0


def cmd_write(root, args):
    op = load_op(root, args.op_id)
    if op.get("status") != "open":
        raise ToolError("操作不是 open 状态：" + op["id"])
    if args.file not in op["declared"]:
        raise ToolError("该文件不在本次操作声明中：" + args.file)
    for rel, expected in op["pre_sha256"].items():
        path = root / rel
        if not path.is_file():
            raise ToolError("写前状态已变化（文件缺失），拒绝写入：" + rel)
        if sha256_of(path) != expected:
            op["status"] = "failed"
            op["error"] = "写前状态已变化：" + rel
            save_op(root, op)
            append_log_safe(root, id=op["id"], action="write", status="failed",
                            operation=op["kind"], reason=op["reason"], source=TOOL_NAME,
                            files=op["declared"], completed=[], error=op["error"])
            print("写前状态已变化，拒绝写入：" + rel)
            print("文件未被本次受控写入修改；请重新 begin。")
            return 1
    source = Path(args.source)
    if not source.is_file():
        raise ToolError("来源文件不存在：" + str(source))
    target = root / args.file
    data = source.read_bytes()
    temporary = target.with_name(target.name + ".write-tmp")
    temporary.write_bytes(data)
    os.replace(str(temporary), str(target))
    if sha256_of(target) != sha256_of(source):
        raise ToolError("写入后校验失败：" + args.file)
    op["post_sha256"][args.file] = sha256_of(target)
    save_op(root, op)
    append_log_safe(root, id=op["id"], action="write", status="ok", operation=op["kind"],
                    reason=op["reason"], source=TOOL_NAME, files=[args.file],
                    completed=[args.file], error=None)
    print("已受控写入：{}（仍需 end {} 收尾）".format(args.file, op["id"]))
    return 0


def cmd_audit(root, args):
    state = load_state(root)
    if state is None:
        print("尚未建立基线，自动建立（等同 init）。")
        cmd_init(root, argparse.Namespace(force=False))
        state = load_state(root)

    ops = all_ops(root)
    open_ops = [op for op in ops if op.get("status") == "open"]
    open_by_path = {}
    for op in open_ops:
        for rel in op.get("declared", []):
            open_by_path.setdefault(rel, []).append(op)

    migrate_metadata(root, state)
    anomalies, warnings = [], []
    baseline = state["files"]
    for rel, meta in sorted(baseline.items()):
        if is_metadata_path(rel):
            continue
        path = root / rel
        covering = [op for op in open_by_path.get(rel, [])
                    if op.get("pre_sha256", {}).get(rel) == meta["sha256"]]
        if not path.is_file():
            if any(op.get("kind") == "delete" for op in covering):
                warnings.append((covering[0]["id"], "待收尾：文件已删除，待 end"))
            else:
                anomalies.append((rel, "文件缺失，且没有受控删除记录（基线 {}".format(meta.get("op")) + "）"))
            continue
        if sha256_of(path) != meta["sha256"]:
            if covering:
                warnings.append((covering[0]["id"], "待收尾：已修改但未 end（{}）".format(rel)))
            else:
                anomalies.append((rel, "修改缺少有效写前快照（基线 {}".format(meta.get("op")) + "）"))

    disk = scan_workspace(root)
    new_files = [rel for rel in disk if rel not in baseline]

    for op in ops:
        status = op.get("status")
        if status == "corrupt":
            anomalies.append((op["id"], "操作记录损坏，无法判定"))
            continue
        if status == "failed":
            anomalies.append((op["id"], "失败操作：" + str(op.get("error"))))
        elif status == "resolved":
            continue
        elif status == "open":
            changed = [rel for rel, expected in op.get("pre_sha256", {}).items()
                       if (root / rel).is_file() and sha256_of(root / rel) != expected]
            if not changed:
                warnings.append((op["id"], "快照后尚未写入，待收尾或忽略"))

    if args.verbose and new_files:
        print("新增文件（新建不强制快照）：")
        for rel in new_files:
            print("  - " + rel)

    for op_id, why in warnings:
        print("警告［{}］{}".format(op_id, why))
    if anomalies:
        print("审计发现 {} 处未处理异常：".format(len(anomalies)))
        for rel, why in anomalies:
            print("  - {}：{}".format(rel, why))
        return 1
    if args.strict and open_ops:
        print("严格模式：仍有 {} 个未结束的 open 操作：".format(len(open_ops)))
        for op in open_ops:
            print("  - {}（{}）".format(op["id"], op.get("kind")))
        return 1
    print("审计通过：基线 {} 个文件无未受保护的变化；待收尾 {} 项。".format(len(baseline), len(warnings)))
    return 0


# ---------------------------------------------------------------- 旧命令

def cmd_snapshot(root, args):
    targets = unique_targets(root, args.files)
    snap_id, manifest = take_snapshot(root, targets, args.op, args.reason, TOOL_NAME)
    print("快照完成：" + snap_id)
    print("原因：" + args.reason)
    print("文件 {} 个：".format(len(manifest["files"])))
    for item in manifest["files"]:
        print("  - {}  {} 字节  sha256={}…".format(item["path"], item["size"], item["sha256"][:16]))
    print("存放位置：{}/{}/{}/".format(STORE_DIR, BACKUP_DIR, snap_id))
    print("注意：snapshot 只是备份，不产生操作记录，也不会推进审计基线；受控写入请用 begin/end。")
    return 0


def cmd_delete(root, args):
    targets = unique_targets(root, args.files)
    for path in targets:
        if not path.exists():
            raise ToolError("文件不存在，无法删除：" + rel_of(path, root))
        if not path.is_file():
            raise ToolError("只支持删除普通文件：" + rel_of(path, root))
    if not args.confirm:
        print("将要删除以下文件（会先保存副本，可恢复）：")
        for path in targets:
            print("  - {}  {} 字节".format(rel_of(path, root), path.stat().st_size))
        print("仅预览，未删除任何文件。确认后加 --confirm。")
        return 0

    snap_id, manifest = take_snapshot(root, targets, "delete", args.reason, TOOL_NAME)
    pre = {item["path"]: item["sha256"] for item in manifest["files"]}
    op = {"schema": SCHEMA, "id": "", "ts": now_iso(), "kind": "delete",
          "reason": args.reason, "snapshot": snap_id,
          "declared": [rel_of(p, root) for p in targets],
          "pre_sha256": pre, "post_sha256": {}, "status": "open", "error": None,
          "from": None, "to": None}
    op = create_op(root, op)

    completed, failed = [], []
    for path in targets:
        rel = rel_of(path, root)
        try:
            path.unlink()
            completed.append(rel)
        except OSError as exc:
            failed.append({"path": rel, "error": str(exc)})
    if failed:
        op["status"] = "failed"
        op["error"] = "部分文件删除失败：" + "、".join(item["path"] for item in failed)
        save_op(root, op)
        state = load_state(root)
        if state:
            for rel in completed:
                state["files"].pop(rel, None)
            save_state(root, state)
        append_log_safe(root, id=op["id"], action="delete", status="partial",
                        operation="delete", reason=args.reason, source=TOOL_NAME,
                        files=op["declared"], completed=completed,
                        failed=[item["path"] for item in failed], error=op["error"])
        print("删除部分失败：成功 {}，失败 {}".format(len(completed), len(failed)))
        for item in failed:
            print("  失败：{}（{}）".format(item["path"], item["error"]))
        return 1
    op["status"] = "verified"
    save_op(root, op)
    advance_state(root, op)
    append_log_safe(root, id=op["id"], action="delete", status="ok", operation="delete",
                    reason=args.reason, source=TOOL_NAME, files=op["declared"],
                    completed=completed, error=None)
    print("删除完成并推进基线：{} 个（快照 {}）".format(len(completed), snap_id))
    return 0


def cmd_restore(root, args):
    manifest, snap_dir = load_manifest(root, args.snap_id)
    files = list(manifest["files"])
    if args.files:
        wanted = set(args.files)
        files = [item for item in files if item["path"] in wanted]
        if not files:
            raise ToolError("在快照中没有找到指定的文件")

    broken = []
    for item in files:
        source = snap_dir / "files" / item["path"]
        if not source.is_file():
            broken.append((item["path"], "备份文件缺失"))
        elif sha256_of(source) != item["sha256"]:
            broken.append((item["path"], "备份内容与清单不一致"))
    if broken:
        append_log_safe(root, id=args.snap_id, action="restore", status="failed",
                        operation="restore", reason=manifest.get("reason", ""), source=TOOL_NAME,
                        files=[item["path"] for item in files], completed=[],
                        error="快照完整性校验未通过")
        print("快照完整性校验未通过，已停止，未修改任何文件：")
        for rel, why in broken:
            print("  - {}：{}".format(rel, why))
        return 1

    rows = []
    for item in files:
        target = root / item["path"]
        current = sha256_of(target) if target.is_file() else None
        if current == item["sha256"]:
            action = "跳过（已一致）"
        elif target.is_file():
            action = "覆盖"
        else:
            action = "重建"
        rows.append({"path": item["path"], "current": current, "want": item["sha256"],
                     "action": action, "target": target})

    if not args.confirm:
        print("快照 {}｜原因：{}".format(args.snap_id, manifest.get("reason", "")))
        print("将处理以下文件：")
        print("  {:<14}{:<34}{:<18}{}".format("动作", "文件", "当前SHA256", "快照SHA256"))
        for row in rows:
            print("  {:<14}{:<34}{:<18}{}".format(row["action"], row["path"],
                                                  (row["current"] or "—")[:16], row["want"][:16]))
        print("仅预览，未修改任何文件。确认后加 --confirm。")
        return 0

    existing = [row["target"] for row in rows if row["target"].is_file()]
    pre_id, pre = None, {}
    if existing:
        try:
            pre_id, pre_manifest = take_snapshot(
                root, existing, "pre-restore", "恢复 {} 前保护".format(args.snap_id), TOOL_NAME)
        except LoggedError as exc:
            print("恢复前快照失败，已停止：" + str(exc))
            return 1
        pre = {item["path"]: item["sha256"] for item in pre_manifest["files"]}
        print("恢复前已保存当前版本：" + pre_id)

    op = {"schema": SCHEMA, "id": "", "ts": now_iso(), "kind": "edit",
          "reason": "恢复 " + args.snap_id, "snapshot": pre_id,
          "declared": [row["path"] for row in rows], "pre_sha256": pre,
          "post_sha256": {}, "status": "open", "error": None, "from": None, "to": None}
    op = create_op(root, op)

    completed, failed = [], []
    for row in rows:
        if row["action"].startswith("跳过"):
            completed.append(row["path"])
            continue
        try:
            row["target"].parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(snap_dir / "files" / row["path"]), str(row["target"]))
            if sha256_of(row["target"]) != row["want"]:
                raise ToolError("写入后校验不一致")
            completed.append(row["path"])
            op["post_sha256"][row["path"]] = row["want"]
        except Exception as exc:
            failed.append({"path": row["path"], "error": str(exc)})

    if failed:
        op["status"] = "failed"
        op["error"] = "部分文件恢复失败"
        save_op(root, op)
        state = load_state(root)
        if state:
            for rel, digest in op["post_sha256"].items():
                state["files"][rel] = {"sha256": digest, "op": op["id"], "ts": now_iso()}
            save_state(root, state)
        append_log_safe(root, id=op["id"], action="restore", status="partial",
                        operation="restore", reason=op["reason"], source=TOOL_NAME,
                        files=op["declared"], completed=completed,
                        failed=[item["path"] for item in failed], error=op["error"])
        print("恢复部分失败：成功 {}，失败 {}".format(len(completed), len(failed)))
        for item in failed:
            print("  失败：{}（{}）".format(item["path"], item["error"]))
        print("说明：失败项保持原样；本次恢复前的版本在快照 {} 中。".format(pre_id))
        return 1

    op["status"] = "verified"
    save_op(root, op)
    advance_state(root, op)
    append_log_safe(root, id=op["id"], action="restore", status="ok", operation="restore",
                    reason=op["reason"], source=TOOL_NAME, files=op["declared"],
                    completed=completed, error=None)
    print("恢复完成并推进基线：成功 {} 个".format(len(completed)))
    return 0


def cmd_verify(root, args):
    manifest, _ = load_manifest(root, args.snap_id)
    ok, bad = 0, []
    for item in manifest["files"]:
        target = root / item["path"]
        if not target.is_file():
            bad.append((item["path"], "文件不存在"))
        elif sha256_of(target) != item["sha256"]:
            bad.append((item["path"], "内容与快照不一致"))
        else:
            ok += 1
    print("校验快照 {}：通过 {} 个，失败 {} 个".format(args.snap_id, ok, len(bad)))
    for rel, why in bad:
        print("  - {}：{}".format(rel, why))
    return 0 if not bad else 1


def format_bytes(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "{:.0f}{}".format(size, unit) if unit == "B" else "{:.1f}{}".format(size, unit)
        size /= 1024.0
    return "{}B".format(size)


def cmd_list(root, args):
    dirs = snapshot_dirs(root)
    if not dirs:
        print("暂无快照。")
        return 0
    shown = dirs[-args.limit:] if args.limit else dirs
    print("共 {} 个快照，显示 {} 个（新→旧）：".format(len(dirs), len(shown)))
    print("  {:<22}{:<22}{:<8}{:<10}{}".format("编号", "时间", "文件数", "占用", "原因"))
    for snap_dir in reversed(shown):
        manifest_file = snap_dir / MANIFEST
        if not manifest_file.is_file():
            print("  {:<22}{}".format(snap_dir.name, "清单缺失"))
            continue
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
        except ValueError:
            print("  {:<22}{}".format(snap_dir.name, "清单损坏"))
            continue
        total = sum(item.get("size", 0) for item in data.get("files", []))
        print("  {:<22}{:<22}{:<8}{:<10}{}".format(
            data.get("id", snap_dir.name), data.get("created_at", "")[:19],
            str(len(data.get("files", []))), format_bytes(total), data.get("reason", "")[:30]))
    return 0


def cmd_show(root, args):
    data, _ = load_manifest(root, args.snap_id)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_log(root, args):
    path = log_path(root)
    if not path.is_file():
        print("暂无变更日志。")
        return 0
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.tail:
        lines = lines[-args.tail:]
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            print("（无法解析的日志行）" + line[:80])
            continue
        print("{:<20}{:<10}{:<8}{:<20}{}".format(
            item.get("ts", "")[:19], item.get("action", ""), item.get("status", ""),
            item.get("id", "")[:20], item.get("reason", "")[:36]))
        if item.get("error"):
            print("    error: " + str(item["error"])[:160])
    return 0


def cmd_prune(root, args):
    dirs = snapshot_dirs(root)
    if len(dirs) <= args.keep:
        print("共 {} 个快照，未超过保留数量 {}，无需清理。".format(len(dirs), args.keep))
        return 0
    doomed = dirs[:len(dirs) - args.keep]
    if not args.confirm:
        print("将删除以下旧快照（共 {} 个，保留最新 {} 个）：".format(len(doomed), args.keep))
        for snap_dir in doomed:
            print("  - " + snap_dir.name)
        print("仅预览，未删除任何快照。确认后加 --confirm。")
        return 0
    completed, failed = [], []
    for snap_dir in doomed:
        try:
            shutil.rmtree(str(snap_dir))
            completed.append(snap_dir.name)
        except OSError as exc:
            failed.append({"path": snap_dir.name, "error": str(exc)})
    append_log_safe(root, id="-", action="prune", status="ok" if not failed else "partial",
                    operation="prune", reason="保留最新 {} 个".format(args.keep),
                    source=TOOL_NAME, files=[], completed=completed,
                    failed=[item["path"] for item in failed],
                    error=None if not failed else "部分快照删除失败")
    print("清理完成：删除 {} 个，失败 {} 个".format(len(completed), len(failed)))
    return 0 if not failed else 1


# ---------------------------------------------------------------- 入口

def build_parser():
    parser = argparse.ArgumentParser(prog=TOOL_NAME, description="快照 / 受控写入 / 审计")
    parser.add_argument("--version", action="version", version="{} {}".format(TOOL_NAME, TOOL_VERSION))
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="建立基线")
    p_init.add_argument("--force", action="store_true")

    p_begin = sub.add_parser("begin", help="写前：快照并生成操作编号")
    p_begin.add_argument("--files", nargs="+")
    p_begin.add_argument("--project")
    p_begin.add_argument("--kind", default="edit", choices=KINDS)
    p_begin.add_argument("--from", dest="from_path")
    p_begin.add_argument("--to", dest="to_path")
    p_begin.add_argument("--reason", required=True)

    p_end = sub.add_parser("end", help="写后：校验并推进基线")
    p_end.add_argument("op_id")

    p_write = sub.add_parser("write", help="受控写入（校验写前状态）")
    p_write.add_argument("--op", dest="op_id", required=True)
    p_write.add_argument("--file", required=True)
    p_write.add_argument("--source", required=True)

    p_audit = sub.add_parser("audit", help="审计未处理的安全异常")
    p_audit.add_argument("--verbose", action="store_true")
    p_audit.add_argument("--strict", action="store_true", help="存在 open 操作时返回非零")

    p_resolve = sub.add_parser("resolve", help="处理 failed 操作（不推进基线）")
    p_resolve.add_argument("op_id")
    p_resolve.add_argument("--reason", required=True)
    p_resolve.add_argument("--note", default="")

    p_snap = sub.add_parser("snapshot", help="旧式备份（不推进基线）")
    p_snap.add_argument("files", nargs="+")
    p_snap.add_argument("--reason", required=True)
    p_snap.add_argument("--op", default="manual", choices=OPS)

    p_del = sub.add_parser("delete", help="先保存副本再删除（生成操作记录）")
    p_del.add_argument("files", nargs="+")
    p_del.add_argument("--reason", required=True)
    p_del.add_argument("--confirm", action="store_true")

    p_rest = sub.add_parser("restore", help="从快照恢复（默认只预览）")
    p_rest.add_argument("snap_id")
    p_rest.add_argument("--files", nargs="+")
    p_rest.add_argument("--confirm", action="store_true")

    p_ver = sub.add_parser("verify", help="校验文件是否与快照一致")
    p_ver.add_argument("snap_id")

    p_list = sub.add_parser("list", help="列出快照")
    p_list.add_argument("--limit", type=int, default=0)

    p_show = sub.add_parser("show", help="查看快照清单")
    p_show.add_argument("snap_id")

    p_log = sub.add_parser("log", help="查看变更日志")
    p_log.add_argument("--tail", type=int, default=20)

    p_prune = sub.add_parser("prune", help="清理旧快照（默认只预览）")
    p_prune.add_argument("--keep", type=int, default=20)
    p_prune.add_argument("--confirm", action="store_true")

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
    handlers = {
        "init": cmd_init, "begin": cmd_begin, "end": cmd_end, "write": cmd_write,
        "resolve": cmd_resolve, "audit": cmd_audit, "snapshot": cmd_snapshot, "delete": cmd_delete,
        "restore": cmd_restore, "verify": cmd_verify, "list": cmd_list,
        "show": cmd_show, "log": cmd_log, "prune": cmd_prune,
    }
    try:
        return handlers[args.command](root, args)
    except LoggedError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1
    except ToolError as exc:
        append_log_safe(root, id="-", action=args.command, status="failed",
                        operation=getattr(args, "kind", args.command),
                        reason=getattr(args, "reason", ""), source=TOOL_NAME,
                        files=list(getattr(args, "files", []) or []), completed=[],
                        error=str(exc))
        print("错误：" + str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        append_log_safe(root, id="-", action=args.command, status="failed",
                        operation=getattr(args, "kind", args.command),
                        reason=getattr(args, "reason", ""), source=TOOL_NAME,
                        files=list(getattr(args, "files", []) or []), completed=[],
                        error=str(exc))
        print("错误：" + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
