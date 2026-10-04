# tools/ui — 界面验收

用无头 Chromium（Edge / Chrome）通过 DevTools 协议打开仪表盘，
对渲染结果做**计算样式断言**。

## 为什么不用截图比对

截图容易误判：抓帧早了、缩放不同、字体差异都会让「看起来不对」而实际没错，
反过来也会漏掉真正的问题（元素被兄弟节点盖住时截图看不出来）。

所以这里的判据是 DOM 的**计算样式与几何**：

```js
getComputedStyle(td).backgroundColor   // 底色在 td 上，不在 tr 上
el.getBoundingClientRect()             // 溢出、铺满、位置
document.documentElement.scrollHeight  // 整页滚动
```

截图只作为辅助产物写到 `.sts2mon/shots/`。

## 前提

1. 仪表盘已在跑：`sts2mon.cmd serve`
2. 有 Chromium 系浏览器（自动探测；也可 `set CHROME_PATH=...` 指定）

## 脚本

| 脚本 | 覆盖 |
|---|---|
| `ui_check.mjs` | 整页/子页切换、主题、滑块、canvas |
| `ui_check_contrast.mjs` | 主题对比度：「本机行高亮」不能被刷成「涂黑」 |
| `ui_check_list.mjs` | 对局列表：不出现 `?` 行、称号正常显示 |
| `ui_check_overflow.mjs` | 横向溢出（表格列宽分配） |
| `ui_check_board.mjs` | 排行榜排序 |
| `ui_check_runs.mjs` | 单人 / 多人过滤 |
| `ui_check_fill.mjs` | 铺满整宽、无左右留白 |
| `ui_check_custom.mjs` | 自定义对局的标记显示 |
| `ui_diag.mjs` / `ui_diag_sub.mjs` | 诊断：列出可见元素及其计算样式 |
| `ui_shots.mjs` / `ui_shots_sub.mjs` | 只出截图 |
| `measure_events.mjs` | 量事件流的渲染规模 |
| `verify_chart.mjs` | 柱状图数值与悬停提示 |

## 共用模块

`_common.mjs` 提供浏览器探测与 DevTools 连接：

```js
import { connect, reporter, browserPath } from './_common.mjs';
const { ev, shot, close } = await connect({ hash: '#runs' });
```

新增脚本请用它，不要再自己写一遍 `spawn(EDGE, [...])` 与 WebSocket 握手。

## 用法

```bat
node tools\ui\ui_check_list.mjs
node tools\ui\ui_check_contrast.mjs <种子>       :: 指定对局种子
set CHROME_PATH=D:\chrome.exe && node tools\ui\ui_check.mjs
```

`dbg_*` 两个是调试用的（查底色、查深色元素），平时不用。
