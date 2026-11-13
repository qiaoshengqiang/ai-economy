# 平台兼容性（Platform Compatibility）

> **状态**
>
> - **macOS**：当前主要开发 / 验证环境（`CURRENT`）
> - **Linux**：预期可用，尚未建立完整 CI（`UNVERIFIED`）
> - **Windows 原生**：**未支持**（`UNSUPPORTED`）
> - **WSL**：需要单独处理路径语义（`EXPERIMENTAL`）
>
> 本文列出从 macOS 迁移到其他平台时需要改动的真实位置。
> 当前阶段不实现 Windows 兼容；开源后欢迎 Windows 开发者按本文逐项完善。

---

## 1. 总览

当前代码大量假设：

```text
POSIX 路径 / 大小写敏感
fcntl.flock
bash / nohup / lsof / ps
.venv/bin/python
node_modules/.bin/pi
符号链接 / chmod
```

这些假设在 macOS / Linux 成立，在 Windows 原生环境不成立。

| 区域 | Windows 风险 | 优先级 |
|---|---|---|
| 路径显示 / 目录选择 | 高 | P0 |
| 项目锁 / Snapshot 锁 | **高（当前会直接失效）** | P0 |
| Pi CLI / 子进程 | 高 | P0 |
| bash 工具 | 高 | P0 |
| 启动脚本 | 中 | P1 |
| Git 快照 / 文件模式 | 中 | P1 |
| 测试 / CI | 高 | P1 |
| 长路径 / UNC / 权限 | 中 | P2 |

---

## 2. 路径与目录选择

### 2.1 工作区 chip 只按 `/` 取 basename

位置：

```text
ui/static/app.js  renderWorkspace()
```

当前：

```js
value.replace(/\/+$/, "").split("/")
```

Windows 路径：

```text
C:\Users\foo\project
```

会得到完整路径，而不是 `project`。

需要改：

- 按 `/` 和 `\` 同时切分；或
- 后端返回 `display_name`，前端只显示该字段。

### 2.2 目录浏览器没有盘符切换

位置：

```text
ui/server.py  Handler._list_dirs()
```

当前从 `Path.home()` 开始，只能“上一级”。

Windows 需要：

- 根目录返回盘符列表：

```text
C:\
D:\
E:\
```

- 或增加“此电脑 / 盘符”入口；
- 保留绝对路径输入；
- 支持 UNC：`\\server\share\...`。

### 2.3 路径大小写

Windows 路径大小写不敏感：

```text
C:\Projects\Foo
c:\projects\foo
```

需要统一：

```text
normalize_path() = os.path.normcase(str(Path(...).resolve()))
```

用于：

- `work/controller.py  _open_project_task()`
- `work/project_lock.py  _lock_path()`
- `commit_requests` 唯一键
- 项目路径比较 / 去重

否则同一个项目可能被当成两个项目，生成两个锁。

### 2.4 Task ID 与 Windows 文件名

`work/task_directory.py` 的任务 ID 正则允许 `:`：

```text
^[A-Za-z0-9_.:-]{1,120}$
```

`:` 在 Windows 文件名中非法。

当前生成的 Task ID 是 `task_<hex>`，暂时安全；
但如果未来允许自定义 Task ID，需要限制 Windows 非法字符：

```text
< > : " / \ | ? *
```

### 2.5 长路径 / UNC

需要验证：

- `MAX_PATH` 长路径；
- `\\?\` 前缀；
- UNC 网络盘；
- 网络盘延迟 / 断线；
- 路径长度导致的 Git 失败。

Git snapshot 已设置 `longpaths = true`，但 Python / Pi / Git 组合仍需实测。

---

## 3. 文件锁（最严重）

### 3.1 ProjectLock 在 Windows 上不生效

位置：

```text
work/project_lock.py
```

当前：

```python
try:
    import fcntl
except ImportError:
    fcntl = None

if fcntl is not None:
    fcntl.flock(...)
```

Windows 没有 `fcntl`，所以：

```text
不会加锁
```

结果：

```text
两个 Task 可以同时修改同一个项目目录
```

这是 Windows 支持的**硬阻断**。

需要：

- POSIX：`fcntl.flock`
- Windows：`msvcrt.locking`
- 统一抽象为 `FileLock`
- `ProjectLockManager` 与 `TaskSnapshotStore` 共用
- 第二个任务失败时返回 `ProjectBusyError`

### 3.2 TaskSnapshotStore 的锁同样失效

位置：

```text
work/task_snapshot.py  TaskSnapshotStore._lock()
```

Windows 上同样会跳过锁。

需要与 ProjectLock 共用跨平台锁实现。

### 3.3 `fcntl` 的静默降级

当前是：

```text
没有 fcntl → 不锁
```

Windows 支持时应改为：

```text
没有 msvcrt → 启动失败 / 明确禁用并发
```

不能静默地“看起来有锁，实际上没有”。

---

## 4. Pi CLI 与子进程

### 4.1 Pi CLI 路径

当前默认：

```text
experiments/pi_compare/pi_node/node_modules/.bin/pi
```

这是 Unix 形式的 npm shim。

Windows 通常生成：

```text
pi.cmd
pi.ps1
pi
```

需要：

- 用 `shutil.which("pi")` / PATHEXT 解析真实可执行文件；
- 或显式配置 `pi.cmd` 路径；
- 验证 `subprocess.Popen` 对 `.cmd` 的行为；
- 必要时用 `cmd.exe /c` 包装。

涉及：

```text
work/pi_rpc.py
ui/server.py  --pi-cli 默认值
start_aieos.sh
```

### 4.2 bash 工具

Pi Work 原生工具包含：

```text
read / write / edit / bash / grep / find / ls
```

Windows 上 `bash` 可能走：

- PowerShell；
- Git Bash；
- WSL。

Agent 生成的命令可能包含 Unix 语法：

```text
cd /tmp
grep -R
find . -name
```

需要：

- 明确 Windows 使用哪种 shell；
- 在 Pi 系统提示中告知 shell 类型；
- 或在 Windows 上先限制为 read/write/edit；
- 增加 Windows smoke test。

### 4.3 环境变量路径

`work/pi_runtime.py` 传入：

```text
AIEOS_TASK_WORKSPACE
AIEOS_PRIVATE_WORKSPACE
AIEOS_MEMORY_DIR
PI_CODING_AGENT_DIR
```

路径值在 Windows 上会是反斜杠；需要确认 Pi / Node 能正确解析。

---

## 5. 启动脚本

当前只有：

```text
start_aieos.sh
```

依赖：

```text
bash
nohup
lsof
ps
.venv/bin/python
```

Windows 原生需要：

```text
start_aieos.ps1
start_aieos.bat
```

或者明确要求：

```text
Git Bash / WSL
```

涉及的默认值：

```text
python  = .venv/bin/python
pi CLI  = node_modules/.bin/pi
pid/log = runs/ui/...
```

Windows 虚拟环境路径是：

```text
.venv\Scripts\python.exe
```

---

## 6. Git / Snapshot

### 6.1 文件模式

`work/task_snapshot.py` 恢复文件时：

```python
os.chmod(target, 0o755 if mode == "100755" else 0o644)
```

Windows 没有 POSIX 执行位语义，需要验证：

- `git ls-tree` 的模式；
- `os.chmod` 在 Windows 的行为；
- `.bat` / `.cmd` 是否需要可执行位。

### 6.2 符号链接

`TaskSnapshotStore` 支持 `120000` symlink：

```python
os.symlink(...)
```

Windows 创建 symlink 需要权限 / 开发者模式；需要降级策略。

### 6.3 `os.link`

`work/task_snapshot.py` 使用 hardlink 加速 index：

```python
os.link(self.index_path, target)
```

Windows 支持 NTFS hardlink，但跨卷 / 网络盘可能失败；已有 `except OSError` 复制兜底，需要测试。

### 6.4 Git 配置

已设置：

```text
core.autocrlf = false
core.safecrlf = false
```

Windows 上仍需要验证：

- 换行是否污染 patch；
- `git apply --binary`；
- `git worktree`（Phase 2）；
- Git 安装路径 / `git.exe`。

### 6.5 忽略文件

默认忽略列表需要补充 Windows：

```text
Thumbs.db
desktop.ini
```

---

## 7. WSL

如果 AIEOS 跑在 WSL：

```text
Path 是 POSIX
Windows 路径 = /mnt/c/...
```

用户从资源管理器复制的：

```text
C:\Users\foo\project
```

不能直接使用。

需要决定：

- WSL 模式只接受 `/mnt/c/...`；
- 或增加 `C:\...` → `/mnt/c/...` 转换；
- UI 明确显示“WSL 路径”。

WSL 与原生 Windows 是两套兼容性目标，不能混为一谈。

---

## 8. 测试与 CI

当前测试大量假设 POSIX：

```text
/tmp
symlink
chmod
fcntl
shell 命令
```

Windows 支持至少需要：

```text
GitHub Actions: windows-latest
```

冒烟项：

- 目录选择 API：`C:\`、`D:\`、UNC；
- 工作区 chip basename；
- ProjectLock 同项目互斥；
- TaskSnapshotStore capture / diff / restore；
- Pi CLI 子进程；
- 启动脚本；
- Task Workspace + Project Workdir 各跑一次。

当前没有 Windows CI，因此不能声称支持。

---

## 9. Windows 支持验收清单

Windows 开发者接手时，建议按顺序：

### P0

- [ ] 跨平台 `FileLock`（fcntl / msvcrt）
- [ ] ProjectLock 和 SnapshotLock 都不再静默失效
- [ ] 工作区 chip basename 同时支持 `/` 和 `\`
- [ ] 目录浏览器盘符 / UNC
- [ ] Pi CLI `.cmd` / `.exe` 解析
- [ ] Pi bash shell 类型明确

### P1

- [ ] `start_aieos.ps1` 或 `.bat`
- [ ] `.venv\Scripts\python.exe`
- [ ] Git snapshot / patch / worktree
- [ ] 符号链接 / hardlink / chmod 降级
- [ ] 长路径 / UNC
- [ ] Windows CI smoke

### P2

- [ ] WSL 路径转换
- [ ] Windows 忽略文件
- [ ] Windows 权限 / ACL 文档
- [ ] Windows 服务化 / 后台运行

---

## 10. 当前建议

```text
现在：只承诺 macOS 可用。
Linux：可以试用，但不作为正式支持。
Windows：不承诺；本文作为社区贡献入口。
```

在 P0 完成前，不建议在 README 写“支持 Windows”。

---

## 11. 相关文件

| 文件 | 作用 |
|---|---|
| `work/project_lock.py` | 项目锁，当前 fcntl-only |
| `work/task_snapshot.py` | Snapshot 锁、symlink、chmod、hardlink |
| `ui/server.py` | 目录浏览器 API、Pi 默认路径 |
| `ui/static/app.js` | 工作区 chip、目录 picker |
| `work/pi_rpc.py` | Pi 子进程 |
| `work/pi_runtime.py` | Pi cwd / 环境变量 / shell 工具 |
| `start_aieos.sh` | 当前唯一启动脚本 |
| `tool_runtime/` | 受控工具执行 / Python 路径 |
| `tests/` | POSIX 假设与 Windows CI 缺口 |
