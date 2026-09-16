import React, { useState } from 'react';
import { useApp } from '@/context/AppContext';

interface NavItem {
  id: string;
  icon: string;
  label: string;
}

// 侧边栏只放「全局」入口：
// - 项目中心：项目列表 + 新建项目（上传小说已并入新建项目）
// - AI 配置 / AI 记忆：与具体项目无关的全局设置
// 单个项目的功能（关键帧/分镜/配音/混音/质检/导出/AI总控）都在项目工作台内部的标签页里。
const navItems: NavItem[] = [
  { id: 'projects', icon: '📁', label: 'project.title' },
  { id: 'ai', icon: '⚙️', label: 'navAIConfig' },
  { id: 'memory', icon: '🧠', label: 'navMemory' },
];

export function Sidebar({
  activeSection,
  onNavigate,
  collapsed,
  onToggle,
}: {
  activeSection: string;
  onNavigate: (id: string) => void;
  collapsed: boolean;
  onToggle: () => void;
}) {
  const { t } = useApp();

  return (
    <aside className="relative flex flex-col transition-all duration-300 z-50"
      style={{
        width: collapsed ? '4rem' : '14rem',
        background: 'linear-gradient(180deg, rgba(15,23,42,0.95) 0%, rgba(30,27,75,0.95) 100%)',
        backdropFilter: 'blur(20px)',
        borderRight: '1px solid rgba(99,102,234,0.3)',
      }}
    >
      {/* Toggle button */}
      <button
        onClick={onToggle}
        className="m-2 p-2 rounded-lg transition-all duration-200 hover:bg-white/10 text-gray-400 hover:text-white"
        style={{ alignSelf: 'flex-end' }}
      >
        <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>

      {/* Logo */}
      {!collapsed && (
        <div className="px-4 py-4 mb-2">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg flex items-center justify-center text-white font-bold text-sm"
              style={{ background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)' }}>
              漫
            </div>
            <span className="text-white font-semibold text-sm" style={{ background: 'linear-gradient(90deg, #667eea, #764ba2)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
              漫剧工坊
            </span>
          </div>
        </div>
      )}

      {/* Nav items */}
      <nav className="flex-1 overflow-y-auto px-2 pb-4">
        {navItems.map((item) => {
          const isActive = activeSection === item.id;
          return (
            <button
              key={item.id}
              onClick={() => onNavigate(item.id)}
              className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg mb-1 transition-all duration-200 group ${
                isActive
                  ? 'text-white shadow-lg'
                  : 'text-gray-400 hover:bg-white/5 hover:text-gray-200'
              }`}
              style={isActive
                ? { background: 'linear-gradient(135deg, rgba(102,126,234,0.8) 0%, rgba(118,75,162,0.8) 100%)', boxShadow: '0 4px 15px rgba(102,126,234,0.4)' }
                : {}
              }
              title={collapsed ? t(item.label) : undefined}
            >
              <span className="text-xl flex-shrink-0">{item.icon}</span>
              {!collapsed && (
                <span className="text-sm font-medium whitespace-nowrap">
                  {t(item.label)}
                </span>
              )}
              {isActive && !collapsed && (
                <div className="ml-auto w-1.5 h-1.5 rounded-full bg-white animate-pulse" />
              )}
            </button>
          );
        })}
      </nav>

      {/* Footer */}
      <div className="p-4 border-t border-white/10">
        {!collapsed && (
          <p className="text-xs text-gray-500 text-center">
            v1.0.0 • AI Powered
          </p>
        )}
      </div>
    </aside>
  );
}
