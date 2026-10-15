// 给统计页的三个子页 × 两种主题截图，用于肉眼验收。
import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath, baseUrl} from './_common.mjs';
const EDGE = browserPath();
const PORT = 9377;
const OUTDIR = process.argv[2] || '.sts2mon/shots';
const WIN = process.argv[3] || '1680,1000';
mkdirSync(OUTDIR, { recursive: true });

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  `--window-size=${WIN}`, '--no-first-run', '--force-device-scale-factor=1',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-shot2',
  `${baseUrl()}/#stats`,
], { stdio: 'ignore' });

async function target() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const p = list.find(t => t.type === 'page' && t.url.startsWith(baseUrl()));
      if (p?.webSocketDebuggerUrl) return p;
    } catch {}
    await sleep(500);
  }
  throw new Error('连不上 DevTools');
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
const ev = (expr) => send('Runtime.evaluate', { expression: expr, awaitPromise: true });

await sleep(2600);
const shots = [];
for (const theme of ['dark', 'light']) {
  await ev(`document.querySelector('.thbtn[data-theme="${theme}"]').click()`);
  await sleep(400);
  for (const sub of ['overview', 'progress', 'deck']) {
    await ev(`document.querySelector('.subtab[data-sub="${sub}"]').click()`);
    await sleep(700);
    // 等两帧确保合成器拿到最新画面
    await ev(`new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))`);
    await sleep(400);
    const r = await send('Page.captureScreenshot', { format: 'png' });
    const d = r.result?.data;
    if (!d) { console.log(`失败 ${theme}/${sub}`); continue; }
    const f = `${OUTDIR}/stats-${theme}-${sub}.png`;
    writeFileSync(f, Buffer.from(d, 'base64'));
    shots.push(f);
    console.log('  ' + f);
  }
}
child.kill();
console.log(`共 ${shots.length} 张`);
