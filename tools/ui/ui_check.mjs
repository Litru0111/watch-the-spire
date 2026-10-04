// 新 UI 的验收脚本：用 Edge DevTools Protocol 量真实几何，把
// 「整页不出现滚动条 / 面板内部滚动 / 主题生效 / 导航滑块对齐」变成可断言的结果。
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

// 浏览器路径自动探测（可用 CHROME_PATH 覆盖）
import { browserPath } from './_common.mjs';
const EDGE = browserPath();
const URL = process.argv[2] || 'http://127.0.0.1:8770/#stats';
const PORT = 9344;
const WIN = process.argv[3] || '1680,1000';

const child = spawn(EDGE, [
  '--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`,
  `--window-size=${WIN}`, '--no-first-run', '--hide-scrollbars=false',
  '--user-data-dir=' + (process.env.TEMP || '.') + '\\edge-ui',
  URL,
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
  throw new Error('连不上 DevTools');
}

const page = await target();
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

await sleep(2500);

  const SCRIPT = `(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const out = { };

  // 真实可见性：不能只信 class。
  // 踩过的坑：.hidden{display:none} 被后面同特异性的 .subpane{display:grid} 覆盖，
  // 三个子页同时渲染但被 body 的 overflow:hidden 裁掉，
  // 于是「整页无滚动」这个检查完全发现不了。必须按计算样式判定。
  const vis = e => {
    if (!e) return false;
    if (e.checkVisibility) return e.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
    const cs = getComputedStyle(e);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && e.offsetParent !== null;
  };
  const shown = sel => [...document.querySelectorAll(sel)].filter(vis).length;

  out.viewport = { w: innerWidth, h: innerHeight };
  out.page = {
    scrollH: document.documentElement.scrollHeight,
    clientH: document.documentElement.clientHeight,
    bodyOverflow: getComputedStyle(document.body).overflow,
  };

  // 三个标签页：切过去，量整页是否出现滚动，并确认其它页真的不可见
  out.tabs = {};
  for (const name of ['stats','runs','live']) {
    document.querySelector('.tab[data-tab="'+name+'"]').click();
    await sleep(420);
    const pane = document.getElementById('tab-' + name);
    const r = pane ? pane.getBoundingClientRect() : null;
    out.tabs[name] = {
      paneH: r ? Math.round(r.height) : null,
      pageScrollH: document.documentElement.scrollHeight,
      clientH: document.documentElement.clientHeight,
      extra: document.documentElement.scrollHeight - document.documentElement.clientHeight,
      // 除当前页外，其它标签页必须真的不可见
      visiblePanes: shown('main > div[id^="tab-"]'),
      visibleHeight: [...document.querySelectorAll('main > div[id^="tab-"]')]
        .filter(vis).reduce((a, e) => a + e.getBoundingClientRect().height, 0),
    };
  }

  // 回到统计页量每个子页
  document.querySelector('.tab[data-tab="stats"]').click();
  await sleep(420);

  // 统计页三个子页：每个都要铺满且整页不溢出
  out.subs = {};
  for (const sub of ['overview','progress','deck']) {
    document.querySelector('.subtab[data-sub="' + sub + '"]').click();
    await sleep(420);
    const pane = document.getElementById('sub-' + sub);
    const r = pane ? pane.getBoundingClientRect() : null;
    const slide = document.getElementById('statsSlide');
    const cur = document.querySelector('.subtab.on');
    out.subs[sub] = {
      paneH: r ? Math.round(r.height) : null,
      visible: vis(pane),
      // 关键：可见的子页**必须恰好 1 个**（多一个就是 .hidden 被覆盖了）
      shownPanes: shown('.subpane'),
      pageExtra: document.documentElement.scrollHeight - document.documentElement.clientHeight,
      slideOk: slide.style.left === cur.offsetLeft + 'px'
            && slide.style.width === cur.offsetWidth + 'px',
      panels: pane ? [...pane.children].filter(vis).map(p => {
        const b = p.getBoundingClientRect();
        return { cls: p.className.replace('panel ',''), w: Math.round(b.width), h: Math.round(b.height) };
      }) : [],
    };
  }
  document.querySelector('.subtab[data-sub="overview"]').click();
  await sleep(300);

  // 导航滑块
  const slide = document.getElementById('tabSlide');
  const cur = document.querySelector('.tab.on');
  out.slide = {
    left: slide.style.left, width: slide.style.width,
    wantLeft: cur.offsetLeft + 'px', wantWidth: cur.offsetWidth + 'px',
    ok: slide.style.left === cur.offsetLeft + 'px'
     && slide.style.width === cur.offsetWidth + 'px',
  };

  // 主题
  out.themes = {};
  for (const t of ['light','dark']) {
    document.querySelector('.thbtn[data-theme="'+t+'"]').click();
    await sleep(260);
    out.themes[t] = {
      attr: document.documentElement.getAttribute('data-theme'),
      bodyBg: getComputedStyle(document.body).backgroundColor,
      bodyFg: getComputedStyle(document.body).color,
      paneBg: getComputedStyle(document.querySelector('#tab-stats .panel')).backgroundColor,
      onCount: document.querySelectorAll('.thbtn.on').length,
      chartGrid: getComputedStyle(document.documentElement).getPropertyValue('--line').trim(),
    };
  }
  // 跟随系统档
  document.querySelector('.thbtn[data-theme="auto"]').click();
  await sleep(200);
  out.themes.auto = {
    attr: document.documentElement.getAttribute('data-theme'),
    onCount: document.querySelectorAll('.thbtn.on').length,
  };
  document.querySelector('.thbtn[data-theme="dark"]').click();
  await sleep(200);

  // 统计页各面板几何（看是否铺满且不溢出）
  out.panels = [...document.querySelectorAll('#sub-overview > *')].map(p => {
    const r = p.getBoundingClientRect();
    return { cls: p.className.replace(/panel /g,'').trim() || '?',
             x: Math.round(r.x), w: Math.round(r.width), h: Math.round(r.height) };
  });

  // 内部滚动容器：应该有（承担面板内滚动）
  out.innerScrollers = [...document.querySelectorAll('.scroll,.rounds,.runlist,#mapwrap')]
    .filter(e => e.offsetParent !== null)
    .map(e => ({ sel: e.id || e.className.split(' ')[0],
                 h: e.clientHeight, sh: e.scrollHeight,
                 scrolls: e.scrollHeight > e.clientHeight + 2 }));

  // 画布是否拿到了真实尺寸
  out.canvases = [...document.querySelectorAll('canvas')]
    .filter(c => c.offsetParent !== null)
    .map(c => ({ id: c.id, w: c.width, h: c.height,
                 cw: Math.round(c.getBoundingClientRect().width),
                 ch: Math.round(c.getBoundingClientRect().height) }));

  return out;
})()`;

const r = await send('Runtime.evaluate', {
  expression: SCRIPT, awaitPromise: true, returnByValue: true,
});
const v = r.result?.result?.value;
if (!v) { console.log('取不到结果:', JSON.stringify(r).slice(0, 900)); process.exit(1); }

const P = (s) => process.stdout.write(s + '\n');
P('='.repeat(76));
P(`视口 ${v.viewport.w}×${v.viewport.h}`);
P('='.repeat(76));
P('');
P('① 整页是否出现滚动（关键验收点）');
P(`   页面 scrollHeight=${v.page.scrollH} clientHeight=${v.page.clientH} ` +
  `body overflow=${v.page.bodyOverflow}`);
for (const [k, t] of Object.entries(v.tabs)) {
  const ok = t.extra <= 2;
  P(`   ${ok ? '✔' : '✘'} ${k.padEnd(6)} 面板高=${String(t.paneH).padStart(4)}  ` +
    `页面溢出=${t.extra}px`);
}
P('');
P('② 统计页子页（三个子页都要铺满、且整页不溢出）');
for (const [k, s] of Object.entries(v.subs || {})) {
  const ok = s.visible && s.pageExtra <= 2 && s.slideOk && s.shownPanes === 1;
  P(`   ${ok ? '✔' : '✘'} ${k.padEnd(9)} 高=${String(s.paneH).padStart(4)} ` +
    `溢出=${s.pageExtra}px 可见子页=${s.shownPanes}(应为1) 滑块=${s.slideOk ? '对齐' : '错位'}`);
  for (const p of s.panels)
    P(`        ${p.cls.padEnd(10)} w=${String(p.w).padStart(4)} h=${String(p.h).padStart(4)}`);
}
P('');
P('③ 主导航滑块');
P(`   ${v.slide.ok ? '✔' : '✘'} left=${v.slide.left}(期望${v.slide.wantLeft}) ` +
  `width=${v.slide.width}(期望${v.slide.wantWidth})`);
P('');
P('④ 主题');
for (const [k, t] of Object.entries(v.themes)) {
  P(`   ${k.padEnd(6)} data-theme=${t.attr}  body=${t.bodyBg}  ` +
    (t.paneBg ? `面板=${t.paneBg}  ` : '') + (t.onCount !== undefined ? `高亮按钮=${t.onCount}` : ''));
}
P('');
P('⑤ 总览子页面板（应铺满且互不重叠）');
for (const p of v.panels) {
  P(`   ${p.cls.padEnd(18)} x=${String(p.x).padStart(4)} w=${String(p.w).padStart(4)} h=${String(p.h).padStart(4)}`);
}
P('');
P('⑥ 面板内滚动容器（有内容时应能滚）');
for (const s of v.innerScrollers) {
  P(`   ${s.sel.padEnd(14)} 可视=${String(s.h).padStart(4)} 内容=${String(s.sh).padStart(5)} ` +
    `${s.scrolls ? '可滚动' : '（内容未超出）'}`);
}
P('');
P('⑦ 画布尺寸');
for (const c of v.canvases) {
  P(`   ${c.id.padEnd(12)} 内部=${c.w}×${c.h}  显示=${c.cw}×${c.ch}`);
}

const pass = Object.values(v.tabs).every(t => t.extra <= 2) && v.slide.ok
  && Object.values(v.subs || {}).every(s => s.visible && s.pageExtra <= 2 && s.slideOk && s.shownPanes === 1)
  && v.themes.dark.bodyBg !== v.themes.light.bodyBg;
P('');
P(pass ? '结论: 全部通过 ✔' : '结论: 有未通过项 ✘');
child.kill();
process.exit(pass ? 0 : 1);
