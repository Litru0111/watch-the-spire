# tools

开发与验证脚本。**全部只用 Python 标准库**，直接运行即可。

## 目录

| 目录 | 内容 |
|---|---|
| `tools/`（本层） | 数据提取、回归测试、对局模拟器 |
| `tools/ui/` | 界面验收（无头浏览器 + 计算样式断言） |
| `tools/dev/` | 逆向工程期的一次性调查脚本（`probe_*` / `dbg_*`） |

## 数据提取

```bat
python tools\extract_effects.py       :: .pck 本地化文案 → card_effects.json
python tools\extract_values.py        :: sts2.dll 数值/升级增量 → card_values.json
python tools\build_power_names.py     :: 从文案挖状态名对照 → power_names.json
```

通常**不需要单独跑** —— 用 `sts2mon.cmd extract` 会按顺序调用这三个，
并把结果写进 `STS2MON_DATA` 指定的目录。

路径解析统一走 `sts2mon.paths`，不再写死盘符：

```bat
set STS2MON_GAME=D:\SteamLibrary\steamapps\common\Slay the Spire 2
python tools\extract_values.py
```

## 回归测试

见 [../docs/测试说明.md](../docs/测试说明.md)。

最常用的两个：

```bat
python tools\simulate_runs.py --runs 25    :: 黑箱模拟器，不需要游戏
python tools\selftest.py                   :: 端到端
```

模拟器值得优先跑：它假造游戏数据走完整链路
（采集 → diff → 入库 → 分析），几秒内覆盖大量边界，
且完全不依赖游戏安装。

## 支持库（被上面的脚本复用）

| 文件 | 职责 |
|---|---|
| `dotnet_meta.py` | 纯 Python 解析 .NET 元数据（PE → CLI 头 → BSJB → 表流） |
| `il_disasm.py` | CIL 反汇编（从 `OnUpgrade` 之类的 IL 里取值） |
| `check_sql.py` | 静态核对 `model.py` 里 INSERT 的列数/占位符/实参是否一致 |
