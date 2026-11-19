import assert from 'node:assert/strict';
import {availabilityOf, availabilityLabelKey, availabilityTitleKey, availabilityGroup} from './account-status.ts';

const account = {uid:'paused', refresh_risk:{until:Date.now()/1000+300, needs_review:false}};
assert.equal(availabilityOf(account), 'riskPaused');
assert.equal(availabilityLabelKey('riskPaused', account), 'risk.paused');
assert.equal(availabilityTitleKey('riskPaused', account), 'risk.recovery');
assert.equal(availabilityGroup('riskPaused'), 'attention');
assert.equal(availabilityOf({...account, refresh_risk:{until:0, needs_review:true}}), 'riskPaused');
assert.equal(availabilityOf({...account, refresh_risk:{until:0, needs_review:false}}), 'online');
const manual = {uid:'manual', manual_disabled:true, manual_reason:'account-risk: arbitrary operator reason'};
assert.equal(availabilityOf(manual), 'manualDisabled');
assert.equal(availabilityLabelKey('manualDisabled', manual), 'accounts.badgeManualDisabled');
assert.equal(availabilityTitleKey('manualDisabled', manual), 'accounts.badgeManualDisabledTitle');
const cooldown = {uid:'soft11140', cooling:true, disabled:false, disabled_reason:'11140 request illegal'};
assert.equal(availabilityOf(cooldown), 'cooling');
assert.equal(availabilityLabelKey('cooling', cooldown), 'accounts.badgeCooling');
console.log('PASS: typed refresh hold, expiry, review, manual independence and 11140 soft cooldown');
