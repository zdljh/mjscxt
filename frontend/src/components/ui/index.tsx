import React from 'react';
import { createPortal } from 'react-dom';
import { useModalBehavior } from '@/hooks/useModalBehavior';

// ===================== 基础反馈 =====================

export function Loading({ size = 'md', label }: { size?: 'sm' | 'md' | 'lg'; label?: string }) {
  const sizes = { sm: 'w-4 h-4', md: 'w-8 h-8', lg: 'w-12 h-12' };
  return (
    <div className="flex flex-col items-center justify-center gap-3 p-8" role="status" aria-live="polite">
      <div className={`${sizes[size]} animate-spin rounded-full border-4 border-gray-200 border-t-blue-600 dark:border-gray-700 dark:border-t-blue-500`} />
      {label && <span className="text-sm text-gray-500 dark:text-gray-400">{label}</span>}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: string;
  title: string;
  description?: string;
  /** 空状态最需要「下一步该做什么」，所以留一个操作位而不是只给一句话 */
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      {icon && <div className="text-4xl mb-4" aria-hidden="true">{icon}</div>}
      <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">{title}</h3>
      {description && <p className="text-sm text-gray-500 dark:text-gray-400 max-w-md">{description}</p>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function Badge({ children, variant = 'default' }: { children: React.ReactNode; variant?: 'default' | 'success' | 'warning' | 'danger' | 'info' }) {
  const variants = {
    default: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
    success: 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300',
    warning: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900 dark:text-yellow-300',
    danger: 'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300',
    info: 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300',
  };
  return (
    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${variants[variant]}`}>
      {children}
    </span>
  );
}

export function Card({
  children,
  className = '',
  title,
  action,
  footer,
  /** 内容区留白：表格/图表类常需要 p-0 或更小的内边距 */
  bodyClassName = 'p-6',
}: {
  children: React.ReactNode;
  className?: string;
  title?: string;
  action?: React.ReactNode;
  footer?: React.ReactNode;
  bodyClassName?: string;
}) {
  return (
    <div className={`bg-white dark:bg-gray-800 rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 ${className}`}>
      {(title || action) && (
        <div className="flex items-center justify-between gap-3 px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          {title && <h3 className="font-semibold text-gray-900 dark:text-white">{title}</h3>}
          {action && <div className="shrink-0">{action}</div>}
        </div>
      )}
      <div className={bodyClassName}>{children}</div>
      {footer && (
        <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700">{footer}</div>
      )}
    </div>
  );
}

// ===================== 表单控件 =====================

export function Button({
  children,
  onClick,
  variant = 'primary',
  size = 'md',
  disabled = false,
  className = '',
  style,
  type = 'button',
  loading = false,
  title,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
  disabled?: boolean;
  className?: string;
  style?: React.CSSProperties;
  type?: 'button' | 'submit' | 'reset';
  /** 提交中：自动禁用并显示转圈，避免重复点击造成重复任务 */
  loading?: boolean;
  title?: string;
}) {
  const base = 'inline-flex items-center justify-center gap-2 font-medium rounded-lg transition-all duration-200 focus:outline-none focus:ring-2 focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed';
  const variants = {
    primary: 'bg-blue-600 text-white hover:bg-blue-700 focus:ring-blue-500',
    secondary: 'bg-gray-200 text-gray-800 hover:bg-gray-300 focus:ring-gray-400 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600',
    danger: 'bg-red-600 text-white hover:bg-red-700 focus:ring-red-500',
    ghost: 'bg-transparent text-gray-600 hover:bg-gray-100 focus:ring-gray-400 dark:text-gray-300 dark:hover:bg-gray-800',
  };
  const sizes = {
    sm: 'px-3 py-1.5 text-sm',
    md: 'px-4 py-2 text-sm',
    lg: 'px-6 py-3 text-base',
  };
  const spin = { sm: 'w-3 h-3', md: 'w-3.5 h-3.5', lg: 'w-4 h-4' };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled || loading}
      style={style}
      title={title}
      aria-busy={loading || undefined}
      className={`${base} ${variants[variant]} ${sizes[size]} ${className}`}
    >
      {loading && (
        <span
          className={`${spin[size]} animate-spin rounded-full border-2 border-current border-t-transparent`}
          aria-hidden="true"
        />
      )}
      {children}
    </button>
  );
}

export function Input({
  value,
  onChange,
  placeholder,
  type = 'text',
  className = '',
  disabled = false,
  label,
  onEnter,
  autoFocus,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  className?: string;
  disabled?: boolean;
  label?: string;
  onEnter?: () => void;
  autoFocus?: boolean;
}) {
  const input = (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      autoFocus={autoFocus}
      onKeyDown={onEnter ? (e) => { if (e.key === 'Enter') onEnter(); } : undefined}
      className={`w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent disabled:opacity-50 dark:bg-gray-800 dark:border-gray-600 dark:text-white ${className}`}
    />
  );
  if (!label) return input;
  return (
    <label className="block">
      <span className="mb-1 block text-sm text-gray-600 dark:text-gray-300">{label}</span>
      {input}
    </label>
  );
}

export function Textarea({
  value,
  onChange,
  rows = 6,
  placeholder,
  className = '',
  label,
  mono = false,
}: {
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  placeholder?: string;
  className?: string;
  label?: string;
  mono?: boolean;
}) {
  const area = (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={rows}
      placeholder={placeholder}
      className={`w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent dark:bg-gray-800 dark:border-gray-600 dark:text-white resize-y ${mono ? 'font-mono text-xs' : ''} ${className}`}
    />
  );
  if (!label) return area;
  return (
    <label className="block">
      <span className="mb-1 block text-sm text-gray-600 dark:text-gray-300">{label}</span>
      {area}
    </label>
  );
}

// ===================== 弹窗 =====================

const MODAL_SIZES = {
  sm: 'max-w-sm',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
  full: 'max-w-6xl',
};
export type ModalSize = keyof typeof MODAL_SIZES;

/**
 * 通用弹窗。
 *
 * ⚠️ 用 Portal 渲染到 document.body：只要祖先元素带有任何 transform / filter /
 * backdrop-filter（例如带入场动画的 .fade-in 页面根节点），position: fixed 就会
 * 以那个祖先为包含块，弹窗会被定位到超长页面的垂直中间 —— 也就是屏幕之外。
 * 挂到 body 上可彻底免疫这个问题。
 *
 * 本轮补齐（此前缺失导致的体验问题）：
 * - ESC 关闭、点遮罩关闭可关（closeOnBackdrop）
 * - 打开时**锁定背景滚动**（此前弹窗打开还能滚后面的长页面）
 * - **焦点陷阱 + 初始焦点 + 关闭后归还焦点**（键盘/读屏用户此前会迷路）
 * - role="dialog" / aria-modal / aria-labelledby；关闭按钮 aria-label
 * - size 档位（此前所有弹窗都写死 max-w-lg，长内容弹窗挤成一条）
 * - footer 操作区；入场动画
 */
export function Modal({
  isOpen,
  onClose,
  title,
  children,
  size = 'md',
  footer,
  closeOnBackdrop = true,
  closeOnEsc = true,
  preventClose = false,
  description,
}: {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  size?: ModalSize;
  footer?: React.ReactNode;
  /** 误点遮罩就丢掉填写内容很恼人，表单类弹窗可传 false */
  closeOnBackdrop?: boolean;
  /** ESC 是否关闭。⚠️ 表单类弹窗挡了遮罩却留 ESC，输入照样会丢，两个要一起关 */
  closeOnEsc?: boolean;
  /** 提交中禁止关闭 */
  preventClose?: boolean;
  description?: string;
}) {
  const titleId = React.useId();
  const descId = React.useId();
  const containerRef = useModalBehavior({ isOpen, onClose, preventClose, closeOnEsc });

  if (!isOpen) return null;

  const node = (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-sm animate-modal-backdrop"
        onClick={closeOnBackdrop && !preventClose ? onClose : undefined}
        aria-hidden="true"
      />
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        className={`relative flex w-full ${MODAL_SIZES[size]} max-h-[90vh] flex-col bg-white dark:bg-gray-800 rounded-2xl shadow-xl animate-modal-in`}
      >
        <div className="flex shrink-0 items-start justify-between gap-3 px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="min-w-0">
            <h2 id={titleId} className="text-lg font-semibold text-gray-900 dark:text-white break-words">{title}</h2>
            {description && (
              <p id={descId} className="mt-1 text-sm text-gray-500 dark:text-gray-400">{description}</p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={preventClose}
            aria-label="关闭"
            className="shrink-0 rounded-lg p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 disabled:opacity-40 dark:hover:bg-gray-700 dark:hover:text-gray-300"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-6">{children}</div>
        {footer && (
          <div className="flex shrink-0 items-center justify-end gap-2 px-6 py-4 border-t border-gray-200 dark:border-gray-700">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
  return typeof document === 'undefined' ? node : createPortal(node, document.body);
}

/**
 * 确认弹窗 —— 替代原生 `window.confirm()`
 *
 * 原生 confirm 的问题：阻塞主线程、样式无法定制（与深色主题完全脱节）、
 * 无法表达「危险操作」语义、不能显示处理中状态。项目里原本有 3 处在用。
 */
export function ConfirmDialog({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  confirmText = '确定',
  cancelText = '取消',
  danger = false,
  loading = false,
}: {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void | Promise<void>;
  title: string;
  message?: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
  loading?: boolean;
}) {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={title}
      size="sm"
      preventClose={loading}
      closeOnBackdrop={!danger}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={loading}>{cancelText}</Button>
          <Button
            variant={danger ? 'danger' : 'primary'}
            onClick={onConfirm}
            loading={loading}
            className="min-w-[5rem]"
          >
            {confirmText}
          </Button>
        </>
      }
    >
      {message ? (
        <div className="text-sm leading-6 text-gray-600 dark:text-gray-300">{message}</div>
      ) : null}
    </Modal>
  );
}
