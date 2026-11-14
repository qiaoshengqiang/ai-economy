# 平台兼容性（Platform Compatibility）

> **状态**
>
> - **macOS**：当前主要开发 / 验证环境（`CURRENT`）
> - **Linux**：预期可用，尚未建立完整 CI（`UNVERIFIED`）
> - **Windows 原生**：**未支持**；适配层骨架 `DESIGNED`（实现入口见 §0）
> - **WSL**：需要单独处理路径语义（`EXPERIMENTAL`）
>
> 本文是跨平台适配的**唯一入口**：
> 既列出真实差异，也说明适配代码应该写在哪、怎么验收。
> 当前阶段不承诺 Windows 可用；骨架已就位，欢迎按 §0 逐项完善。

---

## 0. Windows 开发者入口（先读这一节）

> **一句话：不是复制一套程序，而是新增一个适配器。**
>
> 平台差异已经收敛到 `core/platform_support/`。业务代码不判断
> 操作系统，只调用能力接口。你在 Windows 上的工作，基本只碰一个文件。

### 0.1 架构

```text
main.py 启动
  └── select_platform()                 ← 唯一一次平台判断
        ├── PosixAdapter                ← macOS / Linux（CURRENT）
        └── WindowsAdapter              ← 骨架（DESIGNED）
              ↓
        PlatformAdapter（接口）
        ├── spawn_kwargs()              子进程启动参数
        ├── kill_tree()                 超时终止整棵进程树
        ├── file_lock()                 项目锁 / Snapshot 锁
        ├── file_unlock()
        ├── create_symlink()            快照恢复
        └── chmod()                     权限位（Windows 有意 no-op）
```

启动日志会打印：

```text
平台适配：adapter=posix
平台适配未完成：adapter=windows 缺失=kill_tree（WIN-PROC-1）, ...
```

### 0.2 你要改哪些文件

| 文件 | 做什么 | 优先级 |
|---|---|---|
| `core/platform_support/windows.py` | 实现 3 个 TODO 能力 | **P0** |
| `tests/test_platform_support.py` | 补 Windows 行为测试 | P0（随实现一起） |
| `start_aieos.ps1` / `start_aieos.bat` | Windows 启动脚本 | P1 |
| `ui/static/app.js` / `ui/server.py` | 路径显示 / 盘符 / UNC | P1 |
| 本文档 | 更新状态表与验收清单 | 每个 PR |

**不要改**：Task Core、Ledger、协议、数据库 schema、`shell=False` 的
Tool Runtime 边界、`workspace` 权限模型。

### 0.3 三个 TODO

| TODO | 能力 | 必须满足 | 建议实现 |
|---|---|---|---|
| **WIN-PROC-1** | `kill_tree` | 终止整棵进程树，不留孤儿 | `taskkill /F /T /PID <pid>`，或 Windows Job Object |
| **WIN-LOCK-1** | `file_lock` / `file_unlock` | 保持互斥；`blocking=False` 拿不到锁时抛 `BlockingIOError` | `msvcrt.locking(fd, LK_NBLCK / LK_LOCK, 1)`；释放用 `LK_UNLCK` |
| **WIN-FS-1** | `create_symlink` | 成功，或给出可操作的错误 | `os.symlink`；权限不足时提示开启开发者模式 / 用 WSL |

实现骨架：

```python
# core/platform_support/windows.py
def file_lock(self, handle, *, blocking=True):
    import msvcrt
    handle.seek(0)                       # locking 从当前文件位置开始
    mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
    try:
        msvcrt.locking(handle.fileno(), mode, 1)
    except OSError as exc:
        if not blocking:
            raise BlockingIOError(str(exc)) from exc
        raise

def file_unlock(self, handle):
    import msvcrt
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
```

> ⚠️ `msvcrt.locking` 是按字节范围加锁，且要求文件至少有 1 字节可锁。
> `LK_LOCK` 会重试约 10 秒后失败，不是无限阻塞；如果语义要求无限等待，
> 需要自己加重试循环。实现后必须用两个句柄做互斥测试，确认第二个
> `blocking=False` 真的失败。

### 0.4 失败语义（硬要求）

- 未实现的能力**抛 `PlatformCapabilityError`，不能 no-op**。
- 不允许“先跑起来再说”的降级：
  - 项目锁 no-op → 两个 Task 同时写坏用户项目；
  - 只杀父进程 → 子进程残留、端口占用；
  - symlink 改成复制 → 快照语义改变。
- 实现完成后，`WindowsAdapter.missing_capabilities()` 应返回空列表。

### 0.5 验证步骤

```bash
# 1. 适配器契约（在 Windows 上跑）
python -m pytest tests/test_platform_support.py -q

# 2. 平台边界守卫（必须零违规）
python -m pytest tests/test_platform_guards.py -q

# 3. 全量测试（macOS 基线也不能退化）
./dev.sh test
```

> 当前 `tests/test_platform_support.py` 里只有 POSIX 行为测试带
> `@POSIX_ONLY` 标记；平台选择、Windows 骨架、守卫测试在 Windows
> 主机上可以直接运行。`./dev.sh test` 需要 Git Bash / WSL；
> 原生 PowerShell 先补 `dev.ps1`（P1）。

### 0.6 守卫规则

`tests/test_platform_guards.py` 用 AST 扫描生产代码，以下内容
**只允许出现在 `core/platform_support/`**：

```text
import fcntl / msvcrt / winreg / pwd / grp / resource
os.killpg / os.getpgid / os.symlink / os.chmod / os.getuid ...
os.name / sys.platform / platform.system
subprocess.Popen(..., start_new_session=True)
```

违反 → 测试变红。这样 Windows 特判不会散落回业务代码。

### 0.7 PR 拆分建议

```text
fork → branch feat/windows-support
  PR 1  file_lock / file_unlock          ← 先解除最严重的硬阻断
  PR 2  kill_tree
  PR 3  create_symlink
  PR 4  start_aieos.ps1 + dev.ps1
  PR 5  UI 路径 / 盘符 / UNC
  PR 6  windows-latest CI
```

每个 PR 的合并条件：

1. macOS 全量测试仍为基线通过；
2. 只改允许的范围；
3. 更新本文状态表；
4. 不引入未讨论的新依赖。

### 0.8 当前不能承诺什么

- P0 未完成前，README / UI 不得写“支持 Windows”。
- WSL 与原生 Windows 是两套目标，不能混在一起验收。
- Windows 上的 Pi CLI、bash 工具、语音输入仍属于未验证区域。

---

## 1. 总览

当前代码里仍存在的平台假设：

```text
POSIX 路径 / 大小写敏感
bash / nohup / lsof / ps
.venv/bin/python
node_modules/.bin/pi
符号链接 / chmod
```

已收敛到 `core/platform_support/` 的假设：

```text
fcntl.flock        → PlatformAdapter.file_lock
os.killpg          → PlatformAdapter.kill_tree
os.symlink         → PlatformAdapter.create_symlink
os.chmod           → PlatformAdapter.chmod
start_new_session  → PlatformAdapter.spawn_kwargs
```

这些在 Windows 原生环境仍然不成立，但**不会再散落在业务代码里**。

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
| 语音输入 | Windows 未实现；macOS 前端暂时隐藏（代码保留） | P2 |

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

### 3.1 ProjectLock：接口已统一，Windows 实现待补

位置：

```text
work/project_lock.py
core/platform_support/base.py
core/platform_support/posix.py
core/platform_support/windows.py
```

当前（`CURRENT`）：

```python
# work/project_lock.py
self.platform.file_lock(handle, blocking=False)   # 拿不到锁 → BlockingIOError
```

- POSIX：`fcntl.flock(LOCK_EX | LOCK_NB)`
- Windows：`WindowsAdapter.file_lock` **尚未实现**，会抛
  `PlatformCapabilityError`，**不会静默跳过**
- 第二个任务拿不到锁时，`ProjectLockManager` 仍然抛 `ProjectBusyError`

历史问题（已修复）：旧实现用 `if fcntl is not None` 包住加锁，
Windows 上会直接跳过，导致两个 Task 同时修改同一个项目目录而无任何报错。

待做：

- [ ] Windows：`msvcrt.locking`（或等价实现）
- [ ] 保持 `blocking=False` 时抛 `BlockingIOError` 的契约

### 3.2 TaskSnapshotStore：同样走统一接口

位置：

```text
work/task_snapshot.py  TaskSnapshotStore._lock()
```

当前：与 ProjectLock 共用 `PlatformAdapter.file_lock`。
Windows 上会显式抛 `PlatformCapabilityError`，不会在无锁状态下写快照。

待做：

- [ ] Windows 实现后，验证 snapshot capture / restore
- [ ] 验证并发 capture 不会写坏 index

### 3.3 静默降级已移除

旧实现是：

```text
没有 fcntl → 不锁
```

现在（`CURRENT`）：

```text
Windows → PlatformCapabilityError（显式失败）
```

不能静默地“看起来有锁，实际上没有”。启动日志会列出缺失能力，
任务在真正用到该能力时失败，并在异常信息里指向本文档。

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
self.platform.chmod(target, 0o755 if mode == "100755" else 0o644)
```

- POSIX：`os.chmod`，行为与之前一致。
- Windows：`WindowsAdapter.chmod` 是**有意的 no-op**。
  Windows 没有 POSIX mode，权限由 ACL 管理；不要把它当安全边界。

需要验证：

- `git ls-tree` 的模式；
- `.bat` / `.cmd` 是否需要额外启动方式（不是 chmod 能解决的）。

### 6.2 符号链接

`TaskSnapshotStore` 支持 `120000` symlink：

```python
self.platform.create_symlink(data.decode("utf-8"), target)
```

- POSIX：`os.symlink`。
- Windows：**尚未实现**，会显式抛 `PlatformCapabilityError`。
  实现时不要静默改成“复制文件”——那会改变快照语义。
  权限不足时应提示开启开发者模式，或明确要求 WSL。

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

已补（`CURRENT`）：

```text
tests/test_platform_support.py   ← 适配器契约测试（POSIX 行为）
tests/test_platform_guards.py    ← AST 守卫：平台 API 不得出现在适配器外
```

守卫覆盖：

```text
import fcntl / msvcrt / winreg / pwd / grp / resource
os.killpg / os.getpgid / os.symlink / os.chmod
sys.platform
subprocess.Popen(..., start_new_session=True)
```

仍缺：

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

- [x] 平台能力接口 `PlatformAdapter`（进程 / 锁 / 符号链接 / 权限）
- [x] ProjectLock 与 SnapshotLock 在 Windows 上不再静默失效
- [ ] `WindowsAdapter.file_lock` / `file_unlock`（`msvcrt.locking`）
- [ ] `WindowsAdapter.kill_tree`（taskkill / Job Object）
- [ ] `WindowsAdapter.create_symlink`（含权限错误提示）
- [ ] 工作区 chip basename 同时支持 `/` 和 `\`
- [ ] 目录浏览器盘符 / UNC
- [ ] Pi CLI `.cmd` / `.exe` 解析
- [ ] Pi bash shell 类型明确

> 前两项是本次已完成的**架构准备**；Windows 上仍需实现三个
> `WindowsAdapter` 方法，任务才能真正跑起来。

### P1

- [ ] `start_aieos.ps1` 或 `.bat`
- [ ] `.venv\Scripts\python.exe`
- [ ] Git snapshot / patch / worktree
- [ ] hardlink / chmod 行为验证（symlink 已在 P0）
- [ ] 长路径 / UNC
- [ ] Windows CI smoke

### P2

- [ ] WSL 路径转换
- [ ] Windows 忽略文件
- [ ] Windows 权限 / ACL 文档
- [ ] Windows 服务化 / 后台运行
- [ ] Windows 语音输入（Web Speech API / 原生引擎）

---

## 10. 语音输入

### 10.1 当前实现

语音输入使用**浏览器 Web Speech API**，不经过 AIEOS 服务端，也不上传音频到
AIEOS：

```text
ui/static/index.html   mic-btn
ui/static/app.js       SpeechRecognition / webkitSpeechRecognition
ui/static/style.css    .mic-btn / .mic-btn.recording
```

- 语言固定 `zh-CN`；
- 连续识别 + 中间结果；
- 识别结果插入光标位置，不覆盖已有文字；
- 点击麦克风开始，再点一次停止；提交时自动停止；
- 浏览器不支持时按钮隐藏；
- Windows 当前直接隐藏按钮（`MIC_PLATFORM_SUPPORTED`）；
- **前端暂时隐藏**：`VOICE_INPUT_ENABLED=false`，按钮不显示；
  代码保留，后续把开关改为 `true` 即可恢复。

### 10.2 平台状态

| 平台 / 浏览器 | 状态 | 说明 |
|---|---|---|
| macOS Safari | `HIDDEN` | 代码保留；`webkitSpeechRecognition` 使用 Apple 语音识别 |
| macOS Chrome | `HIDDEN` | 代码保留；Web Speech API，识别服务可能依赖网络 |
| Windows | `UNSUPPORTED` | 当前隐藏按钮，未验证 Edge / Chrome |
| Linux | `UNVERIFIED` | 浏览器支持时可能可用，未验证 |
| Firefox | `UNSUPPORTED` | Web Speech API 支持有限，按钮通常隐藏 |

### 10.3 使用条件

- 页面必须运行在安全上下文：`localhost` / `127.0.0.1` 或 HTTPS；
  通过局域网 IP 的普通 HTTP 打开时，浏览器会拒绝麦克风。
- 浏览器首次使用会请求麦克风权限；macOS 还需要：
  `系统设置 → 隐私与安全性 → 麦克风` 允许对应浏览器。
- macOS Chrome 的识别通常依赖网络；Safari 使用 Apple 的语音识别。
- 当前没有调用原生 `SFSpeechRecognizer`，也没有本地语音模型。
  如果要求 Chrome 也强制走 macOS 原生引擎，需要单独做 native bridge。

### 10.4 Windows 后续

Windows 支持至少需要：

- 验证 Edge / Chrome 的 Web Speech API 是否可用；
- 或接 Windows 原生语音识别；
- 处理系统麦克风权限；
- 在 `PLATFORM_COMPATIBILITY` 与 UI 上区分平台；
- 增加 Windows CI / 手工冒烟。

---

## 11. 当前建议

```text
现在：只承诺 macOS 可用。
Linux：可以试用，但不作为正式支持。
Windows：不承诺；本文作为社区贡献入口。
```

在 P0 完成前，不建议在 README 写“支持 Windows”。

---

## 12. 相关文件

| 文件 | 作用 |
|---|---|
| `core/platform_support/base.py` | **平台能力接口**（进程 / 锁 / symlink / chmod） |
| `core/platform_support/posix.py` | macOS / Linux 实现（`CURRENT`） |
| `core/platform_support/windows.py` | Windows 骨架（`DESIGNED`）—— 你要改的文件 |
| `work/project_lock.py` | 项目锁，调用 `PlatformAdapter.file_lock` |
| `work/task_snapshot.py` | Snapshot 锁、symlink、chmod、hardlink |
| `tool_runtime/runner.py` | 子进程启动参数、超时杀进程树 |
| `work/settings_admin.py` | 配置文件权限位 |
| `ui/server.py` | 目录浏览器 API、Pi 默认路径 |
| `ui/static/app.js` | 工作区 chip、目录 picker |
| `work/pi_rpc.py` | Pi 子进程 |
| `work/pi_runtime.py` | Pi cwd / 环境变量 / shell 工具 |
| `start_aieos.sh` | 当前唯一启动脚本 |
| `tool_runtime/` | 受控工具执行 / Python 路径 |
| `tests/test_platform_support.py` | 适配器契约测试 |
| `tests/test_platform_guards.py` | 平台边界守卫（AST） |
| `tests/` | POSIX 假设与 Windows CI 缺口 |
