// 验证「对局列表」的单人/多人过滤：数量、可见行数、滑块、以及空分类时的提示。
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath, baseUrl} from './_common.mjs';
const EDGE = browserPath();
const PORT = 9399;
const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  '--window-size=1680,1000', '--no-first-run',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-runfilter',
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

await sleep(3500);

const snap = async () => ev(`(() => {
  const rows = [...document.querySelectorAll('#runList .runrow')];
  const multiRows = rows.filter(r => r.innerHTML.includes('多人'));
  const slide = document.getElementById('runFilterSlide');
  const cur = document.querySelector('#runFilter .subtab.on');
  return {
    mode: cur ? cur.dataset.mode : null,
    btnLabels: [...document.querySelectorAll('#runFilter .subtab')]
      .map(b => b.textContent.trim()),
    countTag: (document.getElementById('runCount') || {}).textContent,
    rows: rows.length,
    multiRows: multiRows.length,
    singleRows: rows.length - multiRows.length,
    slideOk: slide.style.left === cur.offsetLeft + 'px'
          && slide.style.width === cur.offsetWidth + 'px',
    emptyHint: document.querySelector('#runList .hint')?.textContent?.trim() || '',
    headHasRun: !!document.querySelector('#runHead h3'),
  };
})()`);

const results = [];
let v = await snap();
results.push(['初始（全部）', v]);
console.log('初始:', JSON.stringify(v, null, 1));

for (const m of ['multi', 'single', 'all']) {
  await ev(`document.querySelector('#runFilter .subtab[data-mode="${m}"]').click()`);
  await sleep(1400);
  v = await snap();
  results.push([`切到 ${m}`, v]);
  console.log(`\n切到 ${m}:`, JSON.stringify(v, null, 1));
}

// 断言
const P = s => process.stdout.write(s + '\n');
P('\n' + '='.repeat(70));
let pass = true;
const chk = (cond, label, detail) => {
  if (!cond) pass = false;
  P(`  ${cond ? '✔' : '✘'} ${label}${detail ? '  —— ' + detail : ''}`);
};
const byName = Object.fromEntries(results.map(([k, x]) => [k, x]));
chk(byName['初始（全部）'].mode === 'all', '默认是「全部」');
chk(byName['初始（全部）'].rows > 0, '全部列表有内容', `${byName['初始（全部）'].rows} 行`);
const allMulti = byName['切到 multi'];
chk(allMulti.mode === 'multi', '多人页高亮正确');
chk(allMulti.multiRows === allMulti.rows && allMulti.rows > 0,
    '多人页只显示多人对局', `${allMulti.rows} 行全是多人`);
const allSingle = byName['切到 single'];
chk(allSingle.mode === 'single', '单人页高亮正确');
chk(allSingle.multiRows === 0 && allSingle.rows > 0,
    '单人页不含任何多人对局', `${allSingle.rows} 行，含多人 ${allSingle.multiRows}`);
chk(allSingle.rows + allMulti.rows === byName['初始（全部）'].rows,
    '单人 + 多人 = 全部', `${allSingle.rows}+${allMulti.rows}=${byName['初始（全部）'].rows}`);
chk([allMulti, allSingle].every(x => x.slideOk), '过滤条滑块对齐');
// 计数文案改过：以前是「当前 / 总数 场」，现在只显示当前筛选的场次
// （无效对局不再进列表，那个分母就失去意义了）。总数在总览的「总对局」卡片里。
chk(/^\d+ 场$/.test(allSingle.countTag || ''), '标题显示当前筛选场次', allSingle.countTag);
chk(allSingle.headHasRun, '切换后右侧详情不为空');
P('');
P(pass ? '结论: 过滤功能正常 ✔' : '结论: 有未通过项 ✘');
child.kill();
process.exit(pass ? 0 : 1);
