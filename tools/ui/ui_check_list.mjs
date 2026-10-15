// 干净的对局列表截图（不依赖会卡住的多文件脚本）。
import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath, baseUrl} from './_common.mjs';
const EDGE = browserPath();
const PORT = 9488;
const OUTDIR = '.sts2mon/shots';
mkdirSync(OUTDIR, { recursive: true });

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  '--window-size=1680,1000', '--no-first-run', '--force-device-scale-factor=1',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-list',
  `${baseUrl()}/#runs`,
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

const P = s => process.stdout.write(s + '\n');
let pass = true;
const chk = (c, label, detail) => { if (!c) pass = false;
  P(`  ${c ? '✔' : '✘'} ${label}${detail ? '  —— ' + detail : ''}`); };

const info = await ev(`(() => {
  const rows = [...document.querySelectorAll('#runList .runrow')];
  const q = rows.filter(r => /(^|\\s)\\?(\\s|$)/.test(r.querySelector('.who')?.textContent || '')
                            || /^\\?/.test((r.querySelector('.who')?.textContent || '').trim()));
  const withBadge = rows.filter(r => r.querySelector('.runbadges .pill'));
  return { rows: rows.length, q: q.length, withBadge: withBadge.length,
           first: rows[0]?.textContent.replace(/\\s+/g,' ').trim().slice(0, 70),
           badgeSample: withBadge[0]?.querySelector('.runbadges')?.textContent.replace(/\\s+/g,' ').trim() };
})()`);
P(`对局列表: ${info.rows} 行`);
chk(info.q === 0, '没有显示成「?」的行', `${info.q} 行`);
chk(info.withBadge >= 5, '列表里能看到称号', `${info.withBadge} 行 · 例: ${info.badgeSample}`);
P(`  首行: ${info.first}`);

for (const theme of ['dark', 'light']) {
  await ev(`document.querySelector('.thbtn[data-theme="${theme}"]').click()`);
  await sleep(600);
  await ev(`new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))`);
  await sleep(250);
  const r = await send('Page.captureScreenshot', { format: 'png' });
  if (r.result?.data) writeFileSync(`${OUTDIR}/list-${theme}.png`, Buffer.from(r.result.data, 'base64'));
}
P(`截图: ${OUTDIR}/list-dark.png · list-light.png`);
P(pass ? '结论: 对局列表正常 ✔' : '结论: 有问题 ✘');
child.kill();
process.exit(pass ? 0 : 1);
