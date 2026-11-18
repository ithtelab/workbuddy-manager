/**
 * 「限时免费」判定的行为测试（`web/lib/model-free.ts`，issue #446）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/model-free.test.mjs
 *
 * 为什么值得测：判据放宽过头会把付费模型标成免费（用户白花额度），
 * 收得太紧又是这次的 bug（筛选命中 0 条）。两个方向都要钉住。
 */
import {isFreeModel} from './model-free.ts';

let pass = 0;
const ok = (cond, label, detail = '') => {
  if (!cond) {
    console.error(`  FAIL  ${label}${detail ? ' -- ' + detail : ''}`);
    process.exitCode = 1;
    return;
  }
  pass += 1;
  console.log(`  ok  ${label}${detail ? ' -- ' + detail : ''}`);
};

console.log('isFreeModel：tag 判据');
{
  ok(isFreeModel({id: 'glm-4.6', tags: ['限时免费']}) === true,
     'tags 含「限时免费」 → 免费');
  ok(isFreeModel({id: 'glm-4.6', tags: ['免费']}) === true, 'tags 含「免费」 → 免费');
  ok(isFreeModel({id: 'glm-4.6', tags: ['免费体验']}) === true, '「免费」作为子串也认');
  ok(isFreeModel({id: 'glm-4.6', tags: ['付费', '多模态']}) === false,
     '没有「免费」字样 → 不免费');
  ok(isFreeModel({id: 'glm-4.6', tags: []}) === false, '空 tags → 不免费');
  ok(isFreeModel({id: 'glm-4.6'}) === false, '缺 tags → 不免费');
}

console.log('isFreeModel：id 后缀判据（issue #446 的根因）');
{
  // 这条就是 issue 里那个模型：腾讯目录没给它打「免费」tag，只在 id 里体现
  ok(isFreeModel({id: 'hy4flash-free', tags: []}) === true,
     'hy4flash-free 无 tag 也判免费（issue #446）');
  ok(isFreeModel({id: 'hy4flash-free'}) === true, '连 tags 字段都没有也认');
  ok(isFreeModel({id: 'glm-4-free', tags: []}) === true, '其它 -free 后缀同样认');
  ok(isFreeModel({id: 'GLM-FREE', tags: []}) === true, '大小写不敏感');
  ok(isFreeModel({id: 'deepseek-v4.1-flash', tags: []}) === false,
     'flash ≠ free（别把 -flash 误判成免费）');
  ok(isFreeModel({id: 'hy4flash', tags: []}) === false, '不带 -free 后缀不认');
  ok(isFreeModel({id: 'free-tier-plus', tags: []}) === false,
     'free 在中间不认（只认后缀，避免误伤）');
}

console.log('isFreeModel：脏输入不炸');
{
  ok(isFreeModel(null) === false, 'null → false');
  ok(isFreeModel(undefined) === false, 'undefined → false');
  ok(isFreeModel({id: '', tags: []}) === false, '空 id → false');
  ok(isFreeModel({id: 'x', tags: null}) === false, 'tags=null → false');
  ok(isFreeModel({id: 'x', tags: [null, 3, '免费']}) === true,
     'tags 里混了脏值仍能认出「免费」');
}

console.log('isFreeModel：付费模型绝不能被误判');
{
  const paid = [
    {id: 'deepseek-v4.1-flash', tags: ['多模态']},
    {id: 'glm-4.6', tags: ['长上下文']},
    {id: 'kimi-k2', tags: []},
    {id: 'gpt-5', tags: ['reasoning']},
  ];
  ok(paid.every((m) => !isFreeModel(m)), '一组付费模型全部判为非免费');
}

if (process.exitCode) {
  console.error('\n有断言未通过');
} else {
  console.log(`\nall passed（${pass} 条）`);
}
