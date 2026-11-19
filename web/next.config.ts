import type {NextConfig} from 'next';

const isExport = process.env.NEXT_OUTPUT_EXPORT === '1';
const backend = process.env.NEXT_PUBLIC_BACKEND_BASE_URL || 'http://127.0.0.1:7864';

// 子路径部署：构建时设 NEXT_PUBLIC_BASE_PATH=/workbuddy-manager。
// 留空即根路径部署（默认），此时不设置 basePath，产物与改动前逐字节一致。
//
// 这里只设 basePath。Next 在 assetPrefix 为空时会自动把它补成 basePath
// （next/dist/server/config.js：`if (result.assetPrefix === '') result.assetPrefix = result.basePath`），
// 而 webpack 的 publicPath 只由 assetPrefix 决定（`${assetPrefix}/_next/`），
// 所以最终是 `/workbuddy-manager/_next/`，不会出现双前缀。
// 也就是说「只设 basePath」与「两者都设成同一个值」产物完全一致，不必特意回避 assetPrefix。
const basePath = (process.env.NEXT_PUBLIC_BASE_PATH || '').replace(/\/+$/, '');

const nextConfig: NextConfig = {
  ...(basePath ? {basePath} : {}),
  ...(isExport ?
    {
      output: 'export' as const,
      trailingSlash: true,
    } :
    {
      async rewrites() {
        return [
          {source: '/api/:path*', destination: `${backend}/api/:path*`},
          {source: '/v1/:path*', destination: `${backend}/v1/:path*`},
          {source: '/v2/:path*', destination: `${backend}/v2/:path*`},
          {source: '/healthz', destination: `${backend}/healthz`},
        ];
      },
    }),
  images: {
    unoptimized: true,
    remotePatterns: [],
  },
  // 构建时间注入为环境变量：package.json 里的 buildDate 是死值（从没更新过），
  // 界面上「Build At」会永远显示同一个日期，属于会误导人的信息。
  // 运行版本另以后端为准，这里只回答「这份前端产物是什么时候构建的」。
  env: {
    NEXT_PUBLIC_BUILD_TIME: new Date().toISOString(),
    // 显式回注规范化后的值（去掉尾部斜杠），保证客户端 lib/base-path.ts 拿到的
    // 与上面 basePath 用的是同一个字符串，不会出现「路由带前缀、跳转不带」的错位。
    NEXT_PUBLIC_BASE_PATH: basePath,
  },
};

export default nextConfig;
