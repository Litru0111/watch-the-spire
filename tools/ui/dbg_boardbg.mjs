// 诊断排行榜「本机行」的背景到底有没有生效。
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath, findSeed, baseUrl} from './_common.mjs';
const EDGE = browserPath();
const PORT = 9455;
// 排行榜只在多人局里有意义；种子从本机数据取，不写死作者的对局
const SEED = process.argv[2] || await findSeed({ players: 3 }) || await findSeed({ players: 2 });
const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  '--window-size=1680,1000', '--no-first-run',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-bg',
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

await sleep(4500);
await ev(`document.querySelector('.thbtn[data-theme="light"]').click()`);
await sleep(700);

console.log(await ev(`(() => {
  const out = [];
  const root = getComputedStyle(document.documentElement);
  out.push('--hover = ' + JSON.stringify(root.getPropertyValue('--hover')));
  out.push('--rowhl = ' + JSON.stringify(root.getPropertyValue('--rowhl')));
  out.push('--refbg = ' + JSON.stringify(root.getPropertyValue('--refbg')));
  const tr = document.querySelector('#boardBody tbody tr.me');
  if (!tr) { out.push('没有 tr.me'); return out.join('\\n'); }
  out.push('tr.me computed background = ' + getComputedStyle(tr).backgroundColor);
  [...tr.children].forEach((td, i) => {
    out.push('  td[' + i + '] bg=' + getComputedStyle(td).backgroundColor
      + '  inline=' + JSON.stringify(td.getAttribute('style') || ''));
  });
  // 有没有其它规则把它覆盖了
  out.push('--- 命中的背景规则 ---');
  for (const ss of document.styleSheets) {
    let rules; try { rules = ss.cssRules; } catch { continue; }
    for (const r of rules) {
      if (!r.selectorText || !r.style || !r.style.background && !r.style.backgroundColor) continue;
      try { if (tr.matches(r.selectorText)) out.push('  ' + r.selectorText + ' { ' + (r.style.backgroundColor || r.style.background) + ' }'); }
      catch {}
    }
  }
  return out.join('\\n');
})()`));

child.kill();
