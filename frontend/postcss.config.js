/** PostCSS 配置：把 Tailwind v3 在构建期编译成静态 CSS。
 *  使用 CommonJS 写法与 package.json（无 "type": "module"）保持一致，
 *  避免 Node 提示 "Module type ... is not specified and it doesn't parse as CommonJS"。 */
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
