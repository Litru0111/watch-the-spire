// 验证「自定义对局」在界面上的显示：列表徽标、详情徽标、实时页模式。
import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath, baseUrl } from './_common.mjs';
const EDGE = browserPath();
const PORT = 9477;
const OUTDIR = '.sts2mon/shots';
mkdirSync(OUTDIR, { recursive: true });

// 前置数据守卫：这条路必须**有自定义对局**才能验收。
// 没有数据时报「缺前置数据」并以退出码 2 结束，而不是报一堆红 ——
// 后者会让人以为界面坏了（本仓库其它测试也是这个约定）。
const custom = await (async () => {
  try {
    const runs = (await (await fetch(`${baseUrl()}/api/runs`)).json()).runs || [];
    return runs.filter(r => r.valid !== false && (r.game_mode || 'standard') === 'custom');
  } catch { return []; }
})();
if (!custom.length) {
  console.error('缺少前置数据：本机没有「自定义对局」记录，无法验收自定义模式的显示。');
  console.error('在游戏里开一局自定义模式并打完/导入后再跑本脚本。');
  process.exit(2);
}

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  '--window-size=1680,1000', '--no-first-run', '--force-device-scale-factor=1',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-custom',
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

// 直接定位到那场**自定义**对局。
// 早先这里假设「最新的第一行就是自定义局」—— 一旦后来的普通对局更多，
// 第一行就不是自定义局，于是整脚本误报失败（实测踩过）。
const customId = custom[0].run_id;
P(`用自定义对局: ${customId}`);

// ① 列表：自定义局那一行的徽标
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

// 点那场自定义局的同一行看详情（按 run_id 精确点，不看顺序）
await ev(`(() => {
  const row = document.querySelector('#runList .runrow[data-run-id="${customId}"]')
           || [...document.querySelectorAll('#runList .runrow')].find(r => /自定义/.test(r.textContent));
  if (row) row.click();
})()`);
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
// 实时页要**游戏正在战斗中**才有内容（live.snapshot 的 available=false 时
// 界面就是「等待数据…」）。没有实时数据时按本仓库惯例报「缺前置数据」，
// 而不是把「等待数据」当成断言失败 —— 那是数据没到，不是界面错了。
let liveData = null;
try {
  liveData = await (await fetch(`${baseUrl()}/api/live`)).json();
} catch { /* 接口不可用时按无数据处理 */ }
if (!liveData?.available) {
  P('  ⏭ 跳过：当前没有进行中的实时对局（游戏不在战斗中 / 未装 DamageMeter）');
  P('     这一项需要在游戏中实测：装 DamageMeter，进自定义对局后重跑。');
  P(`     接口状态: available=${liveData ? liveData.available : '不可用'}`);
} else {
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
  if (r.result?.data) writeFileSync(`${OUTDIR}/custom-${theme}.png`, Buffer.from(r.result.data, 'base64'));
}
P('');
P(`截图: ${OUTDIR}/custom-dark.png · custom-light.png`);
P('');
P(pass ? '结论: 自定义对局显示正常 ✔' : '结论: 有未通过项 ✘');
child.kill();
process.exit(pass ? 0 : 1);
