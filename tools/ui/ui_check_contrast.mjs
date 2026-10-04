// 验证主题对比度：本机玩家行是否「高亮」而不是「涂黑」，以及文本可读性。
// 判据：行底色与面板底色的**亮度差**要小（高亮 = 同色系稍亮/稍暗），
//       而文字与底色的对比度要够（WCAG 相对亮度比 >= 3）。
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath } from './_common.mjs';
const EDGE = browserPath();
const PORT = 9444;
const SEED = process.argv[2] || 'K3C083K7D8';

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  '--window-size=1680,1000', '--no-first-run',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-contrast',
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

const probe = `(() => {
  const lum = c => {
    const m = c.match(/\\d+/g); if (!m) return 0;
    const [r, g, b] = m.slice(0, 3).map(Number).map(v => {
      v /= 255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4);
    });
    return 0.2126*r + 0.7152*g + 0.0722*b;
  };
  const ratio = (a, b) => {
    const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
    return (x + 0.05) / (y + 0.05);
  };
  const out = { rows: [], theme: document.documentElement.getAttribute('data-theme') };

  // 局后总结里的「玩家对比」表：找出本机行与普通行
  const tables = [...document.querySelectorAll('#summaryBody table')];
  const cmp = tables.find(t => /玩家/.test(t.querySelector('th')?.textContent || ''));
  if (cmp) {
    const panelBg = getComputedStyle(cmp.closest('.panel')).backgroundColor;
    out.panelBg = panelBg;
    [...cmp.querySelectorAll('tbody tr')].forEach(tr => {
      const bg = getComputedStyle(tr).backgroundColor;
      const td = tr.querySelector('td');
      const fg = getComputedStyle(td).color;
      const isMe = /★/.test(tr.textContent);
      out.rows.push({ isMe, bg, fg,
        lumBg: +lum(bg).toFixed(3), lumPanel: +lum(panelBg).toFixed(3),
        contrast: +ratio(fg, bg === 'rgba(0, 0, 0, 0)' ? panelBg : bg).toFixed(2) });
    });
  }
  // 排行榜里的本机行。
  // 注意：底色在 <td> 上，<tr> 的 background 是透明的（实测踩过：
  // 读 <tr> 会得到 rgba(0,0,0,0)，误判成「没有高亮」）。
  const board = document.querySelector('#boardBody tbody tr.me');
  if (board) {
    const td = board.querySelector('td');
    const bg = getComputedStyle(td).backgroundColor;
    const panelBg = getComputedStyle(document.querySelector('#boardPanel .panel')).backgroundColor;
    const otherTd = document.querySelector('#boardBody tbody tr:not(.me) td');
    out.boardMe = { bg, panelBg, lumBg: +lum(bg).toFixed(3), lumPanel: +lum(panelBg).toFixed(3),
                    lumOther: otherTd ? +lum(getComputedStyle(otherTd).backgroundColor || panelBg).toFixed(3) : null,
                    contrast: +ratio(getComputedStyle(td).color, bg).toFixed(2) };
  }
  return out;
})()`;

for (const theme of ['dark', 'light']) {
  await ev(`document.querySelector('.thbtn[data-theme="${theme}"]').click()`);
  await sleep(700);
  const r = await ev(probe);
  P(`【${theme}】面板底色亮度=${r.panelBg}`);
  if (!r.rows.length) { P('  （没有找到玩家对比表）'); continue; }
  const me = r.rows.filter(x => x.isMe), others = r.rows.filter(x => !x.isMe);
  for (const x of r.rows)
    P(`   ${x.isMe ? '本机' : '其他'}  行底色=${x.bg}  亮度=${x.lumBg}  ` +
      `与面板差=${(x.lumBg - x.lumPanel).toFixed(3)}  文字对比度=${x.contrast}`);
  if (me.length) {
    const m = me[0];
    const diff = Math.abs(m.lumBg - m.lumPanel);
    // 高亮：同色系、亮度差不大（< 0.25）。涂黑会差很多（深色行 vs 浅色面板）。
    chk(diff < 0.25, `${theme}：本机行是「高亮」而非「涂黑」`,
        `亮度差 ${diff.toFixed(3)}`);
    // 而且必须与其它行不同（真的被区分出来）
    const o = others[0];
    chk(o ? Math.abs(m.lumBg - o.lumBg) > 0.005 : false,
        `${theme}：本机行与其它行有可见区分`,
        o ? `本机 ${m.lumBg} vs 其他 ${o.lumBg}` : '无其它行');
    chk(m.contrast >= 3, `${theme}：本机行文字对比度达标`, `${m.contrast}:1`);
  }
  if (r.boardMe) {
    const b = r.boardMe;
    chk(Math.abs(b.lumBg - b.lumPanel) < 0.35 && b.contrast >= 3,
        `${theme}：排行榜本机行高亮且可读`,
        `底色 ${b.bg}  与面板亮度差 ${(b.lumBg - b.lumPanel).toFixed(3)}  对比度 ${b.contrast}:1`);
  }
  P('');
}
P(pass ? '结论: 主题对比度正常 ✔' : '结论: 存在对比度问题 ✘');
child.kill();
process.exit(pass ? 0 : 1);
