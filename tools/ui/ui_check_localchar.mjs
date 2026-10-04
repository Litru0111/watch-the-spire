// 验证对局列表标题显示**本机角色**，并在队长不同时标注队长。
import { writeFileSync, mkdirSync } from 'node:fs';
import { connect, reporter } from './_common.mjs';

const OUT = '.sts2mon/shots';
mkdirSync(OUT, { recursive: true });
const { ev, shot, close } = await connect({ hash: '#runs' });
const R = reporter();

// 1) 接口层：character 必须是本机角色
const api = await ev(`(async () => {
  const r = await fetch('/api/runs');
  return (await r.json()).runs;
})()`);
// 判定多人要用「本机角色与队长不同」或明确的多人数，不能只看 player_count 存在
const multi = api.filter(r => r.leader_character && r.character
  && r.leader_character !== r.character);
const mismatch = multi.filter(r => r.leader_character && r.character
  && r.leader_character !== r.character);
R.log(`接口返回 ${api.length} 场 · 本机≠队长 ${multi.length} 场`);
R.check(api.every(r => r.character), '每场都有本机角色',
  api.filter(r => !r.character).map(r => r.run_id).join(',') || '无缺失');

// 2) 界面层：行的 .who 文本应含本机角色；队长不同时应出现「队长 X」
const dom = await ev(`(() => {
  const rows = [...document.querySelectorAll('#runList .runrow')];
  return rows.map(el => ({
    id: el.dataset.runId || '',
    who: (el.querySelector('.who')?.textContent || '').replace(/\\s+/g, ' ').trim(),
  }));
})()`);
R.log(`界面渲染 ${dom.length} 行`);
const withLeaderTag = dom.filter(d => /队长 /.test(d.who));
R.check(withLeaderTag.length > 0, '队长与本机不同时标注了队长',
  `${withLeaderTag.length} 行 · 例: ${withLeaderTag[0]?.who || ''}`);

// 反向核对：不该有行把「队长角色」当成主标题
// 主标题 = .who 里第一个词；若某行主标题等于队长且本机不同，就是没修
const apiById = Object.fromEntries(api.map(r => [r.run_id, r]));
const wrong = dom.filter(d => {
  const a = apiById[d.id];
  if (!a || !a.leader_character || a.leader_character === a.character) return false;
  return d.who.startsWith(a.leader_character);
});
R.check(wrong.length === 0, '没有行把队长角色当成本机角色显示',
  wrong.map(w => w.who).slice(0, 3).join(' | ') || '无');

for (const theme of ['dark', 'light']) {
  await ev(`document.querySelector('.thbtn[data-theme="${theme}"]').click()`);
  await new Promise(r => setTimeout(r, 500));
  await shot(`${OUT}/list-${theme}.png`);
}
R.log(`截图: ${OUT}/list-*.png`);
close();
process.exit(R.finish());
