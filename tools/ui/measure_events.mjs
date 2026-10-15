// 用 Edge 的 DevTools Protocol 量事件流面板的真实几何与文本（比截图可靠）
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath, baseUrl} from './_common.mjs';
const EDGE = browserPath();
const URL = process.argv[2] || `${baseUrl()}/#live`;
const PORT = 9333;
const OUT = process.argv[3] || '';

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  '--window-size=1500,1200', '--no-first-run', '--user-data-dir=' +
  (process.env.TEMP || '.') + '\\edge-probe',
  URL,
], { stdio: 'ignore' });

async function targets() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const page = list.find(t => t.type === 'page' && t.url.startsWith(baseUrl()));
      if (page?.webSocketDebuggerUrl) return page;
    } catch { /* 还没起来 */ }
    await sleep(500);
  }
  throw new Error('连不上 DevTools');
}

const page = await targets();
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0;
const pending = new Map();
ws.addEventListener('message', e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
await new Promise(r => ws.addEventListener('open', r));
const send = (method, params = {}) => new Promise(res => {
  const myId = ++id; pending.set(myId, res);
  ws.send(JSON.stringify({ id: myId, method, params }));
});

await sleep(9000);   // 等前端轮询几轮把事件流填满
// 直接打接口看后端存了什么（用实时对局的 run_id）
const lv = await (await fetch(`${baseUrl()}/api/live`)).json();
const ev = await (await fetch(`${baseUrl()}/api/events?id=` +
  encodeURIComponent(lv.run_id || '') + '&limit=30')).json();
console.log(`--- 后端事件记录 (run_id=${lv.run_id}) ---`);
for (const e of (ev.events || []).slice(0, 16)) {
  console.log(`  T${e.turn} [${e.side}] ${e.kind.padEnd(10)} label=${e.label}` +
    `  actor=${e.detail && e.detail.actor} kind=${e.detail && e.detail.actor_kind}`);
}
console.log('');

const r = await send('Runtime.evaluate', {
  expression: `(() => {
    const rows = sel => [...document.querySelectorAll(sel + ' tbody tr')]
      .map(tr => [...tr.children].map(td => td.textContent.trim().replace(/\\s+/g,' ')).join(' | '));
    const drafts = document.querySelector('#liveDrafts');
    const choices = document.querySelector('#liveChoices');
    return JSON.stringify({
      draftTag: (document.querySelector('#liveDraftTag')||{}).textContent || '',
      draftRows: rows('#liveDrafts'),
      choiceTag: (document.querySelector('#liveChoiceTag')||{}).textContent || '',
      choiceRows: rows('#liveChoices'),
      draftHidden: drafts ? drafts.closest('.panel').classList.contains('hidden') : null,
      choiceHidden: choices ? choices.closest('.panel').classList.contains('hidden') : null,
    });
  })()`, returnByValue: true,
});
try {
  const v = JSON.parse(r.result.result.value);
  console.log('--- 卡牌抓取监控（渲染结果）---');
  console.log('  标签:', v.draftTag, v.draftHidden ? '(面板隐藏!)' : '');
  for (const row of v.draftRows) console.log('   ', row);
  console.log('--- 事件选择监控（渲染结果）---');
  console.log('  标签:', v.choiceTag, v.choiceHidden ? '(面板隐藏!)' : '');
  for (const row of v.choiceRows) console.log('   ', row);
} catch (e) { console.log('测量失败', r.result && r.result.result); }
console.log(JSON.stringify(JSON.parse(r.result.result.value), null, 2));
if (OUT) {
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  const fs = await import('node:fs');
  await fs.promises.writeFile(OUT, Buffer.from(shot.result.data, 'base64'));
  console.log('截图已保存:', OUT);
}
ws.close();
child.kill();
await sleep(300);      // 等文件写完再退出，否则截图会丢
process.exit(0);
