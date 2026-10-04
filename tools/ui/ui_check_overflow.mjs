// 检查对局详情各面板有没有意外的横向/纵向滚动条（内容该自适应而不是溢出）。
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath } from './_common.mjs';
const EDGE = browserPath();
const PORT = 9422;
const SEED = process.argv[2] || 'K3C083K7D8';
const WIN = process.argv[3] || '1680,1000';

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  `--window-size=${WIN}`, '--no-first-run',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-overflow',
  `http://127.0.0.1:8770/#runs?seed=${SEED}`,
], { stdio: 'ignore' });

async function target() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const p = list.find(t => t.type === 'page' && t.url.includes('8770'));
      if (p?.webSocketDebuggerUrl) return p;
    } catch {}
    await sleep(500);
  }
  throw new Error('连不上');
}
const page = await target();
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0; const pending = new Map();
ws.addEventListener('message', e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
await new Promise(r => ws.addEventListener('open', r));
const send = (method, params = {}) => new Promise(res => {
  const myId = ++id; pending.set(myId, res);
  ws.send(JSON.stringify({ id: myId, method, params }));
});
const ev = async (e) => (await send('Runtime.evaluate',
  { expression: e, awaitPromise: true, returnByValue: true })).result?.result?.value;

await sleep(4500);

const info = await ev(`(() => {
  const out = [];
  const scan = (sel, label) => {
    document.querySelectorAll(sel).forEach(e => {
      if (e.offsetParent === null) return;
      const ox = e.scrollWidth - e.clientWidth;
      const oy = e.scrollHeight - e.clientHeight;
      out.push({ label: label + (e.id ? '#' + e.id : ''),
                 tag: e.tagName.toLowerCase(),
                 cw: e.clientWidth, sw: e.scrollWidth,
                 ch: e.clientHeight, sh: e.scrollHeight,
                 ox, oy });
    });
  };
  scan('.rundetail-body .panel', 'panel');
  scan('.rundetail-body .scroll', 'scroll');
  scan('.rundetail-body table', 'table');
  scan('#boardBody', 'board');
  scan('.layout', 'layout');
  scan('.runlist', 'runlist');
  return { boxes: out,
           pageOx: document.documentElement.scrollWidth - document.documentElement.clientWidth,
           pageOy: document.documentElement.scrollHeight - document.documentElement.clientHeight };
})()`);

const P = s => process.stdout.write(s + '\n');
P(`视口 ${WIN}  整页横向溢出=${info.pageOx}px  纵向溢出=${info.pageOy}px`);
P('');
P('横向溢出的容器（scrollWidth > clientWidth）：');
let badX = 0;
for (const b of info.boxes) {
  if (b.ox > 2) { badX++; P(`  ✘ ${b.label.padEnd(26)} ${b.cw} -> ${b.sw}  (+${b.ox})`); }
}
if (!badX) P('  ✔ 没有横向溢出');
P('');
P('纵向可滚动容器（正常，面板内部滚动）：');
const scrollable = info.boxes.filter(b => b.oy > 2 && (b.label.includes('scroll') || b.label.includes('board')));
if (scrollable.length) for (const b of scrollable)
  P(`  · ${b.label.padEnd(26)} ${b.ch} -> ${b.sh}  (+${b.oy})`);
else P('  （没有内容超出的滚动容器）');

const pass = info.pageOx <= 2 && !info.boxes.some(b => b.ox > 2 && !b.label.includes('scroll'));
P('');
P(pass ? '结论: 无意外横向溢出 ✔' : '结论: 存在横向溢出 ✘');
child.kill();
process.exit(pass ? 0 : 1);
