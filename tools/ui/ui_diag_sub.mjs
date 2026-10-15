// 精确诊断统计页子页：哪个可见、各占多少高度、为什么没被隐藏。
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath, baseUrl} from './_common.mjs';
const EDGE = browserPath();
const PORT = 9388;
const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  '--window-size=1680,1000', '--no-first-run',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-diag2',
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

await sleep(3000);

const dump = async (tag) => {
  const s = await ev(`(() => {
    const o = {};
    o.subtabs = [...document.querySelectorAll('.subtab')].map(b =>
      b.dataset.sub + (b.classList.contains('on') ? '*' : ''));
    o.panes = ['overview','progress','deck'].map(k => {
      const e = document.getElementById('sub-' + k);
      if (!e) return k + '=MISSING';
      const r = e.getBoundingClientRect();
      return k + ': hidden=' + e.classList.contains('hidden')
        + ' display=' + getComputedStyle(e).display
        + ' h=' + Math.round(r.height) + ' w=' + Math.round(r.width);
    });
    o.statsH = Math.round(document.getElementById('tab-stats').getBoundingClientRect().height);
    o.mainH = Math.round(document.querySelector('main').getBoundingClientRect().height);
    o.pageExtra = document.documentElement.scrollHeight - document.documentElement.clientHeight;
    return o;
  })()`);
  console.log(`--- ${tag} ---`);
  console.log('  subtabs:', s.subtabs.join(' '));
  s.panes.forEach(p => console.log('  ' + p));
  console.log(`  #tab-stats h=${s.statsH}  main h=${s.mainH}  整页溢出=${s.pageExtra}px`);
  console.log('');
};

await dump('初始（overview）');
for (const sub of ['progress', 'deck', 'overview']) {
  await ev(`document.querySelector('.subtab[data-sub="${sub}"]').click()`);
  await sleep(500);
  await dump(`点击 ${sub} 后`);
}

// 检查 CSS 是否真的存在 .subpane.hidden 规则
console.log('CSS 里 .subpane 与 .hidden 的规则:');
console.log(await ev(`(() => {
  const out = [];
  for (const ss of document.styleSheets) {
    let rules; try { rules = ss.cssRules; } catch { continue; }
    for (const r of rules) {
      if (r.selectorText && (r.selectorText.includes('subpane') || r.selectorText === '.hidden'
          || r.selectorText.includes('.hidden')))
        out.push(r.selectorText + ' { ' + r.style.cssText.slice(0, 80) + ' }');
    }
  }
  return out.join('\\n');
})()`));

child.kill();
