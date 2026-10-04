// 给三个标签页 × 两种主题截图，用来肉眼验收新 UI。
import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath } from './_common.mjs';
const EDGE = browserPath();
const PORT = 9355;
const OUTDIR = process.argv[2] || '.sts2mon/shots';
const WIN = process.argv[3] || '1680,1000';
mkdirSync(OUTDIR, { recursive: true });

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  `--window-size=${WIN}`, '--no-first-run', '--force-device-scale-factor=1',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-shot',
  'http://127.0.0.1:8770/#stats',
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
  throw new Error('连不上 DevTools');
}

const page = await target();
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0;
const pending = new Map();
ws.addEventListener('message', e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
await new Promise(r => ws.addEventListener('open', r));
const send = (method, params = {}) => new Promise(res => {
  const myId = ++id; pending.set(myId, res);
  ws.send(JSON.stringify({ id: myId, method, params }));
});

await sleep(2500);

const shots = [];
for (const theme of ['dark', 'light']) {
  await send('Runtime.evaluate', {
    expression: `document.querySelector('.thbtn[data-theme="${theme}"]').click()`,
  });
  await sleep(500);
  for (const tab of ['stats', 'runs', 'live']) {
    await send('Runtime.evaluate', {
      expression: `document.querySelector('.tab[data-tab="${tab}"]').click()`,
    });
    await sleep(900);
    const r = await send('Page.captureScreenshot', { format: 'png' });
    const data = r.result?.data;
    if (!data) { console.log(`截图失败 ${theme}/${tab}`); continue; }
    const f = `${OUTDIR}/${theme}-${tab}.png`;
    writeFileSync(f, Buffer.from(data, 'base64'));
    shots.push(f);
    console.log(`  ${f}`);
  }
}

child.kill();
console.log(`共 ${shots.length} 张`);
