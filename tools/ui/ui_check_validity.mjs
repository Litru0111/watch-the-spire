// 验证对局列表默认只显示有效对局，且能切到「含无效」。
import { mkdirSync } from 'node:fs';
import { connect, reporter } from './_common.mjs';

const OUT = '.sts2mon/shots';
mkdirSync(OUT, { recursive: true });
const { ev, shot, close } = await connect({ hash: '#runs' });
const R = reporter();

// 接口层：应有 valid / invalid_reason，且无效的能被解释
const api = await ev(`(async () => (await (await fetch('/api/runs')).json()).runs)()`);
const inv = api.filter(r => r.valid === false);
R.log(`接口: ${api.length} 场 · 有效 ${api.length - inv.length} · 无效 ${inv.length}`);
R.check(inv.every(r => r.invalid_reason), '每个无效对局都给出了原因',
  inv.map(r => `${r.run_id}:${r.invalid_reason}`).slice(0, 3).join(' | ') || '无无效项');
R.check(api.every(r => 'valid' in r), '每场都带 valid 标志');

// 界面层：默认过滤下，列表里不应出现无效对局的 run_id
const dom = async () => ev(`(() => {
  const rows = [...document.querySelectorAll('#runList .runrow')];
  return { ids: rows.map(el => el.dataset.runId || ''),
           count: document.querySelector('#runCount')?.textContent || '',
           qualityOn: document.querySelector('#runQuality .subtab.on')?.dataset.quality || '' };
})()`);

const d1 = await dom();
R.log(`默认视图: ${d1.ids.length} 行 · 计数「${d1.count}」· 选中维度「${d1.qualityOn}」`);
R.check(d1.qualityOn === 'valid', '默认选中的是「有效对局」');
const leaked = d1.ids.filter(id => inv.some(r => r.run_id === id));
R.check(leaked.length === 0, '默认列表里没有无效对局',
  leaked.join(', ') || '无');
R.check(d1.ids.length > 0, '默认列表非空', `${d1.ids.length} 行`);

// 切到「含无效」
await ev(`document.querySelector('#runQuality .subtab[data-quality="all"]').click()`);
await new Promise(r => setTimeout(r, 900));
const d2 = await dom();
R.log(`切换后: ${d2.ids.length} 行 · 计数「${d2.count}」· 选中维度「${d2.qualityOn}」`);
R.check(d2.qualityOn === 'all', '切换后选中「含无效」');
R.check(d2.ids.length >= d1.ids.length, '「含无效」的行数不少于默认');
const shown = inv.filter(r => d2.ids.includes(r.run_id));
R.check(shown.length === inv.length, '所有无效对局在「含无效」下可见',
  `${shown.length}/${inv.length}`);

// 人数维度仍要工作（两个维度叠加）
await ev(`document.querySelector('#runQuality .subtab[data-quality="valid"]').click()`);
await new Promise(r => setTimeout(r, 700));
await ev(`document.querySelector('#runFilter .subtab[data-mode="multi"]').click()`);
await new Promise(r => setTimeout(r, 900));
const d3 = await dom();
R.log(`有效×多人: ${d3.ids.length} 行 · 计数「${d3.count}」`);
R.check(d3.ids.length > 0 && d3.ids.length <= d1.ids.length,
  '两个维度可以叠加（有效 × 多人）', `${d3.ids.length} 行`);
const leaked3 = d3.ids.filter(id => inv.some(r => r.run_id === id));
R.check(leaked3.length === 0, '叠加过滤下仍无无效对局');

// 截图
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
