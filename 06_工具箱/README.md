# 工具箱

- [新建项目.py](新建项目.py)：默认预览；`--confirm` 才写入。写总表前自动 `begin`，写入后 `end`，失败即停。
- [快照.py](快照.py)：受控写入（begin/end/write）、审计（audit）、快照、恢复与校验。所有路径相对工作区根目录。
- [资料索引.py](资料索引.py)：知识库 / 技能库索引的登记、更新、移除与一致性检查；写入自动 begin/end。
- [进度同步.py](进度同步.py)：从 `任务与进度.md` 派生项目总表摘要；`check` 只读，`sync` 需 `--confirm` 并自动 begin/end。
- [检查工作区.py](检查工作区.py)：只读体检，汇总结构、总表、三件套、索引、进度与安全审计（严格模式）。
- [任务路由说明.md](任务路由说明.md)：路由细则、检索方法、示例与反例。
- [模板/](模板/)：项目初始文档模板。
- [Git使用说明.md](Git使用说明.md)：项目级 Git 安全流程。

## 受控写入（2.0）

```sh
# 写前：快照 + 操作编号
python3 06_工具箱/快照.py begin --files 文件名.md --reason "原因"
python3 06_工具箱/快照.py begin --project "项目名" --reason "原因"

# 编辑完成后
python3 06_工具箱/快照.py end <操作编号>
python3 06_工具箱/快照.py audit --strict
```

删除、移动、新建：

```sh
python3 06_工具箱/快照.py delete --reason "原因" 文件.md --confirm
python3 06_工具箱/快照.py begin --kind move --from 旧.md --to 新.md --reason "原因"
python3 06_工具箱/快照.py begin --kind create --files 新.md --reason "原因"
```

失败操作处理：`快照.py resolve <编号> --reason "原因"`（只记录处理历史，不推进基线）。

恢复：

```sh
python3 06_工具箱/快照.py restore <快照编号>
python3 06_工具箱/快照.py restore <快照编号> --confirm
python3 06_工具箱/快照.py verify <快照编号>
```

日常检查：

```sh
python3 06_工具箱/检查工作区.py
python3 06_工具箱/资料索引.py check
python3 06_工具箱/进度同步.py check
python3 06_工具箱/快照.py list
```

说明：旧的 `snapshot` 命令仍可做临时备份，但**不产生操作记录、不推进审计基线**；受控写入请用 `begin/end`。脚本按自身位置自动定位工作区根目录，与本机路径无关。若环境不允许命令执行，停止并解释，不要绕过权限。
