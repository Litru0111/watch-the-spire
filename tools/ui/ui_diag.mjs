// 诊断：点实时监控后，到底是哪个面板可见、导航高亮在哪。
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath } from './_common.mjs';
const EDGE = browserPath();
const PORT = 9366;

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  '--window-size=1680,1000', '--no-first-run',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-diag',
  'http://127.0.0.1:8770/#stats',
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
const ev = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  return r.result?.result?.value;
};

await sleep(3000);
console.log('初始 hash =', await ev('location.hash'));
console.log('初始可见:', await ev(`
  ['stats','runs','live'].map(k => {
    const e = document.getElementById('tab-'+k);
    return k + '=' + (e.classList.contains('hidden') ? 'hidden' : 'visible');
  }).join(' ')`));

// 模拟点击「实时监控」
console.log('\n--- 点击 实时监控 ---');
console.log('点击前:', await ev(`(() => {
  const t = document.querySelector('.tab[data-tab="live"]');
  return 'found=' + !!t + ' onclick=' + (typeof t.onclick);
})()`));
await ev(`document.querySelector('.tab[data-tab="live"]').click()`);
await sleep(1200);
console.log('点击后 hash =', await ev('location.hash'));
console.log('可见状态:', await ev(`
  ['stats','runs','live'].map(k => {
    const e = document.getElementById('tab-'+k);
    return k + '=' + (e.classList.contains('hidden') ? 'hidden' : 'visible');
  }).join(' ')`));
console.log('导航高亮:', await ev(`
  [...document.querySelectorAll('.tab[data-tab]')].map(t => t.dataset.tab + (t.classList.contains('on')?'*':'')).join(' ')`));
console.log('滑块:', await ev(`(() => {
  const s = document.getElementById('tabSlide');
  const c = document.querySelector('.tab.on');
  return s.style.left + '/' + s.style.width + '  期望 ' + c.offsetLeft + '/' + c.offsetWidth;
})()`));

// 各标签页的实际几何
console.log('\n各标签页几何:');
console.log(await ev(`
  [...document.querySelectorAll('main > div[id^="tab-"]')].map(e => {
    const r = e.getBoundingClientRect();
    return e.id + ' hidden=' + e.classList.contains('hidden')
      + ' w=' + Math.round(r.width) + ' h=' + Math.round(r.height)
      + ' display=' + getComputedStyle(e).display;
  }).join('\\n')`));

// 统计页那些面板此刻是否被再次填充（说明有代码在偷偷刷统计）
console.log('\n统计页面板当前内容长度:');
console.log(await ev(`
  ['ovCards','charTable','ascTable','deathTable','encTable','cardTable','pickTable']
    .map(id => id + '=' + (document.getElementById(id)?.innerHTML.length ?? -1)).join(' ')`));

// live 面板内容
console.log('\nlive 面板内容长度:');
console.log(await ev(`
  ['liveBody','livePredict','liveCards','liveLog','liveDrafts','liveChoices']
    .map(id => id + '=' + (document.getElementById(id)?.innerHTML.length ?? -1)).join(' ')`));

// window.onerror / 未捕获异常
console.log('\n捕获的错误:', await ev(`JSON.stringify(window.__errs || [])`));

child.kill();
