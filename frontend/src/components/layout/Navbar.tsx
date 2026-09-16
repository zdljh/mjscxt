import React, { useState } from 'react';
import { useApp } from '@/context/AppContext';

export function Navbar() {
  const { t, lang, setLang } = useApp();
  const [showLangMenu, setShowLangMenu] = useState(false);

  return (
    <nav className="relative px-6 py-3 flex items-center justify-between sticky top-0 z-40"
      style={{
        background: 'rgba(255,255,255,0.8)',
        backdropFilter: 'blur(20px)',
        borderBottom: '1px solid rgba(99,102,241,0.15)',
      }}
    >
      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-lg flex items-center justify-center text-white font-bold text-sm"
          style={{ background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)', boxShadow: '0 4px 12px rgba(102,126,234,0.4)' }}>
          漫
        </div>
        <h1 className="text-lg font-bold" style={{ background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
          {t('app.title')}
        </h1>
      </div>

      <div className="flex items-center gap-4">
        {/* Status indicator */}
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs"
          style={{ background: 'rgba(16,185,129,0.1)', color: '#10b981', border: '1px solid rgba(16,185,129,0.2)' }}>
          <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
          Online
        </div>

        {/* Language selector */}
        <div className="relative">
          <button
            onClick={() => setShowLangMenu(!showLangMenu)}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg transition-all duration-200 hover:shadow-md"
            style={{ background: 'rgba(99,102,241,0.1)', border: '1px solid rgba(99,102,241,0.2)', color: '#6366f1' }}
          >
            <span>{lang === 'zh-CN' ? '🇨🇳' : '🇺🇸'}</span>
            <span className="text-sm font-medium">{lang === 'zh-CN' ? '中文' : 'English'}</span>
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
          {showLangMenu && (
            <div className="absolute right-0 top-full mt-2 w-32 rounded-xl shadow-xl overflow-hidden z-50"
              style={{ background: 'rgba(255,255,255,0.95)', backdropFilter: 'blur(20px)', border: '1px solid rgba(99,102,241,0.2)' }}>
              <button
                onClick={() => { setLang('zh-CN'); setShowLangMenu(false); }}
                className="w-full text-left px-4 py-2.5 text-sm hover:bg-indigo-50 transition-colors"
                style={{ color: '#374151' }}
              >
                🇨🇳 简体中文
              </button>
              <button
                onClick={() => { setLang('en-US'); setShowLangMenu(false); }}
                className="w-full text-left px-4 py-2.5 text-sm hover:bg-indigo-50 transition-colors"
                style={{ color: '#374151' }}
              >
                🇺🇸 English
              </button>
            </div>
          )}
        </div>

        {/* User avatar */}
        <div className="w-8 h-8 rounded-full flex items-center justify-center text-white text-sm font-medium cursor-pointer transition-all hover:shadow-lg hover:scale-105"
          style={{ background: 'linear-gradient(135deg, #10b981 0%, #3b82f6 100%)' }}>
          A
        </div>
      </div>
    </nav>
  );
}
