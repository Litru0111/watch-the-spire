# 🐋 Watch the Spire

**《杀戮尖塔 2》非官方数据监控台** —— 实时记录每层地图与路线、逐场战斗明细、
局后总结与全局统计，全部在一个本地网页里看。

---

> ## ⚠️ AI 生成声明
>
> **本项目绝大部分代码由 AI 编写**（DeepSeek Harness 驱动的编码 agent），
> 由人类提出需求、审阅结果并实际游玩验证。请据此判断是否使用：
>
> * **能保证的**：仓库里每个功能都有对应的自检脚本，当前全绿；
>   agent 在开发过程中记录了一批**真实发生过的 bug 与修法**
>   （见各仓库的提交信息与 `docs/`），不是"看起来能跑"。
> * **不能保证的**：AI 可能写出**只在作者数据上成立**的逻辑。
>   例如评分分位、楼层解析、多人局玩家归属等都依赖实测样本；
>   换一套存档或游戏版本更新后，可能给出**看似合理但错误**的数字。
> * **因此请把界面上的数字当作参考**，不要用于任何严肃决策。
>   发现不对时，欢迎带上 `sts2mon.cmd doctor` 的输出与相关存档提 issue。
> * 代码风格与注释密度可能不一致；部分注释记录了排查过程，
>   对维护有用，但也意味着读起来不像人类手写的代码那样均匀。
>
> 这不是免责声明式的套话 —— 项目的**能力边界**在下面「数据从哪来」一节
> 逐条列明，包括哪些数据**拿不到**、哪些是**近似值**、哪些**无法检测**。

---

> 与 Mega Crit 无关联。本项目不含任何游戏资源或游戏文案数据；
> 展示用的卡牌/遗物文案由 `sts2mon extract` 在你的机器上从你已拥有的游戏里生成。

## 亮点

- **零第三方依赖** —— 只用 Python 标准库（`http.server` / `sqlite3` / `dataclasses` / `struct`）。
  没有 `pip install`，没有前端构建，克隆下来就能跑。
- **实时采集** —— 轮询游戏写出的状态文件，变化即 diff 成事件流。
  开局就能监测（不必等到第一场战斗结束）。
- **单人 / 多人** —— 多人局按玩家分流，每人一套评分与事件回放，并带排行榜。
- **从游戏文件提取数据** —— 卡牌/遗物文案与数值直接从 `.pck` 本地化表与
  .NET 程序集里解析出来（纯 Python 实现的元数据与 IL 解析，不依赖 dnSpy 之类工具）。
- **黑箱对局模拟器** —— 不需要启动游戏就能跑端到端回归。

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows（游戏本体目前只有 Windows 版） |
| Python | 3.10 或更高 |
| 游戏 | Slay the Spire 2（用于取数据；没有也能看已有的数据库） |
| 游戏内 mod | [DamageMeter](https://steamcommunity.com/sharedfiles/filedetails/?id=3747557283)（实时战斗数据）<br>DanmakuSpire（中文名称库，可选） |

## 快速开始

```bat
git clone https://github.com/Litru0111/watch-the-spire.git
cd watch-the-spire

REM 0) 环境自检 —— 会逐项告诉你数据源/游戏目录是否就绪
sts2mon.cmd doctor

REM 1) 从游戏文件提取文案与数值（只需一次，实测约 10 秒）
sts2mon.cmd extract

REM 2) 导入历史对局
sts2mon.cmd ingest

REM 3) 打开仪表盘
sts2mon.cmd serve
REM   → http://127.0.0.1:8770
```

实时监控（玩游戏时开着它）：

```bat
sts2mon.cmd watch
```

### 不用 .cmd 启动器

`sts2mon.cmd` 会自动设好 `PYTHONPATH`，所以**从任何目录**调用都可以。
如果直接走模块方式，则必须在**项目根目录**下执行：

```bat
cd watch-the-spire
python -m sts2mon doctor
```

或者安装成命令（会写进当前 Python 环境）：

```bat
pip install -e .
watch-the-spire doctor
```

## 它监控什么

| 页面 | 内容 |
|---|---|
| **全局统计** | 角色胜率、进阶分布、死亡原因；附「进度/卡牌」两个子页（逐层遭遇、抓牌倾向） |
| **对局记录** | 左：对局列表（可切全部/单人/多人）；中：地图与路线、玩家排行榜；右：逐层数据、局后总结（评分+路线+数值曲线）、单场战斗明细 |
| **实时监控** | 当前层数/房间、敌我 HP 与出牌、卡牌抓取监控、事件与选择、我方数值预测 |

## 数据从哪来

全部是**只读**采集，程序不会改写任何游戏文件。

| 数据源 | 提供什么 | 位置 |
|---|---|---|
| `current_run.save` | 权威 HP/金币/牌组、地图路线、选择记录 | `<游戏数据>/steam/<id>/modded/profile*/saves/` |
| `DamageMeter_combat_state.json` | 战斗内逐回合/逐卡数据 | `<游戏数据>/DamageMeter_combat_state.json` |
| `skada_run_token.json` | 「新一局」的第一信号 | 同上目录 |
| `rdps_meter/<seed>.json` | 更细的逐卡伤害/增益 | 同上目录 |
| `history/*.run` | 对局结束后落盘的完整存档 | `<游戏数据>/steam/<id>/modded/profile*/saves/history/` |
| `object-catalog.json` | 中文名称库（DanmakuSpire mod 导出） | `<游戏数据>/DanmakuSpire/` |
| `logs/godot.log` | 出牌日志（伤害归因） | `<游戏数据>/logs/` |

`<游戏数据>` 在 Windows 上是 `%APPDATA%\SlayTheSpire2`。

### 能力边界（不掩饰）

- 对局存档**离开该层才写入**，所以实时数据有一层延迟；战斗内数据来自 DamageMeter。
- `.run` 里**没有**「本机是谁」的字段，只能靠存档路径里的 Steam ID 推断。
- **无法检测是否用过 SL（读档）** —— 游戏不留下任何痕迹。
- 没装 DanmakuSpire 时，中文名会退化成英文 ID。

## 配置

两个环境变量，路径解析的优先级都高于自动探测：

| 变量 | 作用 | 默认 |
|---|---|---|
| `STS2MON_DATA` | 数据目录（数据库、提取结果） | `<项目根>/.sts2mon` |
| `STS2MON_GAME` | 游戏安装目录 | 自动探测（Steam 库文件 + 常见路径） |

例：

```bat
set STS2MON_DATA=D:\my-sts2-data
set STS2MON_GAME=D:\SteamLibrary\steamapps\common\Slay the Spire 2
sts2mon.cmd doctor
```

## 命令一览

| 命令 | 说明 |
|---|---|
| `doctor` | 环境自检：路径解析结果、各数据源是否就绪 |
| `extract` | 从游戏文件提取文案/数值（公开版不含这些数据） |
| `ingest` | 导入全部历史对局 |
| `serve` | 启动仪表盘（`--watch` 同时开监控） |
| `watch` | 实时监控 + 仪表盘 |
| `demo` | 把一场真实对局重放成实时数据流（不用启动游戏） |
| `stats` | 命令行打印全局统计 |
| `export` | 导出统计结果（CSV/JSON） |
| `clean` | 清理 `live-*` 实时残片 |

## 自检与回归

```bat
python tools\selftest.py                 # 端到端
python tools\simulate_runs.py --runs 25  # 黑箱模拟器（不需要游戏）
```

部分测试需要真实存档或提取数据才能跑（会在输出里标明跳过原因，不会假装通过）。
详见 [docs/测试说明.md](docs/测试说明.md)。

## 架构

```
适配器 Adapter ──► 事件总线 EventBus ──► 状态机 Tracker ──► 存储 Store(SQLite)
                                                              │
                                              分析 Analytics ──┴──► 仪表盘 Dashboard
```

实时、回放、历史三条路径**共用**同一套 Tracker 与 Store，
所以「实时看到的」和「事后复盘的」保证一致。

### 关键文件

| 文件 | 职责 |
|---|---|
| `sts2mon/paths.py` | 数据目录与游戏目录解析（环境变量 > 项目根 > 用户目录） |
| `sts2mon/adapters/` | 各数据源适配器（存档 / 战斗 / 令牌 / rdps / 历史） |
| `sts2mon/tracker.py` | 事件流 → 对局状态的状态机（含多人分流） |
| `sts2mon/model.py` | 数据模型 + SQLite 持久化（含自愈式列迁移） |
| `sts2mon/summary.py` | 局后总结与评分 |
| `sts2mon/effects.py` | 卡牌/遗物文案与数值（含升级增量） |
| `sts2mon/web.py` | HTTP 服务与 JSON 接口 |
| `sts2mon/assets/dashboard.html` | 整个前端（单文件，无构建步骤） |
| `tools/extract_*.py` | 从 `.pck` / `.dll` 提取数据 |
| `tools/simulate_runs.py` | 黑箱对局模拟器 |

更多设计说明见 [docs/](docs/)。

## 许可

[MIT](LICENSE)。游戏相关的商标与内容归其各自权利人所有。
