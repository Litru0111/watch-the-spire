import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from sts2mon import paths as _paths  # noqa: E402
"""定位中文效果文本（当前 pck 里的 description 是日文）。"""
import re
from pathlib import Path

PCK = _paths.pck_path()
blob = PCK.read_bytes()

KANA = re.compile(r"[\u3040-\u30ff]")
CJK = re.compile(r"[\u4e00-\u9fff]")

print("① 搜 STRIKE_IRONCLAD 的所有出现（看有没有中文版描述）")
idxs = [m.start() for m in re.finditer(b"STRIKE_IRONCLAD", blob)]
print(f"   出现 {len(idxs)} 次")
for i in idxs[:6]:
    chunk = blob[max(0, i - 40): i + 260]
    text = chunk.decode("utf-8", "replace")
    text = "".join(c if c.isprintable() else "·" for c in text)
    print(f"      …{text[:290]}…")

print()
print("② 统计 pck 里「中文且不含假名」的长文本量")
# 抽出所有像「一整句中文」的片段
sent = []
# 注意**不能**写成 rb'...\u4e00...'：字节 raw 串不会解码 \uXXXX，
# 而 re 对**字节模式**里的 \u 转义在 3.12+ 直接报错（实测踩过，
# 报 `re.error: bad escape \u`）。先让 Python 解成真实字符，再 encode。
_CN = "[\u4e00-\u9fff]"
_ASCII = "[\x20-\x7e]"
_PAT = (f"(?:{_CN}|{_ASCII}){{0,4}}"
        f"(?:{_CN}{{2,}}(?:[^\x00-\x7f]{{0,3}}{_CN}{{1,}}){{1,}})").encode()
for m in re.finditer(_PAT, blob):
    s = m.group().decode("utf-8", "ignore")
    if len(s) >= 8 and not KANA.search(s):
        sent.append(s)
print(f"   候选中文长句 {len(sent)} 条（去重 {len(set(sent))}）")
for s in list(dict.fromkeys(sent))[:12]:
    print(f"      {s[:100]}")

print()
print("③ 搜中文版常见效果措辞（游戏里显示的样子）")
for probe in ("点伤害", "点格挡", "层易伤", "层中毒", "抽1张牌", "消耗", "升级"):
    n = blob.count(probe.encode())
    print(f"   {probe!r}: {n} 次")

print()
print("④ 游戏目录/模组里的语言配置（可能指定了用哪套翻译）")
for base in (_paths.game_dir(),
             Path.home() / "AppData/Roaming/SlayTheSpire2"):
    for p in list(base.glob("*.json")) + list(base.glob("*.cfg")) + list(base.glob("*.xml")):
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")[:400]
        except Exception:
            continue
        if re.search(r"language|locale|lang", t, re.I):
            print(f"   {p}: {t[:200]}")
