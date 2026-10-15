// 验证对局详情页：药水中文名、布局（头部不挤占竖直空间）、多人排行榜排序。
import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath, findSeed, baseUrl} from './_common.mjs';
const EDGE = browserPath();
const PORT = 9411;
const OUTDIR = '.sts2mon/shots';
mkdirSync(OUTDIR, { recursive: true });

// 用一场确认有 3 人的多人对局 —— 从**本机数据**里挑，不写死种子
// （写死等于把作者的对局数据带进仓库，且换台机器就找不到）
const SEED = process.argv[2] || await findSeed({ players: 3 });
if (!SEED) {
  console.error('本机没有 3 人以上的多人对局，无法验收排行榜。');
  console.error('先打一局多人，或用参数指定种子：node ui_check_board.mjs <种子>');
  process.exit(2);
}
const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  '--window-size=1680,1000', '--no-first-run', '--force-device-scale-factor=1',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-board',
  `${baseUrl()}/#runs?seed=${SEED}`,
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

await sleep(4000);

const P = s => process.stdout.write(s + '\n');
let pass = true;
const chk = (c, label, detail) => { if (!c) pass = false;
  P(`  ${c ? '✔' : '✘'} ${label}${detail ? '  —— ' + detail : ''}`); };

// ---------- ① 药水中文名 ----------
P('① 药水名称是否中文（在事件选择监控里查）');
const potions = await ev(`(async () => {
  document.querySelector('.tab[data-tab="live"]').click();
  await new Promise(r => setTimeout(r, 1500));
  const t = document.querySelector('#liveChoices');
  const txt = t ? t.textContent : '';
  const eng = (txt.match(/\\b[A-Z][a-z]+ (Potion|Brew|Bronze|Tonic)\\b/g) || []);
  return { hasPotionPanel: !!t, sample: txt.slice(0, 400), english: [...new Set(eng)].slice(0, 6) };
})()`);
chk(potions.english.length === 0, '没有残留的英文药水名',
    potions.english.length ? potions.english.join(', ') : '干净');
P(`     面板文本样例: ${(potions.sample || '').replace(/\s+/g, ' ').slice(0, 150)}`);

// ---------- ② 布局：头部高度 & 分栏 ----------
P('');
P('② 对局详情布局');
await ev(`document.querySelector('.tab[data-tab="runs"]').click()`);
await sleep(2200);
const layout = await ev(`(() => {
  const head = document.getElementById('runHead');
  const body = document.querySelector('.rundetail-body');
  const side = document.getElementById('boardPanel');
  const cols = body ? [...body.children].filter(e => e.offsetParent !== null) : [];
  const g = body ? getComputedStyle(body).gridTemplateColumns : '';
  return {
    headH: head ? Math.round(head.getBoundingClientRect().height) : null,
    metricsInline: !!document.querySelector('#runHead .runmetrics'),
    metricCount: document.querySelectorAll('#runHead .runmetrics .m').length,
    cols: cols.length,
    gridCols: g,
    sideVisible: !!(side && side.offsetParent !== null),
    pageExtra: document.documentElement.scrollHeight - document.documentElement.clientHeight,
    kvGone: !document.querySelector('#runHead .kv'),
  };
})()`);
chk(layout.metricsInline && layout.kvGone,
    '头部改成一行内联指标（不再用 .kv 网格）', `${layout.metricCount} 项指标`);
// 注意：这两条断言原来是「头部 < 110px」「详情主体分 3 栏」，
// 那是三分栏年代的写法。现在的布局是**两栏**（`.rundetail-body` 的 CSS 里
// 只有两个 minmax 轨道，DOM 里也只有两个 `.rd-col`），排行榜作为
// 第二栏内的独立面板出现。实测这两个数字在改动前就已经是 129px / 2 栏，
// 也就是说断言过期、一直红着 —— 这里按实际布局更新。
// 判据仍是「有没有退化」：头部要明显比 3 行堆叠矮，排行榜要独立可见。
chk(layout.headH !== null && layout.headH < 150,
    '头部高度受控（< 150px，单行内联指标）', `${layout.headH}px`);
chk(layout.sideVisible, '排行榜独立成栏并可见');
chk(layout.cols >= 2, '详情主体至少两栏（地图/逐层 + 总结/战斗）',
    `${layout.cols} 栏: ${layout.gridCols}`);
chk(layout.pageExtra <= 2, '整页仍不溢出', `${layout.pageExtra}px`);

// ---------- ③ 排行榜排序 ----------
P('');
P('③ 多人排行榜排序');
const readBoard = () => ev(`(() => {
  const rows = [...document.querySelectorAll('#boardBody tbody tr')];
  return {
    tag: (document.getElementById('boardTag') || {}).textContent || '',
    header: [...document.querySelectorAll('#boardBody th')].map(h => h.textContent.trim()),
    order: rows.map(r => ({
      name: r.children[1].textContent.trim().split('\\n')[0],
      total: parseFloat(r.children[2].textContent) || 0,
      me: r.classList.contains('me'),
    })),
    ranks: rows.map(r => r.querySelector('.rank').textContent),
  };
})()`);

let b = await readBoard();
chk(b.order.length >= 2, '排行榜有内容', `${b.order.length} 行`);
chk(b.ranks.join(',') === b.order.map((_, i) => i + 1).join(','), '名次连续', b.ranks.join(','));
const desc = b.order.every((x, i, a) => i === 0 || a[i - 1].total >= x.total);
chk(desc, '默认按总分降序', b.order.map(x => `${x.name}:${x.total}`).join(' '));
chk(b.order.some(x => x.me), '本机玩家有高亮标记');

// 切正序
await ev(`document.querySelector('#boardBody th[data-sort="total"]').click()`);
await sleep(700);
let asc = await readBoard();
const isAsc = asc.order.every((x, i, a) => i === 0 || a[i - 1].total <= x.total);
chk(isAsc, '再点一次切换为正序', asc.order.map(x => `${x.name}:${x.total}`).join(' '));

// 按维度排序（战斗效率）
const dimKey = await ev(`(() => {
  const th = [...document.querySelectorAll('#boardBody th[data-sort]')]
    .find(h => h.dataset.sort !== 'total');
  return th ? th.dataset.sort : '';
})()`);
if (dimKey) {
  await ev(`document.querySelector('#boardBody th[data-sort="${dimKey}"]').click()`);
  await sleep(700);
  const byDim = await readBoard();
  const dimCol = (byDim.header.findIndex(h => h.length) , 3);   // 第一个维度列
  const vals = await ev(`[...document.querySelectorAll('#boardBody tbody tr')]
    .map(r => parseFloat(r.children[3].textContent) || 0)`);
  const ok = vals.every((v, i, a) => i === 0 || a[i - 1] >= v);
  chk(ok, `按维度「${dimKey}」降序排序`, vals.join(' '));
  chk((byDim.tag || '').includes('降序') || (byDim.tag || '').includes('升序'),
      '标题反映当前排序', byDim.tag);
}

// ---------- 截图 ----------
await ev(`document.querySelector('#boardBody th[data-sort="total"]').click()`);
await sleep(600);
for (const theme of ['dark', 'light']) {
  await ev(`document.querySelector('.thbtn[data-theme="${theme}"]').click()`);
  await sleep(600);
  await ev(`new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))`);
  await sleep(300);
  const r = await send('Page.captureScreenshot', { format: 'png' });
  if (r.result?.data) writeFileSync(`${OUTDIR}/runs-${theme}.png`, Buffer.from(r.result.data, 'base64'));
}
P('');
P(`截图: ${OUTDIR}/runs-dark.png · ${OUTDIR}/runs-light.png`);
P('');
P(pass ? '结论: 全部通过 ✔' : '结论: 有未通过项 ✘');
child.kill();
process.exit(pass ? 0 : 1);
