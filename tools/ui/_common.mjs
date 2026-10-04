// 界面验收脚本的共用工具：定位浏览器、连 DevTools。
//
// 各脚本早先各自写死了 Edge 的安装路径，换台机器就全跑不了。
// 这里改成自动探测（Edge → Chrome → 环境变量 CHROME_PATH 覆盖）。
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const CANDIDATES = [
  process.env.CHROME_PATH,
  process.env.EDGE_PATH,
  process.env['ProgramFiles(x86)'] &&
    join(process.env['ProgramFiles(x86)'], 'Microsoft/Edge/Application/msedge.exe'),
  process.env.ProgramFiles &&
    join(process.env.ProgramFiles, 'Microsoft/Edge/Application/msedge.exe'),
  process.env.ProgramFiles &&
    join(process.env.ProgramFiles, 'Google/Chrome/Application/chrome.exe'),
  process.env['ProgramFiles(x86)'] &&
    join(process.env['ProgramFiles(x86)'], 'Google/Chrome/Application/chrome.exe'),
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].filter(Boolean);

export function browserPath() {
  for (const p of CANDIDATES) {
    if (p && existsSync(p)) return p;
  }
  throw new Error(
    '找不到 Chromium 系浏览器。用 CHROME_PATH 指定可执行文件，例如\n' +
    '  set CHROME_PATH=C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe');
}

export function baseUrl() {
  return process.env.STS2MON_URL || `http://127.0.0.1:${process.env.PORT || 8770}`;
}

/**
 * 启动无头浏览器并连上 DevTools。
 * 返回 { ev, send, shot, close }。
 */
export async function connect(opts = {}) {
  const {
    hash = '#runs',
    port = 9500 + Math.floor(Math.random() * 300),
    size = '1680,1000',
    profile = 'wts-ui',
  } = opts;
  const url = opts.url || `${baseUrl()}/${hash}`;
  const dir = join(tmpdir(), `${profile}-${port}`);
  mkdirSync(dir, { recursive: true });

  const child = spawn(browserPath(), [
    '--headless=new', '--disable-gpu', `--remote-debugging-port=${port}`,
    `--window-size=${size}`, '--no-first-run', '--force-device-scale-factor=1',
    `--user-data-dir=${dir}`, url,
  ], { stdio: 'ignore' });

  let target = null;
  for (let i = 0; i < 40 && !target; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      target = list.find(t => t.type === 'page' && t.url.startsWith('http'));
    } catch { /* 还没起来 */ }
    if (!target) await sleep(500);
  }
  if (!target?.webSocketDebuggerUrl) {
    child.kill();
    throw new Error('连不上 DevTools（浏览器没起来？）');
  }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
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
  const ev = async (expr) => (await send('Runtime.evaluate',
    { expression: expr, awaitPromise: true, returnByValue: true }
  )).result?.result?.value;
  const shot = async (file) => {
    await ev(`new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))`);
    await sleep(250);
    const r = await send('Page.captureScreenshot', { format: 'png' });
    if (r.result?.data) {
      const { writeFileSync } = await import('node:fs');
      writeFileSync(file, Buffer.from(r.result.data, 'base64'));
      return true;
    }
    return false;
  };
  const close = () => child.kill();

  await sleep(opts.settle ?? 4000);
  return { ev, send, shot, close, child, url };
}

/** 简易断言器：累计失败，最后统一报告。 */
export function reporter() {
  const results = [];
  const log = (s) => process.stdout.write(s + '\n');
  return {
    log,
    check(cond, label, detail) {
      results.push([!!cond, label]);
      log(`  ${cond ? '✔' : '✘'} ${label}${detail ? '  —— ' + detail : ''}`);
    },
    save(title, info, dir = '.sts2mon/shots', themes = ['dark', 'light']) {
      return { title, info, dir, themes };
    },
    finish() {
      const ok = results.filter(r => r[0]).length;
      log('');
      log(`结果: ${ok}/${results.length} 项通过`);
      for (const [c, l] of results) if (!c) log(`   ✘ ${l}`);
      return ok === results.length ? 0 : 1;
    },
  };
}
