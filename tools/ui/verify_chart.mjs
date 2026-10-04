// 用 Edge DevTools 协议验证柱状图：红柱（本回合受伤）是否显示 + 悬停提示是否出现。
// 用法: node tools/verify_chart.mjs <调试端口> <目标URL>
const [port, url] = process.argv.slice(2);
const DEBUG = `http://127.0.0.1:${port}`;

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  // 自己开一个新标签页，避免误抓到 Edge 自己的内部页面
  const created = await (await fetch(`${DEBUG}/json/new?about:blank`, { method: 'PUT' })).json();
  const page = created;
  if (!page || !page.webSocketDebuggerUrl) { console.log('无法新建标签页'); process.exit(1); }
  console.log('新建标签页:', page.id);

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const send = (method, params = {}) => new Promise(res => {
    const mid = ++id;
    pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  const events = [];
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
    else events.push(m);
  });
  await new Promise(r => ws.addEventListener('open', r));

  await send('Runtime.enable');
  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride',
             { width: 1500, height: 1200, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url });
  await sleep(7000);
  const where = await send('Runtime.evaluate',
                           { expression: 'location.href', returnByValue: true });
  console.log('当前地址:', where.result?.value);

  // 读取图表内部状态
  const probe = await send('Runtime.evaluate', {
    expression: `(() => {
      const c = document.querySelector('#liveChart');
      if (!c) return {error: '找不到 canvas'};
      return {
        canvasW: c.width, canvasH: c.height,
        cssW: c.clientWidth,
        geo: typeof liveChartGeo !== 'undefined' ? liveChartGeo : null,
        turns: typeof liveTurns !== 'undefined' ? liveTurns : null,
        tag: document.querySelector('#liveTurnTag')?.textContent || '',
      };
    })()`, returnByValue: true,
  });
  const info = probe.result?.value || {};
  console.log('\n[1] 图表内部状态');
  console.log('   canvas 尺寸:', info.canvasW, 'x', info.canvasH, ' css 宽度:', info.cssW);
  console.log('   标题:', info.tag);
  console.log('   数据点:', JSON.stringify(info.turns));
  const turns = info.turns || [];
  const withTaken = turns.filter(t => t.taken > 0);
  console.log(`   有承伤的回合: ${withTaken.length} / ${turns.length}`,
    withTaken.length ? `→ ${withTaken.map(t => 'T'+t.turn+':'+t.taken).join(', ')}` : '');

  if (!info.geo) { console.log('   ✘ 没有几何信息，图表可能没绘制'); ws.close(); process.exit(1); }
  const { x0, slot, padL } = info.geo;
  const scale = info.canvasW / Math.max(info.cssW, 1);
  console.log('   几何:', JSON.stringify(info.geo), ' scale=', scale);
  // 选一个有承伤的回合（没有就选最后一个）
  const idx = withTaken.length ? turns.indexOf(withTaken[withTaken.length - 1]) : turns.length - 1;
  const cssX = (x0 + idx * slot) / scale;
  const cssY = info.canvasH * 0.5 / scale;

  // 先确认 canvas 在视口中的位置，换算成页面坐标
  const rectInfo = await send('Runtime.evaluate', {
    expression: `(() => { const r = document.querySelector('#liveChart').getBoundingClientRect();
      return {left: r.left, top: r.top, w: r.width, h: r.height}; })()`,
    returnByValue: true,
  });
  const rect = rectInfo.result?.value || {left: 0, top: 0, w: 0, h: 0};
  console.log('   canvas 视口位置:', JSON.stringify(rect));
  const pageX = rect.left + cssX;
  const pageY = rect.top + (info.canvasH / scale) * 0.5;

  console.log(`\n[2] 把鼠标移到第 ${turns[idx]?.turn} 回合的柱子上（页面 x=${pageX.toFixed(1)}, y=${pageY.toFixed(1)}）`);
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: pageX, y: pageY, buttons: 0 });
  await sleep(500);

  const tipState = `(() => {
    const tip = document.querySelector('#liveTip');
    if (!tip) return {exists: false};
    const st = getComputedStyle(tip);
    const r = tip.getBoundingClientRect();
    return {exists: true, hidden: tip.classList.contains('hidden'),
            text: tip.textContent.trim().replace(/\\s+/g, ' '),
            display: st.display, visibility: st.visibility, opacity: st.opacity,
            w: Math.round(r.width), h: Math.round(r.height),
            hover: typeof liveHoverIdx !== 'undefined' ? liveHoverIdx : null,
            cursor: document.querySelector('#liveChart').style.cursor};
  })()`;

  const read = async () => (await send('Runtime.evaluate',
    { expression: tipState, returnByValue: true })).result?.value || {};
  const t1 = await read();
  console.log('   悬停 0.5s 后:', JSON.stringify(t1));

  // 关键：跨越 2 次轮询刷新（1s 一次），确认提示框不会被重绘擦掉
  const samples = [];
  for (let i = 0; i < 5; i++) { await sleep(500); samples.push(await read()); }
  const alive = samples.filter(s => s.exists && !s.hidden && s.w > 0);
  console.log(`   持续观察 2.5s（跨 2 次轮询重绘）: 提示框可见 ${alive.length}/5 次采样`);
  console.log('   最后一次内容:', alive.length ? alive[alive.length - 1].text : '(不可见)');

  const ok = t1.exists && !t1.hidden && t1.w > 0 && alive.length === samples.length
             && /第 \d+ 回合/.test(t1.text) && t1.hover === idx;
  console.log(ok ? '   ✔ 提示框正常显示且能扛住轮询重绘'
                 : '   ✘ 提示框有问题（隐藏/被擦掉/内容不对）');

  // 截图（含提示框）
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  if (shot?.data) {
    const fs = await import('node:fs');
    fs.writeFileSync('.sts2mon/shots/chart_hover.png', Buffer.from(shot.data, 'base64'));
    console.log('   已保存截图: .sts2mon/shots/chart_hover.png');
  }
  ws.close();
}

main().catch(e => { console.error('失败:', e); process.exit(1); });
