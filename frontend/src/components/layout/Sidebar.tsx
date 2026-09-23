import React from 'react';
import { useApp } from '@/context/AppContext';
import { Brain, FolderOpen, Settings } from '@/components/ui/icons';

interface NavItem {
  id: string;
  icon: React.ReactNode;
  label: string;
}

/** 键盘焦点环：与 ui/index.tsx 的 FOCUS_RING 保持一致 */
const FOCUS_RING =
'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

// 侧边栏只放「全局」入口：
// - 项目中心：项目列表 + 新建项目（上传小说已并入新建项目）
// - AI 配置 / AI 记忆：与具体项目无关的全局设置
// 单个项目的功能（关键帧/分镜/配音/混音/质检/导出/AI总控）都在项目工作台内部的标签页里。
const navItems: NavItem[] = [
  { id: 'projects', icon: <FolderOpen className="h-5 w-5" />, label: 'project.title' },
  { id: 'ai', icon: <Settings className="h-5 w-5" />, label: 'navAIConfig' },
  { id: 'memory', icon: <Brain className="h-5 w-5" />, label: 'navMemory' },
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
    <aside
      className={`relative z-sticky flex flex-col border-r border-line bg-surface transition-all duration-300 ${
        collapsed ? 'w-16' : 'w-16 md:w-56'
      }`}
    >
      {/* Toggle button：窄屏下侧边栏恒为图标态（宽 4rem），折叠/展开无意义，故隐藏 */}
      <button
        onClick={onToggle}
        aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'}
        className={`m-2 hidden self-end rounded-lg p-2 text-ink-2 transition-all duration-200 hover:bg-surface-2 hover:text-ink-1 md:block ${FOCUS_RING}`}
      >
        <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>

      {/* Logo：窄屏（图标态）不显示文字品牌块 */}
      {!collapsed && (
        <div className="hidden px-4 py-4 mb-2 md:block">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand text-sm font-bold text-white">
              漫
            </div>
            <span className="text-sm font-semibold text-ink-1">
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
              aria-current={isActive ? 'page' : undefined}
              aria-label={t(item.label)}
              className={`mb-1 flex w-full items-center gap-3 rounded-lg px-3 py-2.5 transition-all duration-200 group ${
                isActive
                  ? 'bg-brand-subtle text-brand'
                  : 'text-ink-2 hover:bg-surface-2 hover:text-ink-1'
              } ${FOCUS_RING}`}
              title={collapsed ? t(item.label) : undefined}
            >
              <span className="flex-shrink-0">{item.icon}</span>
              {!collapsed && (
                <span className="hidden whitespace-nowrap text-sm font-medium md:inline">
                  {t(item.label)}
                </span>
              )}
              {isActive && !collapsed && (
                <div className="ml-auto hidden h-1.5 w-1.5 animate-pulse rounded-full bg-brand md:block" />
              )}
            </button>
          );
        })}
      </nav>

      {/* Footer */}
      <div className="border-t border-line p-4">
        {!collapsed && (
          <p className="hidden text-center text-xs text-ink-3 md:block">
            v1.0.0 • AI Powered
          </p>
        )}
      </div>
    </aside>
  );
}
