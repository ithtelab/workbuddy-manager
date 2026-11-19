/**
 * 「限时免费」判定（issue #446）—— 白名单选择器的免费筛选依赖它。
 *
 * 为什么单独一个模块：这个判据错起来**界面不报错**，只是筛完一条都没有，
 * 用户会以为「筛选坏了」或者「根本没有免费模型」。issue #446 就是这种：
 * 只按目录 `tags` 判，而腾讯的免费模型并非都带 tag —— `hy4flash-free`
 * 只在 id 里体现，于是「只看限时免费」命中 0 条。
 *
 * 两条判据取**并集**：
 *   1. 目录 `tags` 中含「免费」（上游明确标注的）；
 *   2. id 以 `-free` 结尾（按腾讯的命名约定）。
 *
 * 反方向刻意保守：认不出来的一律**不**标免费。把付费模型误标成免费会让用户
 * 白花额度，比漏标严重得多。
 *
 * ⚠️ 与 `account-status.ts` 同规矩：本模块**不得引入任何运行时 import**
 * （只有 `import type`），这样 `web/lib/*.test.mjs` 能用 Node 的
 * type stripping 直接跑源码。
 */
import type {CatalogModel} from './types';

/** 目录条目里表示「免费」的 tag 片段（含「免费」二字即可，兼容「限时免费」等写法）。 */
const FREE_TAG_MARK = '免费';

/** id 后缀约定：`hy4flash-free`、`glm-4-free` 这类。 */
const FREE_ID_SUFFIX = /-free$/i;

/**
 * 该模型是否属于「限时免费」。
 *
 * 接受最小形状而不是完整 `CatalogModel`：测试与将来别处复用都不必凑齐
 * 十几个字段，也让这个模块不依赖目录结构。
 */
export function isFreeModel(model: Pick<CatalogModel, 'id' | 'tags'> | null | undefined): boolean {
  if (!model) return false;
  const tags = model.tags;
  if (Array.isArray(tags) && tags.some((tag) => typeof tag === 'string' && tag.includes(FREE_TAG_MARK))) {
    return true;
  }
  return typeof model.id === 'string' && FREE_ID_SUFFIX.test(model.id);
}
