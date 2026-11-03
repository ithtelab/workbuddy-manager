import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';

const page = readFileSync(new URL('../app/(main)/logs/page.tsx', import.meta.url), 'utf8');
const heartbeat = readFileSync(new URL('./use-heartbeat.ts', import.meta.url), 'utf8');

test('logs heartbeat refreshes account diagnosis only for administrators', () => {
  assert.match(page, /useHeartbeat\(\(\) => \{\s*void reloadLogs\(\);\s*if \(isAdmin\) void reloadBackfill\(\);\s*\}, 60000\)/);
  assert.match(page, /backfill: isAdmin \? \(\) => logApi\.accountBackfill\(\)/);
});

test('manual retry refreshes diagnosis and failed checks are visible', () => {
  assert.match(page, /const retry = useCallback\([\s\S]*?if \(isAdmin\) void reloadBackfill\(\)/);
  assert.match(page, /Object\.keys\(backfillErrors\)\.length/);
});

test('shared heartbeat pauses in background and refreshes when visible', () => {
  assert.match(heartbeat, /if \(!document\.hidden\) ref\.current\(\)/);
  assert.match(heartbeat, /addEventListener\('visibilitychange', tick\)/);
  assert.match(heartbeat, /removeEventListener\('visibilitychange', tick\)/);
});
