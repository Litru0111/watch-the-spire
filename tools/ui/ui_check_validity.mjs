// 验证对局列表**只显示有效对局**，而且界面上不再有「含无效」开关。
//
// 这条契约变过一次，值得写清楚：
//   以前是「默认只看有效 + 一个『含无效』开关」——那是开发期排查用的入口。
//   现在无效对局（回放残留 / 空对局 / 截断采集）在 loadRuns() 里就被丢掉，
//   界面上**没有**任何途径让它们出现。
//
// 所以这里分两层验：
//   * 接口层：/api/runs 仍然返回全部对局并带 valid / invalid_reason
//     （排查数据源问题时直接看接口，不需要为它留用户开关）；
//   * 界面层：一个无效对局都不许露出来，且开关确实不存在了。
import { mkdirSync } from 'node:fs';
import { connect, reporter } from './_common.mjs';

const OUT = '.sts2mon/shots';
mkdirSync(OUT, { recursive: true });
const { ev, shot, close } = await connect({ hash: '#runs' });
const R = reporter();

// ---------- 接口层：valid 标记与原因仍在 ----------
const api = await ev(`(async () => (await (await fetch('/api/runs')).json()).runs)()`);
const inv = api.filter(r => r.valid === false);
R.log(`接口: ${api.length} 场 · 有效 ${api.length - inv.length} · 无效 ${inv.length}`);
R.check(inv.length > 0, '接口仍返回无效对局（排查需要）', `${inv.length} 场`);
R.check(inv.every(r => r.invalid_reason), '每个无效对局都给出了原因',
  inv.map(r => `${r.run_id}:${r.invalid_reason}`).slice(0, 3).join(' | ') || '无无效项');
R.check(api.every(r => 'valid' in r), '每场都带 valid 标志');

// ---------- 界面层：无效对局不可达 ----------
const dom = async () => ev(`(() => {
  const rows = [...document.querySelectorAll('#runList .runrow')];
  const on = document.querySelector('#runFilter .subtab.on');
  return { ids: rows.map(el => el.dataset.runId || ''),
           count: document.querySelector('#runCount')?.textContent || '',
           modeOn: on?.dataset.mode || '' };
})()`);

const d1 = await dom();
R.log(`默认视图: ${d1.ids.length} 行 · 计数「${d1.count}」· 人数维度「${d1.modeOn}」`);
R.check(d1.ids.length > 0, '列表非空', `${d1.ids.length} 行`);
const leaked = d1.ids.filter(id => inv.some(r => r.run_id === id));
R.check(leaked.length === 0, '列表里没有无效对局', leaked.join(', ') || '无');
R.check(d1.ids.length === api.length - inv.length,
  `列表行数 = 有效对局数（${api.length - inv.length}）`, `${d1.ids.length} 行`);

// 开关必须已经不存在——它不该只是"被点不到"，而是从 DOM 里拿掉
const gone = await ev(`({
    qualityNav: !!document.querySelector('#runQuality'),
    qualityBtn: [...document.querySelectorAll('.subtab')]
                  .some(b => (b.textContent || '').includes('含无效')),
    qualityState: typeof runQuality })`);
R.check(!gone.qualityNav, '「含无效」过滤条已从 DOM 移除');
R.check(!gone.qualityBtn, '页面上不存在「含无效」按钮');
R.check(gone.qualityState === 'undefined', 'runQuality 状态与相关逻辑已删除',
  `typeof runQuality = ${gone.qualityState}`);

// 计数文案不该再带「/ 总数」
R.check(/^\d+ 场$/.test(d1.count.trim()), '计数文案为「N 场」', d1.count);

// ---------- 人数维度仍要工作 ----------
await ev(`document.querySelector('#runFilter .subtab[data-mode="multi"]').click()`);
await new Promise(r => setTimeout(r, 900));
const d2 = await dom();
R.log(`多人: ${d2.ids.length} 行 · 计数「${d2.count}」`);
R.check(d2.modeOn === 'multi', '人数维度可切换', d2.modeOn);
R.check(d2.ids.length > 0 && d2.ids.length < d1.ids.length,
  '多人是全部的真子集', `${d2.ids.length} < ${d1.ids.length}`);
R.check(d2.ids.filter(id => inv.some(r => r.run_id === id)).length === 0,
  '切到多人后仍无无效对局');

const d3 = await ev(`(() => {
  document.querySelector('#runFilter .subtab[data-mode="single"]').click();
  return null; })()`);
await new Promise(r => setTimeout(r, 900));
const sin = await dom();
R.check(sin.ids.length + d2.ids.length === d1.ids.length,
  '单人 + 多人 = 全部（分类无遗漏、无重复）',
  `${sin.ids.length} + ${d2.ids.length} = ${d1.ids.length}`);

// ---------- 截图 ----------
await ev(`document.querySelector('#runFilter .subtab[data-mode="all"]').click()`);
await new Promise(r => setTimeout(r, 800));
for (const theme of ['dark', 'light']) {
  await ev(`document.querySelector('.thbtn[data-theme="${theme}"]').click()`);
  await new Promise(r => setTimeout(r, 500));
  await shot(`${OUT}/list-${theme}.png`);
}
R.log(`截图: ${OUT}/list-*.png`);
close();
process.exit(R.finish());
