/**
 * 刷新间隔偏好的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/display-prefs.test.mjs
 * 或走 Python 包装：`python -m pytest server/tests/test_display_prefs.py`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：这个偏好只有一个「读」和一个「写」，看着不可能出错 ——
 * 但它有三个容易踩的坑：
 *  · **localStorage 抛错**（隐私模式 / 被禁用）→ 读的地方必须吞掉异常，
 *    否则页面白屏；
 *  · **存了非法值**（上一版留下的、手工改的）→ 必须回到默认，不能变成
 *    `NaN` 塞进 `setInterval`（那等于每 0 毫秒刷一次，把后端打爆）；
 *  · **默认值漂移** → 默认必须是 20 秒（产品约定）。
 */
import {
  DEFAULT_REFRESH_INTERVAL,
  getRefreshInterval,
  REFRESH_INTERVAL_CHOICES,
  setRefreshInterval,
} from './display-prefs.ts';

let failed = 0;

function check(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    console.log(`  ok  ${name}`);
  } else {
    console.log(`  FAIL ${name}\n       got  ${g}\n       want ${w}`);
    failed += 1;
  }
}

/** 造一个可写的 localStorage 替身（Node 里没有 window）。 */
function withStorage(initial, fn) {
  const store = new Map(Object.entries(initial ?? {}));
  globalThis.window = {
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => { store.set(k, String(v)); },
    },
  };
  try {
    return fn(store);
  } finally {
    delete globalThis.window;
  }
}

/** localStorage 一抛错就返回默认值（隐私模式）。 */
function withBrokenStorage(fn) {
  globalThis.window = {
    localStorage: {
      getItem: () => { throw new Error('denied'); },
      setItem: () => { throw new Error('denied'); },
    },
  };
  try {
    return fn();
  } finally {
    delete globalThis.window;
  }
}

console.log('refresh interval:');

check('default is 20s', DEFAULT_REFRESH_INTERVAL, 20);
check('choices include default', REFRESH_INTERVAL_CHOICES.includes(DEFAULT_REFRESH_INTERVAL), true);

// 没有任何存值 → 默认
withStorage({}, () => {
  check('defaults when unset', getRefreshInterval(), DEFAULT_REFRESH_INTERVAL);
});

// 存的值合法 → 原样返回
withStorage({'wb.tap-refresh-interval': '30'}, () => {
  check('reads stored value', getRefreshInterval(), 30);
});

// 存了非法值 → 回默认（不能变成 NaN：那会让 setInterval 每 0ms 刷一次）
withStorage({'wb.tap-refresh-interval': 'abc'}, () => {
  check('rejects non-numeric', getRefreshInterval(), DEFAULT_REFRESH_INTERVAL);
});
withStorage({'wb.tap-refresh-interval': '0'}, () => {
  check('rejects zero', getRefreshInterval(), DEFAULT_REFRESH_INTERVAL);
});
withStorage({'wb.tap-refresh-interval': '-5'}, () => {
  check('rejects negative', getRefreshInterval(), DEFAULT_REFRESH_INTERVAL);
});
withStorage({'wb.tap-refresh-interval': '7'}, () => {
  check('rejects value not in choices', getRefreshInterval(), DEFAULT_REFRESH_INTERVAL);
});

// 写入 → 能读回来
withStorage({}, (store) => {
  setRefreshInterval(60);
  check('write then read', getRefreshInterval(), 60);
  check('persists under expected key', store.get('wb.tap-refresh-interval'), '60');
});

// localStorage 抛错 → 读回默认、写不报错（隐私模式不该让页面白屏）
withBrokenStorage(() => {
  check('survives denied read', getRefreshInterval(), DEFAULT_REFRESH_INTERVAL);
  let threw = false;
  try {
    setRefreshInterval(10);
  } catch {
    threw = true;
  }
  check('survives denied write', threw, false);
});

console.log(failed ? `\n${failed} failed` : '\nall passed');
process.exit(failed ? 1 : 0);
