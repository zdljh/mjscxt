import React, { useState } from 'react';
import { useApp } from '@/context/AppContext';

/** 键盘焦点环：与 ui/index.tsx 的 FOCUS_RING 保持一致 */
const FOCUS_RING =
'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

export function Navbar() {
  const { t, lang, setLang } = useApp();
  const [showLangMenu, setShowLangMenu] = useState(false);

  return (
    <nav className="sticky top-0 z-sticky flex items-center justify-between border-b border-line bg-surface/80 px-6 py-3 backdrop-blur-xl">
      <div className="flex items-center gap-3">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand text-sm font-bold text-white shadow-sm">
          漫
        </div>
        <h1 className="text-lg font-bold text-ink-1">
          {t('app.title')}
        </h1>
      </div>

      <div className="flex items-center gap-4">
        {/* Status indicator */}
        <div className="flex items-center gap-2 rounded-full border border-success/20 bg-success-subtle px-3 py-1.5 text-xs text-success-strong">
          <span className="h-2 w-2 animate-pulse rounded-full bg-success" />
          Online
        </div>

        {/* Language selector */}
        <div className="relative">
          <button
            onClick={() => setShowLangMenu(!showLangMenu)}
            className={`flex items-center gap-2 rounded-md border border-brand/20 bg-brand-subtle px-3 py-1.5 text-brand transition-all duration-200 hover:bg-brand/10 hover:shadow-md ${FOCUS_RING}`}
          >
            <span>{lang === 'zh-CN' ? '🇨🇳' : '🇺🇸'}</span>
            <span className="text-sm font-medium">{lang === 'zh-CN' ? '中文' : 'English'}</span>
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
          {showLangMenu && (
            <div className="absolute right-0 top-full z-dropdown mt-2 w-32 overflow-hidden rounded-xl border border-line bg-surface/95 shadow-md backdrop-blur-xl">
              <button
                onClick={() => { setLang('zh-CN'); setShowLangMenu(false); }}
                className={`w-full px-4 py-2.5 text-left text-sm text-ink-1 transition-colors hover:bg-brand-subtle ${FOCUS_RING}`}
              >
                🇨🇳 简体中文
              </button>
              <button
                onClick={() => { setLang('en-US'); setShowLangMenu(false); }}
                className={`w-full px-4 py-2.5 text-left text-sm text-ink-1 transition-colors hover:bg-brand-subtle ${FOCUS_RING}`}
              >
                🇺🇸 English
              </button>
            </div>
          )}
        </div>

        {/* User avatar */}
        <div className={`flex h-8 w-8 cursor-pointer items-center justify-center rounded-full bg-brand text-sm font-medium text-white transition-transform hover:scale-105 hover:shadow-lg ${FOCUS_RING}`}>
          A
        </div>
      </div>
    </nav>
  );
}
