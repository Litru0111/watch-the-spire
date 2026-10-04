import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from sts2mon import paths as _paths  # noqa: E402
"""定位 pck 里卡牌效果文本的结构。"""
import re
from collections import Counter
from pathlib import Path

PCK = _paths.pck_path()
blob = PCK.read_bytes()

MOJIBAKE = "\ufffd"

def ctx(probe: bytes, span: int = 220, limit: int = 2) -> None:
    idxs = [m.start() for m in re.finditer(re.escape(probe), blob)]
    print(f"  {probe.decode('utf-8', 'ignore')!r}: 出现 {len(idxs)} 次")
    for i in idxs[:limit]:
        chunk = blob[max(0, i - span // 2): i + span]
        text = "".join(chr(b) if 32 <= b < 127 or b > 127 else "·" for b in chunk)
        try:
            text = chunk.decode("utf-8", "replace")
            text = "".join(c if c.isprintable() or c == "\n" else "·" for c in text)
        except Exception:
            pass
        print(f"      …{text}…")
    print()

print("① 按卡牌 ID 搜描述键")
for probe in ("STRIKE_IRONCLAD.description", "STRIKE_IRONCLAD", "BASH.description",
              "AKABEKO.description", "AKABEKO"):
    ctx(probe.encode(), limit=1)

print("② pck 里类似「键值对」的中文文本块，找几个含「造成」的")
hits = [m.start() for m in re.finditer("造成".encode(), blob)]
print(f"  '造成' 共 {len(hits)} 处，抽样 3 处上下文：")
for i in hits[:3]:
    chunk = blob[max(0, i - 160): i + 200]
    text = chunk.decode("utf-8", "replace")
    text = "".join(c if c.isprintable() else "·" for c in text)
    print(f"      …{text}…")
print()

print("③ 看中文文本附近的「键名」形态（是否形如 Xxx.title / Xxx.description）")
keypat = re.compile(rb"[A-Z][A-Z0-9_]{3,40}\.(title|description|loss|victory)")
near = []
for i in hits[:40]:
    chunk = blob[max(0, i - 400): i + 100]
    near += [m.group().decode() for m in keypat.finditer(chunk)]
print(f"  在 '造成' 附近出现的键: {Counter(near).most_common(12)}")

print()
print("④ 全库统计：Xxx.description 形式的键有多少种")
allk = Counter(m.group(1).decode() for m in
               re.finditer(rb"([A-Z][A-Z0-9_]{3,40})\.description", blob))
print(f"  .description 键 {len(allk)} 种（前 12）: {allk.most_common(12)}")
allt = Counter(m.group(1).decode() for m in
               re.finditer(rb"([A-Z][A-Z0-9_]{3,40})\.title", blob))
print(f"  .title 键 {len(allt)} 种（前 12）: {allt.most_common(12)}")
