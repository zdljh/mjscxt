// ============================================
// i18n 模块（TypeScript）
// ============================================
import { i18nApi } from '../api/client';

let currentLang = 'zh-CN';
let messages: Record<string, string> = {};
// 语言包版本号：每次 loadLocale 成功都会 +1。
// 用途：t() 本身是模块级函数、引用永不变化，若组件把 t 放进 useMemo/useCallback 依赖，
// 会在语言包尚未加载时算出「原始 key」并被永久缓存（页面出现 project.create 这种字样）。
// AppContext 依赖本版本号重建 t 的引用，从根上修掉这一类问题。
let localeVersion = 0;

export function getLocaleVersion(): number {
  return localeVersion;
}

export async function loadLocale(lang: string): Promise<void> {
  try {
    const data = await i18nApi.get(lang);
    // Flatten nested messages
    const flat: Record<string, string> = {};
    function flatten(obj: Record<string, unknown>, prefix = '') {
      for (const [key, val] of Object.entries(obj)) {
        const fullKey = prefix ? `${prefix}.${key}` : key;
        if (val && typeof val === 'object' && !Array.isArray(val)) {
          flatten(val as Record<string, unknown>, fullKey);
        } else {
          flat[fullKey] = String(val);
        }
      }
    }
    flatten(data.messages);
    messages = flat;
    currentLang = lang;
    localeVersion += 1;
    document.documentElement.lang = lang;
  } catch (error) {
    console.error('Failed to load locale:', error);
  }
}

export function t(key: string, params?: Record<string, string | number>): string {
  let value = messages[key] || key;
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      value = value.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v));
    });
  }
  return value;
}

export function getCurrentLang(): string {
  return currentLang;
}

export async function switchLang(lang: string): Promise<void> {
  await loadLocale(lang);
  window.location.reload();
}
