/**
 * 账号页排序偏好的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/accounts-sort.test.mjs
 * 或走 Python 包装：`python -m pytest server/tests/test_accounts_sort.py`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：与刷新间隔偏好同一类坑——
 *  · **localStorage 抛错**（隐私模式 / 被禁用）→ 读的地方必须吞掉异常；
 *  · **存了非法值**（旧版留下的、手工改的）→ 必须回默认，不能把乱值当档位渲染；
 *  · **档位清单与实现漂移** → 排序下拉新增一档而实现没跟上，那一档就永远
 *    记不住（刷新回默认）。下面用实现导出的档位清单做**五档逐一「写 → 读」
 *    回环**：实现少认任何一档，对应条目就会失败——不依赖源码级断言也能独立抓住。
 */
import {
  ACCOUNTS_SORT_KEYS,
  getAccountsSort,
  getAccountsSortDirection,
  setAccountsSort,
  setAccountsSortDirection,
} from './display-prefs.ts';

let failed = 0;

function check(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    console.log(`  ok  ${name}`);
    return;
  }
  console.log(`  FAIL ${name}\n       got  ${g}\n       want ${w}`);
  failed += 1;
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

console.log('accounts sort prefs:');

// 没有任何存值 → 默认档 + 升序
withStorage({}, () => {
  check('sort defaults to default', getAccountsSort(), 'default');
  check('direction defaults to asc', getAccountsSortDirection(), 'asc');
});

// 存的值合法 → 原样返回（读路径独立于写路径）
withStorage({'wb.accounts.sort': 'credits', 'wb.accounts.sort-direction': 'desc'}, () => {
  check('reads stored sort', getAccountsSort(), 'credits');
  check('reads stored direction', getAccountsSortDirection(), 'desc');
});
withStorage({'wb.accounts.sort': 'added'}, () => {
  check('reads stored added', getAccountsSort(), 'added');
});

// 存了非法值 → 回默认（不能把乱值当档位渲染）
withStorage({'wb.accounts.sort': 'bogus'}, () => {
  check('rejects unknown sort', getAccountsSort(), 'default');
});
withStorage({'wb.accounts.sort': ''}, () => {
  check('rejects empty sort', getAccountsSort(), 'default');
});
withStorage({'wb.accounts.sort-direction': 'up'}, () => {
  check('rejects unknown direction', getAccountsSortDirection(), 'asc');
});

// 写入 → 能读回来，且落在约定的 key 下；五档逐一回环（实现少认一档这里就挂）
withStorage({}, (store) => {
  for (const key of ACCOUNTS_SORT_KEYS) {
    setAccountsSort(key);
    check(`write then read ${key}`, getAccountsSort(), key);
    check(`persists ${key} under expected key`, store.get('wb.accounts.sort'), key);
  }
  setAccountsSortDirection('desc');
  check('write then read direction', getAccountsSortDirection(), 'desc');
  check('direction persists under expected key', store.get('wb.accounts.sort-direction'), 'desc');
});

// localStorage 抛错 → 读回默认、写不报错（隐私模式不该让页面白屏）
withBrokenStorage(() => {
  check('survives denied read (sort)', getAccountsSort(), 'default');
  check('survives denied read (direction)', getAccountsSortDirection(), 'asc');
  let threw = false;
  try {
    setAccountsSort('credits');
    setAccountsSortDirection('desc');
  } catch {
    threw = true;
  }
  check('survives denied write', threw, false);
});

console.log(failed ? `\n${failed} failed` : '\nall passed');
process.exit(failed ? 1 : 0);
