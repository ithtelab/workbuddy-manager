/**
 * 部署基础路径（basePath）。
 *
 * 默认部署在域名根路径，此时该值为空串，所有路径与改动前完全一致。
 * 需要挂到子路径时（如 `https://example.com/workbuddy-manager/`），在**构建前端
 * 产物时**设置 `NEXT_PUBLIC_BASE_PATH=/workbuddy-manager`，并让反向代理把该前缀
 * 剥掉再转发给本服务（见 deploy/README.md「子路径部署」）。
 *
 * 为什么不能只依赖 Next.js 的 `basePath`：它只自动改写 `next/link`、
 * `next/router`、`next/navigation` 的跳转与静态资源引用，**管不到**下面这些：
 *
 *   - `window.location.href = '/login'` 这类裸字符串赋值（浏览器原生跳转）
 *   - 原生 `fetch('/api/...')`
 *   - 原生 `<a href="/dashboard">`
 *   - axios 的 `baseURL`
 *
 * 漏掉任意一处，子路径部署下都会跳到域名根路径，而根路径通常属于另一个站点，
 * 于是表现为「点一下就 404」。所以站内路径统一走本模块。
 */
export const BASE_PATH = (process.env.NEXT_PUBLIC_BASE_PATH || '').replace(/\/+$/, '');

/**
 * 给站内绝对路径补上 basePath。
 *
 * 外部链接（`https://...`）、协议相对地址（`//cdn...`）、锚点、相对路径原样返回；
 * 已经带前缀的不重复叠加，因此对同一路径重复调用是安全的。
 */
export function withBasePath(path: string): string {
  // `//` 开头是协议相对地址，指向另一个主机，不能加前缀
  if (!BASE_PATH || !path.startsWith('/') || path.startsWith('//')) return path;
  if (path === BASE_PATH || path.startsWith(`${BASE_PATH}/`)) return path;
  return `${BASE_PATH}${path}`;
}
