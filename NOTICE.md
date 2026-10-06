# 第三方声明 · Third-Party Notices

本文件与 [`LICENSE`](LICENSE) 分开存放：`LICENSE` 保持纯 MIT 正文，
这样 GitHub 能正确识别许可证（实测把声明写在 MIT 后面会让 GitHub
判成 `NOASSERTION`，既不显示 MIT 徽章、SPDX 也检测不出）。

## 关于《杀戮尖塔 2》(Slay the Spire 2)

本项目是第三方非官方的数据监控工具，**与 Mega Crit 无任何关联**，
也未获得其赞助或认可。

* 本项目**不包含**游戏本体、游戏资源文件，也不重新分发游戏的
  本地化文案与数值数据。
* `sts2mon extract` 会在**用户自己的机器上**读取用户已合法拥有的
  游戏安装文件，生成仅供本机使用的展示用数据；这些产物被
  `.gitignore` 排除，不会被提交或分发。
* 程序对游戏文件是**只读**访问，不会改写任何游戏文件。
* 「Slay the Spire」及相关名称为其各自权利人的商标，
  本项目仅作指代性使用。

## 第三方 mod

实时战斗数据依赖社区 mod **DamageMeter**（Steam 创意工坊物品
`3747557283`），中文名称库依赖 **DanmakuSpire**（可选）。
这两个 mod 由各自的作者维护，遵循其各自的许可；本项目只是读取它们
写出的状态文件，不包含也不分发 mod 本体。

## AI 生成声明

本项目绝大部分代码由 AI 编写，详见 [README](README.md) 开头的说明。

---

## English

This project is an unofficial, third-party data monitor for Slay the Spire 2.
It is **not affiliated with, sponsored by, or endorsed by Mega Crit**.

It ships no game assets and no extracted game text or numeric data.
`sts2mon extract` generates those **locally** from a copy of the game the user
already owns; the generated files are excluded by `.gitignore` and never
distributed. The program accesses game files **read-only**.

Real-time combat data relies on the community mod **DamageMeter**
(Steam Workshop item `3747557283`); the Chinese name catalog relies on
**DanmakuSpire** (optional). Those mods are maintained by their own authors
under their own licenses; this project only reads the state files they write
and redistributes neither of them.

Much of this project's code was written by AI — see the notice at the top of
the [README](README.md).
