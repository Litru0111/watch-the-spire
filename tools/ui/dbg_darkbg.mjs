// 精确列出总结面板里每个可见元素的底色，找出亮色主题下仍然是深色的那个。
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath, findSeed, baseUrl} from './_common.mjs';
const EDGE = browserPath();
const PORT = 9466;
// 看总结面板的底色，随便挑一场即可 —— 种子从本机数据取，不写死
const SEED = process.argv[2] || await findSeed();
const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  '--window-size=1680,1000', '--no-first-run',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-dark',
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
await sleep(800);

const dump = await ev(`(() => {
  const lum = c => {
    const m = c.match(/[\\d.]+/g); if (!m) return null;
    const [r,g,b] = m.slice(0,3).map(Number).map(v => {
      v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4);
    });
    return +(0.2126*r+0.7152*g+0.0722*b).toFixed(3);
  };
  const out = [];
  const walk = (root, depth) => {
    for (const el of root.children) {
      const r = el.getBoundingClientRect();
      if (r.width < 40 || r.height < 12) continue;
      const cs = getComputedStyle(el);
      const bg = cs.backgroundColor;
      const L = lum(bg);
      // 只在「可见且有明确底色」时报告；深色底（亮度<0.2）且当前是亮主题 = 可疑
      const dark = L !== null && L < 0.2 && !/rgba\\(0, 0, 0, 0\\)/.test(bg);
      out.push({ d: depth, tag: el.tagName.toLowerCase(),
                 id: el.id || '', cls: (el.className || '').toString().slice(0, 26),
                 bg, lum: L, dark,
                 w: Math.round(r.width), h: Math.round(r.height),
                 txt: (el.textContent || '').trim().slice(0, 22) });
      if (depth < 4) walk(el, depth + 1);
    }
  };
  walk(document.querySelector('.rundetail'), 0);
  return { theme: document.documentElement.getAttribute('data-theme'),
           refbg: getComputedStyle(document.documentElement).getPropertyValue('--refbg'),
           items: out.filter(x => x.dark) };
})()`);

console.log(`主题=${dump.theme}  --refbg=${JSON.stringify(dump.refbg)}`);
console.log(`亮色主题下仍然是深色底的可见元素: ${dump.items.length} 个`);
for (const x of dump.items) {
  console.log(`  ${'  '.repeat(x.d)}<${x.tag}${x.id ? '#' + x.id : ''}` +
    `${x.cls ? '.' + x.cls.replace(/\\s+/g, '.') : ''}>  bg=${x.bg} 亮度=${x.lum} ` +
    `${x.w}×${x.h}  「${x.txt}」`);
}
child.kill();
