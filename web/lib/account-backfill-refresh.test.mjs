import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {test} from 'node:test';
import React, {act, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const page = readFileSync(new URL('../app/(main)/logs/page.tsx', import.meta.url), 'utf8');
const hookSource = readFileSync(new URL('./use-heartbeat.ts', import.meta.url), 'utf8');
// Execute the production hook and the exact callback wired by LogsPage, not a second
// implementation of its timer/visibility logic. A small React view observes the result.
const compiled = ts.transpileModule(hookSource, {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
}).outputText.replace(/from ['"]react['"]/, `from '${pathToFileURL(require.resolve('react')).href}'`);
const {useHeartbeat} = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
const callback = page.match(/useHeartbeat\((\(\) => \{[\s\S]*?\}), 60000\);/);
assert.ok(callback, 'LogsPage must wire the shared heartbeat');
const makeCallback = new Function('reloadLogs', 'reloadBackfill', 'isAdmin', `return ${callback[1]};`);

test('diagnosis heartbeat updates the rendered hint, visibility and administrator guard', async () => {
  const dom = new JSDOM('<div id="root"></div>', {url: 'http://example.test'});
  const previous = new Map();
  for (const [key, value] of Object.entries({window: dom.window, document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true})) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, {value, configurable: true, writable: true});
  }
  let hidden = false;
  Object.defineProperty(dom.window.document, 'hidden', {get: () => hidden});
  let tick;
  let cleared = false;
  dom.window.setInterval = (fn, ms) => {
    assert.equal(ms, 60000);
    tick = fn;
    return 1;
  };
  dom.window.clearInterval = () => {cleared = true;};
  let checks = 0;
  let logs = 0;
  let nextHint = 'waiting for account';
  function View({isAdmin}) {
    const [hint, setHint] = useState('waiting for account');
    const reloadLogs = () => {logs += 1;};
    const reloadBackfill = async () => {
      checks += 1;
      setHint(nextHint);
    };
    useHeartbeat(makeCallback(reloadLogs, reloadBackfill, isAdmin), 60000);
    return hint ? React.createElement('div', {role: 'alert'}, hint) : null;
  }
  const root = createRoot(dom.window.document.getElementById('root'));
  try {
    await act(async () => {root.render(React.createElement(View, {isAdmin: true}));});
    assert.equal(dom.window.document.querySelector('[role="alert"]').textContent, 'waiting for account');
    nextHint = '';
    await act(async () => {tick();});
    assert.equal(checks, 1);
    assert.equal(dom.window.document.querySelector('[role="alert"]'), null, 'recovery removes the rendered banner');

    hidden = true;
    await act(async () => {
      tick();
      dom.window.document.dispatchEvent(new dom.window.Event('visibilitychange'));
    });
    assert.equal(checks, 1, 'hidden page does not fetch diagnosis');
    assert.equal(logs, 1);
    hidden = false;
    nextHint = 'new account warning';
    await act(async () => {
      dom.window.document.dispatchEvent(new dom.window.Event('visibilitychange'));
    });
    assert.equal(checks, 2, 'returning to the tab refreshes immediately');
    assert.equal(dom.window.document.querySelector('[role="alert"]').textContent, nextHint);

    await act(async () => {root.render(React.createElement(View, {isAdmin: false}));});
    await act(async () => {tick();});
    assert.equal(checks, 2, 'non-administrator must not request diagnosis');
    assert.equal(logs, 3, 'list refresh remains available');
    await act(async () => {root.unmount();});
    assert.equal(cleared, true);
    dom.window.document.dispatchEvent(new dom.window.Event('visibilitychange'));
    assert.equal(checks, 2, 'unmount removes visibility listener');
  } finally {
    dom.window.close();
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test('manual retry refreshes diagnosis and failed checks are visible', () => {
  assert.match(page, /const retry = useCallback\([\s\S]*?if \(isAdmin\) void reloadBackfill\(\)/);
  assert.match(page, /Object\.keys\(backfillErrors\)\.length/);
  assert.match(page, /backfill: isAdmin \? \(\) => logApi\.accountBackfill\(\)/);
});
