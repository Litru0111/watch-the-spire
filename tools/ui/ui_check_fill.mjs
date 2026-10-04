// 验证：铺满整宽（左右不留白）、逐层表中文名、排行榜无横向滚动、整页不溢出。
import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath } from './_common.mjs';
const EDGE = browserPath();
const PORT = 9433;
const OUTDIR = '.sts2mon/shots';
mkdirSync(OUTDIR, { recursive: true });
const SEED = process.argv[2] || 'K3C083K7D8';
const WIN = process.argv[3] || '1680,1000';

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  `--window-size=${WIN}`, '--no-first-run', '--force-device-scale-factor=1',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-fill',
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

const P = s => process.stdout.write(s + '\n');
let pass = true;
const chk = (c, label, detail) => { if (!c) pass = false;
  P(`  ${c ? '✔' : '✘'} ${label}${detail ? '  —— ' + detail : ''}`); };

// ① 铺满
const geom = await ev(`(() => {
  const main = document.querySelector('main');
  const r = main.getBoundingClientRect();
  const cs = getComputedStyle(main);
  const head = document.querySelector('header').getBoundingClientRect();
  const layout = document.querySelector('.layout').getBoundingClientRect();
  return {
    viewW: innerWidth,
    mainW: Math.round(r.width), mainLeft: Math.round(r.left), mainRight: Math.round(r.right),
    padL: cs.paddingLeft, padR: cs.paddingRight, maxW: cs.maxWidth,
    headW: Math.round(head.width), headLeft: Math.round(head.left),
    layoutW: Math.round(layout.width), layoutLeft: Math.round(layout.left),
    pageOx: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
})()`);
P('① 是否铺满整宽');
chk(geom.maxW === 'none', 'main 没有 max-width', geom.maxW);
P(`     main 宽=${geom.mainW} 左=${geom.mainLeft} 右=${geom.mainRight} 视口=${geom.viewW}`);
chk(geom.mainRight >= geom.viewW - 14,
    '右侧只留 padding，没有多余留白', `右边距 ${geom.viewW - geom.mainRight}px`);
chk(geom.mainLeft <= 13, '左侧只留 padding', `左边距 ${geom.mainLeft}px`);
chk(geom.headLeft === 0 && geom.headW === geom.viewW,
    '顶栏也铺满', `${geom.headW}/${geom.viewW}`);
chk(geom.pageOx <= 1, '没有横向滚动条', `${geom.pageOx}px`);

// ② 逐层表中文名
P('');
P('② 逐层数据的「获得」列是否中文');
const gained = await ev(`(() => {
  const cells = [...document.querySelectorAll('#floorTable td.gainedcell')];
  const texts = cells.map(c => c.textContent.trim()).filter(t => t && t !== '—');
  const ascii = texts.filter(t => /[A-Za-z]{3,}/.test(t));
  return { n: cells.length, sample: texts.slice(0, 6), english: ascii.slice(0, 5),
           rowSample: (document.querySelector('#floorTable tbody tr') || {}).textContent || '' };
})()`);
chk(gained.english.length === 0, '没有残留的英文 ID',
    gained.english.length ? gained.english.join(' | ') : '干净');
P(`     样例: ${gained.sample.join(' · ')}`);

// ③ 排行榜横向滚动
P('');
P('③ 排行榜是否还有横向滚动');
const board = await ev(`(() => {
  const b = document.getElementById('boardBody');
  const tbl = b ? b.querySelector('table') : null;
  const panel = document.getElementById('boardPanel');
  const pr = panel ? panel.getBoundingClientRect() : null;
  return {
    boardOx: b ? b.scrollWidth - b.clientWidth : null,
    tableW: tbl ? Math.round(tbl.getBoundingClientRect().width) : null,
    panelW: pr ? Math.round(pr.width) : null,
    rows: document.querySelectorAll('#boardBody tbody tr').length,
  };
})()`);
chk(board.boardOx !== null && board.boardOx <= 2, '排行榜无横向滚动',
    `${board.boardOx}px`);
P(`     表格宽=${board.tableW} 面板宽=${board.panelW} 行数=${board.rows}`);

// ④ 各页签整体
P('');
P('④ 各页签整页溢出');
for (const t of ['stats','runs','live']) {
  await ev(`document.querySelector('.tab[data-tab="${t}"]').click()`);
  await sleep(900);
  const e = await ev(`document.documentElement.scrollHeight - document.documentElement.clientHeight`);
  const ox = await ev(`document.documentElement.scrollWidth - document.documentElement.clientWidth`);
  chk(e <= 2 && ox <= 1, `${t.padEnd(6)} 不溢出`, `纵向 ${e}px 横向 ${ox}px`);
}

// 截图
await ev(`document.querySelector('.tab[data-tab="runs"]').click()`);
await sleep(1200);
for (const theme of ['dark', 'light']) {
  await ev(`document.querySelector('.thbtn[data-theme="${theme}"]').click()`);
  await sleep(700);
  await ev(`new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))`);
  await sleep(300);
  const r = await send('Page.captureScreenshot', { format: 'png' });
  if (r.result?.data) writeFileSync(`${OUTDIR}/fill-${theme}.png`, Buffer.from(r.result.data, 'base64'));
}
P('');
P(`截图: ${OUTDIR}/fill-dark.png · fill-light.png`);
P('');
P(pass ? '结论: 全部通过 ✔' : '结论: 有未通过项 ✘');
child.kill();
process.exit(pass ? 0 : 1);
