import assert from 'node:assert/strict';
import {availabilityLabelKey, availabilityOf, disabledReasonText, isAccountBanned, needsRelogin, partitionCredits} from './account-status.ts';
import {collectScopedExpiries} from './dashboard-credits.ts';

const online = {uid: 'ok', in_pool: true, healthy: true, credits: 50};
const banned = {uid: 'ban', disabled: true, credits: 96066,
  disabled_reason: 'account banned by upstream (request illegal, code 11140, http 403), re-login required'};
assert.equal(isAccountBanned(banned), true);
assert.equal(availabilityLabelKey('disabled', banned), 'accounts.badgeBanned');
assert.equal(needsRelogin(banned), false);
assert.equal(disabledReasonText(banned).includes('re-login'), false);
assert.equal(needsRelogin({...banned, disabled_reason: '12153 session dead'}), true);
assert.equal(isAccountBanned({...banned, disabled_reason: '11140 The model provider is rate-limiting requests'}), false);
assert.equal(isAccountBanned({...banned, disabled: false}), false);
assert.equal(isAccountBanned({...banned, disabled_reason: 'account banned by upstream (request illegal, code 11128, http 400), re-login required'}), false);

const cooling = {uid: 'cool', in_pool: true, healthy: false, cooling: true, credits: 10};
const accounts = [online, banned, cooling,
  {uid: 'paused', manual_disabled: true, credits: 20}, {uid: 'expired', is_expired: true, credits: 30},
  {uid: 'unknown', poolUnknown: true, credits: 40}, {uid: 'missing', in_pool: false, credits: 60},
  {uid: 'no-balance', healthy: true}, {uid: 'not-healthy', healthy: false, credits: 70},
  {uid: 'failed', err_total: 1, success_count: 0, credits: 80},
  {...online, uid: 'model-limited', credits: 5, rate_limited_models: [{model: 'x', until: '2099-01-01'}]}];
let result = partitionCredits(accounts, {ok: 100});
assert.deepEqual([result.available, result.unavailable, result.total, result.known, result.unknown],
  [115, 96366, 96481, 10, 1]);
assert.equal(result.usableAccounts.some(a => a.uid === 'ban'), false);
assert.equal(result.usableAccounts.some(a => a.uid === 'cool'), true);
assert.equal(availabilityOf(cooling), 'cooling', 'Credit accounting must not clear the cooldown status');
assert.deepEqual(collectScopedExpiries(result.usableAccounts, {
  cool: {expiries: [{at: 1800000000, amount: 10}]},
  ban: {expiries: [{at: 1800000001, amount: 96066}]},
}), [{at: 1800000000, amount: 10}], 'Available-credit expiry includes cooling accounts only');
for (const restriction of [
  {disabled: true}, {manual_disabled: true}, {disabled_by_panel: true},
  {is_expired: true}, {poolUnknown: true}, {in_pool: false},
  {refresh_risk: {until: Date.now() / 1000 + 300, needs_review: false}},
  {refresh_risk: {until: 0, needs_review: true}},
]) {
  const blocked = {...cooling, ...restriction};
  const totals = partitionCredits([blocked], {});
  assert.deepEqual([totals.available, totals.unavailable], [0, 10],
    `Cooling must not override ${JSON.stringify(restriction)}`);
}
for (const duplicates of [[{...cooling, disabled: true}, cooling], [cooling, {...cooling, disabled: true}]]) {
  const totals = partitionCredits(duplicates, {cool: 25});
  assert.deepEqual([totals.available, totals.unavailable, totals.known], [25, 0, 1],
    'A usable cooling copy in another group counts once, regardless of order');
}
result = partitionCredits([{...online, manual_disabled: true}, online, online], {});
assert.equal(result.available, 50);
assert.equal(result.unavailable, 0);
assert.equal(result.known, 1);
result = partitionCredits([online], {ok: 0});
assert.equal(result.available, 0);
result = partitionCredits([{uid: 'bad', credits: NaN}], {});
assert.equal(result.known, 0);
assert.equal(result.unknown, 1);
console.log('Banned-account classification, recovery queue, credit partition and deduplication passed');
