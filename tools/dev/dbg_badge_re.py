"""定位称号键为什么没被提取（单测正则）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extract_effects as EE  # noqa: E402
from sts2mon import paths  # noqa: E402

samples = [
    b'"ELITE.bronzeTitle": "x"',
    b'  "ELITE.bronzeTitle": "x"',
    b'"ELITE.silverDescription": "x"',
    b'"STRIKE_IRONCLAD.title": "Strike"',
    b'"CCCCOMBO.title": "x"',
    b'"ELITE.bronzeTitle": "Eliten-Jager:in"',
]
print("单测正则:")
for s in samples:
    m = EE.KV.search(s)
    print(f"   {s.decode()[:46]:<48} -> {m.group(1).decode() if m else '不匹配'}")

blob = paths.pck_path().read_bytes()
i = blob.find(b"ELITE.bronzeTitle")
seg = blob[i - 3:i + 60]
print()
print("真实片段:", repr(seg))
m = EE.KV.search(seg)
print("在该片段上匹配:", m.group(1).decode() if m else "不匹配")

# 用真实片段做「键的捕获组」检查
hits = EE.KV.findall(blob)
print()
print(f"全文件 findall 命中 {len(hits)} 条")
badge = [(k, v) for k, v in hits if b"ronze" in k or b"ilver" in k or b"old" in k]
print(f"其中称号键 {len(badge)}")
for k, v in badge[:6]:
    print(f"   {k.decode():<32} {v.decode('utf-8', 'replace')[:40]}")
