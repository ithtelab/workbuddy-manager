import assert from 'node:assert/strict';
import {scheduleDelayError} from './schedule-validation.ts';
const valid = {random_delay_minutes: 60, account_delay_min_seconds: 30, account_delay_max_seconds: 120};
assert.equal(scheduleDelayError(valid), undefined);
assert.equal(scheduleDelayError({}), undefined);
assert.equal(scheduleDelayError({...valid, account_delay_min_seconds: 0, account_delay_max_seconds: 0}), undefined);
assert.equal(scheduleDelayError({...valid, account_delay_min_seconds: 600, account_delay_max_seconds: 600}), undefined);
assert.equal(scheduleDelayError({...valid, account_delay_min_seconds: 121}), '账号最短间隔不能大于最长间隔');
assert.equal(scheduleDelayError({...valid, account_delay_max_seconds: 29}), '账号最短间隔不能大于最长间隔');
for (const key of Object.keys(valid)) {
 for (const value of [-1, 1.5, true, '30', NaN, Infinity, 601]) {
  assert.ok(scheduleDelayError({...valid, [key]: value}), `${key} accepted ${value}`);
 }
}
assert.ok(scheduleDelayError({...valid, random_delay_minutes: 61}));
console.log('schedule form validation passed: bounds, integer types, merged ranges, zero defaults');
