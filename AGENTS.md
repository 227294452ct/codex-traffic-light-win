# Codex Traffic Light — 给 codex 的进度读取说明

本目录是 **红绿灯（Codex Traffic Light MXP Windows 移植版）**。它用一个悬浮窗
红/黄/绿/暗灯实时显示各任务的当前状态（黄=正在干活 `working`、绿=完成 `done`、
红=等你回复 `waiting`、暗=空闲 `idle`），并把每个任务的状态写入同一个
`state.json`。

Codex（你）可以通过 `codex-light-mxp` 命令**读取当前进度**，不必自己去解析
`state.json` 的原始结构。

## 读取当前进度（一句话）

```bash
codex-light-mxp progress
```

返回一句人话摘要，例如：

```
🟡 红绿灯：黄灯 · 正在干活（2 个任务跟踪中）
· 状态更新 08-25 17:09:00 ｜ 额度：5小时剩72% · 周剩48%
▶ 🟡 [codex-hook] → session:01a03829-1d80-78e3-894c-e887d1d728c3
   · 「当前消息...」 · 3分钟前
   · 目录：F:\agent项目\Hermes\红绿灯
▶ 🟡 [hermes-watcher] → hermes:watcher
   · 「Hermes traffic light: working (user_message age=1s)」 · 刚刚
```

## 结构化进度（机器可读，推荐给你）

```bash
codex-light-mxp progress --json
```

输出一个干净的 JSON：`aggregate_state` / `aggregate_label` / `color` / `emoji` /
`active_tasks` / `state_updated_at` / `quota` / `tasks[]`（每个任务含 `state`、
`state_label`、`source`、`hook_event_name`、`message`、`workspace`、`updated_at`、
`age_seconds`）。你（codex）优先用这个格式来"读取当前进度"。

## 其它只读命令

| 命令 | 说明 |
| --- | --- |
| `codex-light-mxp status` | 只回一个裸状态名（working/done/waiting/idle/quit） |
| `codex-light-mxp status --json` | 原始 state.json 快照 |

## 常见用法（codex 场景）

- 接手任务前：先 `codex-light-mxp progress`，看有哪些任务在跑、当前整体是黄/绿/红。
- 干活中：不需要手动写状态—— hooks 会自动把 `UserPromptSubmit`/`PreToolUse` 置为
  黄（working）、`PermissionRequest` 置为红（waiting）、`Stop` 置为绿/红。
- 若你（codex）正卡在等用户回复，红绿灯会自动变红，用户能看到。

## 注意

- 首次使用前需确保命令在 `PATH` 里（install.bat 已把 `%USERPROFILE%\.codex\bin`
  加入 PATH，里面是 `codex-light-mxp.cmd` / `codex-light-hook-mxp.cmd`）。
- `progress` 是**只读**命令，不会改动任何状态。
