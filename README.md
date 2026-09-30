# Codex Traffic Light MXP — Windows 版

[langkonzil/codex-traffic-light-mxp](https://github.com/langkonzil/codex-traffic-light-mxp)
（macOS Swift/AppKit 版）的 **Windows 移植版**：用纯 Python 3 标准库（tkinter + winsound）实现，
**无任何第三方依赖**，命令、hooks 协议、状态文件 JSON 格式与原版完全兼容——同一份
`state.json` 在 macOS / Windows 之间可直接互换。

当前 UI 为 **304×60 动态岛悬浮窗**：左侧状态点与中文状态、右侧红黄绿三灯。
Windows 显示层使用系统自带 GDI+ 抗锯齿和 `UpdateLayeredWindow` 逐像素 Alpha，
使圆角与阴影平滑融入桌面；不需要额外安装图形库。浮窗和右键菜单不显示额度，
额度采集与命令行查询仍保留。

> 许可证：Apache License 2.0（原项目同为 Apache 2.0，本移植版保留原作者版权与
> 署名声明，详见 [NOTICE](NOTICE) 与 [LICENSE](LICENSE)。

悬浮红绿灯用颜色提示 Codex 当前状态：

| 颜色 | 状态 | 含义 | 提示行为 |
| --- | --- | --- | --- |
| 🟡 黄灯 | `working` | Codex 正在工作 | 静默显示 |
| 🟢 绿灯 | `done` | 任务已完成，可以验收 | 播放提示音 3 秒，10 分钟后自动回到空闲 |
| 🔴 红灯 | `waiting` | 需要你回复、确认、授权 | 闪烁并播放提示音 10 秒，随后保持红灯静默 |
| ⚫ 暗灯 | `idle` | 没有活跃任务 | 静默显示 |

多任务聚合优先级：`waiting` > `working` > 最近 10 分钟内的 `done` > `idle`（与原版一致）。

## 目录结构

```
codex-traffic-light-win/
├── codex-light-mxp.py           # 命令行控制（与原版同名同参数）
├── codex-light-hook-mxp.py      # Codex hooks 桥（stdin 读 hook JSON）
├── run_app.pyw                  # GUI 启动（pythonw 无控制台窗口）
├── codex_light/
│   ├── core.py                  # 状态机 / 状态文件 / 环境变量默认值
│   ├── hook.py                  # hook 事件解析 / 映射 / 日志
│   ├── quota.py                 # 额度提取 + codex app-server JSON-RPC 采集
│   ├── layered.py               # Windows GDI+ 抗锯齿 / 逐像素 Alpha 渲染
│   └── app.py                   # tkinter 悬浮红绿灯 GUI
├── hermes-watcher.py            # Hermes 会话状态监测
├── qwen-watcher.py              # 千问办公活动监测
├── codex-traffic-light-mxp.vbs  # 自启动与进程守护模板
├── light-on.vbs                 # 退出后重新开启红绿灯
├── install.bat / uninstall.bat  # 安装 / 卸载
├── hooks.example.win.toml       # hooks 配置模板（Windows 路径）
└── tests/test_smoke.py          # 测试（无需 pytest）
```

## 快速开始

### 1. 安装

双击 `install.bat`（或 `cmd /c install.bat`），它会：

1. 把程序复制到 `%LOCALAPPDATA%\CodexTrafficLight\app`
2. 在 `%USERPROFILE%\.codex\bin\` 生成 `codex-light-mxp.cmd` / `codex-light-hook-mxp.cmd` 命令
3. 在「启动」文件夹放一个 VBS 实现开机自启
4. 生成带真实路径的 hooks 配置：`%LOCALAPPDATA%\CodexTrafficLight\hooks.win.generated.toml`

自启动守护每 30 秒检查 GUI 与 watcher。通过右键菜单选择“退出并不再自动启动”后，
可运行 `%LOCALAPPDATA%\CodexTrafficLight\light-on.vbs` 重新开启。

### 2. 启动红绿灯

```bat
pythonw "%LOCALAPPDATA%\CodexTrafficLight\app\run_app.pyw"
```

或手动运行（开发模式，能看到日志）：

```bat
python run_app.pyw
```

- **拖动**：按住左键拖到任意位置（位置会记住）
- **双击**：隐藏/显示
- **右键**：菜单（手动设状态、静音、清空失联任务、退出）

### 3. 接入 Codex Hooks

把 `%LOCALAPPDATA%\CodexTrafficLight\hooks.win.generated.toml` 里的 `[hooks]` 块合并进
`%USERPROFILE%\.codex\config.toml`，然后在 Codex 里运行 `/hooks` 并信任这些命令。
之后 Codex 每次 `UserPromptSubmit`（→黄）、`PermissionRequest`（→红）、`Stop`（→绿）都会自动变灯。

> 手动对照模板：`hooks.example.win.toml`（把 `<USERPROFILE>` 换成真实目录）。

### 4. 命令行用法（与原版一致）

```bat
codex-light-mxp working          rem 手动设黄灯
codex-light-mxp done             rem 绿灯
codex-light-mxp waiting          rem 红灯
codex-light-mxp idle             rem 全暗
codex-light-mxp status           rem 查询（输出 idle/working/done/waiting/quit）
codex-light-mxp --json status    rem JSON 快照
codex-light-mxp progress         rem 查询当前进度（人话摘要：灯色/聚合态/各任务/额度）
codex-light-mxp progress --json  rem 结构化进度快照（codex 推荐用这个读取当前进度）
codex-light-mxp clear            rem 清空失联任务（保留额度）
codex-light-mxp quit             rem 写入 quit，GUI 检测到后自动退出
codex-light-mxp --task demo-a working
codex-light-mxp quota --five-hour 72 --weekly 48
echo {"quota":{"five_hour_remaining_percent":72,"weekly_remaining_percent":48}} | codex-light-mxp quota --stdin
codex-light-mxp quota --app-server --json    rem 通过 codex app-server 读真实额度
```

### 让 Codex 读取当前进度

红绿灯原本只给人看颜色，Codex 只能通过 hooks **写入**状态。现在新增了
`codex-light-mxp progress`，让 Codex（或你）用一条命令**读回当前进度**：

- `codex-light-mxp progress` —— 人话摘要：当前灯色、聚合状态、每个任务的中文
  状态 + 最后消息 + 工作目录 + 相对时间，以及额度。
- `codex-light-mxp progress --json` —— 结构化快照（`aggregate_state` /
  `aggregate_label` / `color` / `emoji` / `active_tasks` / `state_updated_at` /
  `quota` / `tasks[]`，每个任务含 `state`、`state_label`、`source`、
  `hook_event_name`、`message`、`workspace`、`updated_at`、`age_seconds`）。
  Codex 自己读进度时推荐用这个格式。

仓库根目录的 [`AGENTS.md`](AGENTS.md) 已经写好了给 Codex 的指引（怎么调
`progress`、返回什么），codex 在该目录工作时会自动读取它。

## Windows 适配说明（环境变量）

与原版唯一有意的差异是**默认数据目录**，其余环境变量全部保留、语义不变：

| 环境变量 | 说明 | Windows 默认值 |
| --- | --- | --- |
| `CODEX_TRAFFIC_LIGHT_STATE_PATH` | 状态文件路径覆盖 | —（未设时用默认目录） |
| `CODEX_TRAFFIC_LIGHT_DATA_DIR` | 数据目录覆盖（新增，便携安装用） | — |
| `CODEX_TRAFFIC_LIGHT_HOOK_LOG_PATH` | hook 日志路径覆盖 | — |
| `CODEX_TRAFFIC_LIGHT_QUOTA_LOG_PATH` | 额度采集日志路径覆盖（新增） | — |
| `CODEX_TRAFFIC_LIGHT_PREFERENCES_PATH` | 偏好文件路径覆盖（新增） | — |
| `CODEX_TRAFFIC_LIGHT_CODEX_BIN` | codex 可执行文件（app-server 采集用） | PATH 中的 `codex` |
| `CODEX_LIGHT_DONE_IDLE_SECONDS` | 绿灯自动回空闲秒数 | 600 |
| `CODEX_LIGHT_WAITING_ALERT_SECONDS` | 红灯闪烁/提示音持续秒数 | 10 |
| `CODEX_LIGHT_APP_SERVER_QUOTA_REFRESH_SECONDS` | 额度轮询间隔 | 300 |

默认数据目录（未覆盖时）：

- Windows：`%LOCALAPPDATA%\CodexTrafficLight\`
- macOS（兼容）：`~/Library/Application Support/CodexTrafficLight/`

运行期文件：

```
%LOCALAPPDATA%\CodexTrafficLight\state.json       状态（与原版格式相同）
%LOCALAPPDATA%\CodexTrafficLight\preferences.json 偏好（静音/显示等）
%LOCALAPPDATA%\CodexTrafficLight\hook-mxp.log     hooks 日志
%LOCALAPPDATA%\CodexTrafficLight\quota-mxp.log    额度采集日志
```

## 接入 Hermes（本机桌面 AI 助手）

`hermes-watcher.py` 把 **Hermes 桌面版**也接进红绿灯：每 2 秒只读轮询
`%LOCALAPPDATA%\hermes\state.db`（会话消息库），把 Hermes 的实时活动以任务
`hermes:watcher` 写入同一个 `state.json`，与 Codex 任务按原版优先级聚合
（`waiting` > `working` > `done` > `idle`）。Codex 和 Hermes 同时在跑时，
红灯/黄灯谁更紧急显示谁。

判定规则：

| 状态 | 条件 |
| --- | --- |
| 🟡 working | 最近 60 秒内有工具消息或 assistant 正在调工具（agent 在干活） |
| 🔴 waiting | 最后一条是 assistant 消息、内容在问你（复用 hook 的中文"等你回复"判定），且已停顿 15 秒 |
| 🟢 done | 最后一条是普通 assistant 回复，120 秒窗口内（然后按 10 分钟 TTL 自动熄灭） |
| ⚫ idle | 长时间无活动 → 任务自动移除 |

外部 `clear`/清空任务后，watcher 会在 2 秒内自动重建自己的任务。

`hermes-watcher.py` 环境变量（均可选）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `HERMES_LIGHT_DB_PATH` | `%LOCALAPPDATA%\hermes\state.db` | 会话数据库路径 |
| `HERMES_LIGHT_POLL_SECONDS` | 2 | 轮询间隔 |
| `HERMES_LIGHT_ACTIVITY_WINDOW_SECONDS` | 60 | 工具活动判定窗口 |
| `HERMES_LIGHT_WAIT_CONFIRM_SECONDS` | 15 | 最后消息带问句后确认"在等你"的停顿秒数 |
| `HERMES_LIGHT_DONE_WINDOW_SECONDS` | 120 | 答完消息保持绿灯的窗口 |
| `HERMES_LIGHT_LOG_PATH` | 数据目录\hermes-watcher.log | 状态变化日志 |

手动测试：

```bat
python hermes-watcher.py --once        rem 输出当前判定，如 working (tool_activity)
```

## 与原版的差异

- **UI 层**：AppKit 菜单栏 → 304×60 动态岛悬浮窗；Windows 使用 GDI+ 抗锯齿与逐像素 Alpha 合成，
  tkinter 负责事件、右键菜单和兼容回退。
- **提示音**：macOS 系统声音（Glass/Basso）→ Windows 系统声音（SystemAsterisk / SystemHand），
  时长与循环逻辑不变。
- **codex 二进制解析**：Windows 下 npm 安装的 codex 是 `.cmd` shim，采集器会自动经 `cmd /c` 启动；
  也可用 `CODEX_TRAFFIC_LIGHT_CODEX_BIN` 指向 `codex.exe`。
- **单实例**：通过绑定 `127.0.0.1:47780` 实现（重复启动直接退出）。
- 其余（状态机、聚合优先级、hooks 映射、JSON-RPC 协议、重试/节流策略）与原版 1:1 移植。

## 测试

```bat
python -X utf8 tests\test_smoke.py
```

覆盖：聚合优先级、done 过期、hook 事件映射（含中文"等你回复"判定）、额度提取/映射、
JSON-RPC 编解码、hook 日志格式、Windows 路径默认值、CLI 端到端冒烟（同原版 README 步骤）。

## 卸载

双击 `uninstall.bat`（移除开机自启、命令、程序目录；`config.toml` 里的 hooks 行需手动删）。
