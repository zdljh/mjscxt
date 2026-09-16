import React, { createContext, useContext, useState, useEffect, useCallback, useMemo } from 'react';
import { loadLocale, t, getLocaleVersion } from '@/i18n';

interface AppContextType {
  lang: string;
  setLang: (lang: string) => void;
  t: (key: string, params?: Record<string, string | number>) => string;
  loading: boolean;
  error: string | null;
  showError: (msg: string) => void;
  clearError: () => void;
}

const AppContext = createContext<AppContextType>({
  lang: 'zh-CN',
  setLang: () => {},
  t,
  loading: false,
  error: null,
  showError: () => {},
  clearError: () => {},
});

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = useState('zh-CN');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 语言包版本号 —— 见 i18n/index.ts 的说明。
  // 关键点：t 必须是「随语言包变化而改变引用」的函数，否则组件里
  // useMemo(() => t('x'), [t]) 会在语言包加载前算出原始 key 并永久缓存。
  const [localeVersion, setLocaleVersion] = useState(getLocaleVersion());

  useEffect(() => {
    const browserLang = navigator.language.startsWith('zh') ? 'zh-CN' : 'en-US';
    loadLocale(browserLang).then(() => {
      setLangState(browserLang);
      setLocaleVersion(getLocaleVersion());
      setLoading(false);
    }).catch((e) => {
      console.error('Failed to load locale:', e);
      setLoading(false);
    });
  }, []);

  const setLang = useCallback(async (newLang: string) => {
    await loadLocale(newLang);
    setLangState(newLang);
    setLocaleVersion(getLocaleVersion());
  }, []);

  // 引用随 localeVersion 变化 → 依赖 t 的 useMemo/useCallback 会在语言包就绪后重算
  const translate = useCallback(
    (key: string, params?: Record<string, string | number>) => t(key, params),
    [localeVersion]
  );

  const showError = useCallback((msg: string) => setError(msg), []);
  const clearError = useCallback(() => setError(null), []);

  const value = useMemo(
    () => ({ lang, setLang, t: translate, loading, error, showError, clearError }),
    [lang, setLang, translate, loading, error, showError, clearError]
  );

  return (
    <AppContext.Provider value={value}>
      {children}
    </AppContext.Provider>
  );
}

export function useApp() {
  return useContext(AppContext);
}
