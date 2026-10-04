// 验证「自定义对局」在界面上的显示：列表徽标、详情徽标、实时页模式。
import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath } from './_common.mjs';
const EDGE = browserPath();
const PORT = 9477;
const OUTDIR = '.sts2mon/shots';
mkdirSync(OUTDIR, { recursive: true });

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  '--window-size=1680,1000', '--no-first-run', '--force-device-scale-factor=1',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-custom',
  'http://127.0.0.1:8770/#runs',
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

const P = s => process.stdout.write(s + '\n');
let pass = true;
const chk = (c, label, detail) => { if (!c) pass = false;
  P(`  ${c ? '✔' : '✘'} ${label}${detail ? '  —— ' + detail : ''}`); };

// ① 列表：点击第一行（当前自定义局就是最新的），看徽标
P('① 对局列表的「自定义」徽标');
const listInfo = await ev(`(() => {
  const rows = [...document.querySelectorAll('#runList .runrow')];
  const withBadge = rows.filter(r => /自定义/.test(r.textContent));
  return { rows: rows.length,
           badgeRows: withBadge.length,
           firstText: rows[0] ? rows[0].textContent.replace(/\\s+/g, ' ').trim().slice(0, 90) : '',
           badgeSample: withBadge.length ? withBadge[0].textContent.match(/自定义[^\\n]*/)?.[0] : '' };
})()`);
chk(listInfo.badgeRows >= 1, '列表里能看到「自定义」徽标', listInfo.badgeSample || '');
P(`     首行: ${listInfo.firstText}`);

// 点第一行看详情
await ev(`document.querySelector('#runList .runrow').click()`);
await sleep(1600);
const detail = await ev(`(() => {
  const h = document.querySelector('#runHead h3');
  const t = h ? h.textContent.replace(/\\s+/g, ' ').trim() : '';
  return { text: t, hasBadge: /自定义/.test(t), hasDraft: /选牌/.test(t) };
})()`);
chk(detail.hasBadge, '详情页头部显示「自定义」', detail.text.slice(0, 80));
chk(detail.hasDraft, '并列出修正项中文名「选牌」');

// ② 实时页模式
P('');
P('② 实时监控页的模式显示');
await ev(`document.querySelector('.tab[data-tab="live"]').click()`);
await sleep(2600);
const live = await ev(`(() => {
  const m = document.getElementById('liveMeta');
  return { text: m ? m.textContent.replace(/\\s+/g, ' ').trim() : '',
           hasCustom: m ? /自定义/.test(m.textContent) : false,
           hasDraft: m ? /选牌/.test(m.textContent) : false };
})()`);
chk(live.hasCustom, '实时页显示「自定义」', live.text.slice(0, 90));
chk(live.hasDraft, '实时页列出「选牌」');

// 截图
await ev(`document.querySelector('.tab[data-tab="runs"]').click()`);
await sleep(1200);
for (const theme of ['dark', 'light']) {
  await ev(`document.querySelector('.thbtn[data-theme="${theme}"]').click()`);
  await sleep(700);
  await ev(`new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))`);
  await sleep(300);
  const r = await send('Page.captureScreenshot', { format: 'png' });
  if (r.result?.data) writeFileSync(`${OUTDIR}/custom-${theme}.png`, Buffer.from(r.result.data, 'base64'));
}
P('');
P(`截图: ${OUTDIR}/custom-dark.png · custom-light.png`);
P('');
P(pass ? '结论: 自定义对局显示正常 ✔' : '结论: 有未通过项 ✘');
child.kill();
process.exit(pass ? 0 : 1);
