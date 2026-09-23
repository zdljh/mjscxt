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
 * ── 2026-09-23 设计体系收敛 ──
 * theme.extend 里新增的语义色**全部指向 index.css 的 CSS 变量**，
 * 让 token 成为唯一真源：改一处变量即可整站生效，组件层不再写死 hex/rgba。
 *   canvas  → 页面画布     surface / surface-2 → 卡片与次级区块
 *   line    → 描边         ink-1/2/3           → 三级文字
 *   brand   → 品牌主色     accent              → 青蓝强调
 *   action  → 主按钮实心底
 *   success / warning / danger / info → 通用语义色（-subtle 浅底 / -strong 深字）
 *   state-* → 生产状态语义色（pending/running/done/failed/skipped/attention）
 * 圆角 / 阴影 / 字号 / 动效时长同样在此统一，避免各页自带一套数值。
 *
 * ⚠️ 颜色统一写成 `rgb(var(--x) / <alpha-value>)`：
 *    index.css 里的 token 存的是 RGB 三元组，靠 `<alpha-value>` 占位符才能让
 *    `bg-brand/10`、`ring-brand/40` 这类透明度修饰符产出合法 CSS。
 *    写成 `var(--x)` 的话透明度修饰符会静默失效（生成无效声明）。
 * ⚠️ 这里的 borderRadius / fontSize / boxShadow 是**覆盖** Tailwind 默认值：
 *    既有页面用到的 rounded-lg、text-base、shadow-sm 等会随之改变观感，
 *    这是收敛为单一设计语言的预期代价，不是回归。
 *
 * 注：package.json 未声明 "type": "module"，故此处使用 CommonJS 写法，
 * 避免 Node 每次都要重新解析模块类型产生告警。
 */

/** 把「--token」映射成支持透明度修饰符的 Tailwind 颜色 */
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;

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
      colors: {
        canvas: token('bg-canvas'),
        surface: {
          DEFAULT: token('bg-surface'),
          2: token('bg-surface-2'),
        },
        line: {
          DEFAULT: token('border'),
          strong: token('border-strong'),
        },
        ink: {
          1: token('text-primary'),
          2: token('text-secondary'),
          3: token('text-tertiary'),
        },
        brand: {
          DEFAULT: token('brand'),
          hover: token('brand-hover'),
          subtle: token('brand-subtle'),
        },
        accent: {
          DEFAULT: token('accent'),
          subtle: token('accent-subtle'),
        },
        action: token('action'),
        // ⚠️ 数据可视化专用（SVG / 图表分类色），UI 层不要引用，详见 index.css
        viz: {
          rose: token('viz-rose'),
          violet: token('viz-violet'),
          teal: token('viz-teal'),
          amber: token('viz-amber'),
        },
        success: {
          DEFAULT: token('success'),
          subtle: token('success-subtle'),
          strong: token('success-strong'),
        },
        warning: {
          DEFAULT: token('warning'),
          subtle: token('warning-subtle'),
          strong: token('warning-strong'),
        },
        danger: {
          DEFAULT: token('danger'),
          subtle: token('danger-subtle'),
          strong: token('danger-strong'),
        },
        info: {
          DEFAULT: token('info'),
          subtle: token('info-subtle'),
          strong: token('info-strong'),
        },
        state: {
          pending: token('state-pending'),
          'pending-subtle': token('state-pending-subtle'),
          'pending-strong': token('state-pending-strong'),
          running: token('state-running'),
          'running-subtle': token('state-running-subtle'),
          'running-strong': token('state-running-strong'),
          done: token('state-done'),
          'done-subtle': token('state-done-subtle'),
          'done-strong': token('state-done-strong'),
          failed: token('state-failed'),
          'failed-subtle': token('state-failed-subtle'),
          'failed-strong': token('state-failed-strong'),
          skipped: token('state-skipped'),
          'skipped-subtle': token('state-skipped-subtle'),
          'skipped-strong': token('state-skipped-strong'),
          attention: token('state-attention'),
          'attention-subtle': token('state-attention-subtle'),
          'attention-strong': token('state-attention-strong'),
        },
      },
      borderRadius: {
        sm: '6px',
        md: '10px',
        lg: '12px',
        xl: '16px',
      },
      boxShadow: {
        xs: '0 1px 2px rgb(15 23 42 / .04)',
        sm: '0 1px 3px rgb(15 23 42 / .06), 0 1px 2px rgb(15 23 42 / .04)',
        md: '0 4px 16px rgb(15 23 42 / .08)',
        lg: '0 12px 32px rgb(15 23 42 / .12)',
      },
      fontSize: {
        xs: ['12px', '18px'],
        sm: ['13px', '20px'],
        base: ['14px', '22px'],
        lg: ['16px', '24px'],
        xl: ['20px', '28px'],
        '2xl': ['24px', '32px'],
        '3xl': ['30px', '38px'],
      },
      transitionDuration: {
        DEFAULT: '160ms',
      },
      zIndex: {
        sticky: 'var(--z-sticky)',
        dropdown: 'var(--z-dropdown)',
        drawer: 'var(--z-drawer)',
        modal: 'var(--z-modal)',
        toast: 'var(--z-toast)',
        preview: 'var(--z-preview)',
      },
    },
  },
  plugins: [],
};
