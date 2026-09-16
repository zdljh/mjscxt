/** Tailwind 配置（v3，与原先 CDN 版本保持一致，避免 v3→v4 的视觉回归）
 *
 * 背景：此前 index.html 通过 <script src="https://cdn.tailwindcss.com"> 在运行时
 * 现场生成工具类，构建产物里 0 个 Tailwind 类。后果：
 *   1) 断网 / 内网 / Electron 离线打包 → 整个界面失去样式；
 *   2) 每个页面加载都要在浏览器里做一次 JIT 扫描；
 *   3) Tailwind 官方明确禁止在生产环境使用该 CDN。
 * 这里改为构建期编译：postcss + tailwindcss(v3) 产出静态 CSS。
 *
 * 刻意选择 v3 而非原先 devDependencies 里的 v4：本项目有 188 处裸 `border`
 * （v3 默认 gray-200、v4 变成 currentColor）与 27 处 ring/outline-none
 * （v4 已重命名），直接迁 v4 会造成整站视觉回归。v3 能 1:1 复刻既有观感。
 *
 * 注：package.json 未声明 "type": "module"，故此处使用 CommonJS 写法，
 * 避免 Node 每次都要重新解析模块类型产生告警。
 */
module.exports = {
  darkMode: 'class',
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['PingFang SC', 'Microsoft YaHei', 'system-ui', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
