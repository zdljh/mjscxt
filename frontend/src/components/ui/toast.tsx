import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

/**
 * 全局轻提示（Toast）
 *
 * 为什么需要：此前项目**没有任何全局反馈通道** ——
 * `AppContext` 虽然导出了 `showError`，但既没人调用、也没人渲染 `error`，
 * 属于死 API；页面只能用原生 `alert()`（阻塞、样式与应用不一致）或
 * `console.error`（用户根本看不到）。结果就是「点了按钮没反应」这类投诉。
 *
 * 设计要点：
 * - 多条堆叠（右上角），最新的在上方；
 * - 自动消失，error 停留更久（需要用户看清）；
 * - `aria-live` 让读屏软件能播报；
 * - 挂到 document.body：祖先带 transform/filter 时 fixed 会失效（与 Modal 同理）。
 */

export type ToastType = 'success' | 'error' | 'info' | 'warning';

export interface ToastItem {
  id: number;
  type: ToastType;
  message: string;
  /** 毫秒；0 表示不自动关闭 */
  duration?: number;
  /** 可选的操作按钮（如「查看详情」「重试」） */
  action?: { label: string; onClick: () => void };
}

interface ToastContextType {
  push: (message: string, type?: ToastType, options?: Omit<ToastItem, 'id' | 'message' | 'type'>) => number;
  success: (message: string, options?: Omit<ToastItem, 'id' | 'message' | 'type'>) => number;
  error: (message: string, options?: Omit<ToastItem, 'id' | 'message' | 'type'>) => number;
  info: (message: string, options?: Omit<ToastItem, 'id' | 'message' | 'type'>) => number;
  warning: (message: string, options?: Omit<ToastItem, 'id' | 'message' | 'type'>) => number;
  dismiss: (id: number) => void;
  clear: () => void;
}

const ToastContext = createContext<ToastContextType>({
  push: () => 0,
  success: () => 0,
  error: () => 0,
  info: () => 0,
  warning: () => 0,
  dismiss: () => {},
  clear: () => {},
});

/** 默认停留时长：错误给更长时间，避免用户还没读完就消失 */
const DEFAULT_DURATION: Record<ToastType, number> = {
  success: 3000,
  info: 3500,
  warning: 5000,
  error: 6500,
};

const STYLES: Record<ToastType, { wrap: string; icon: string; glyph: string }> = {
  success: {
    wrap: 'border-success/30 bg-success-subtle text-success-strong',
    icon: 'bg-success',
    glyph: 'M5 13l4 4L19 7',
  },
  error: {
    wrap: 'border-danger/30 bg-danger-subtle text-danger-strong',
    icon: 'bg-danger',
    glyph: 'M6 18L18 6M6 6l12 12',
  },
  warning: {
    wrap: 'border-warning/30 bg-warning-subtle text-warning-strong',
    icon: 'bg-warning',
    glyph: 'M12 9v4m0 4h.01M12 3l9 16H3l9-16z',
  },
  info: {
    wrap: 'border-info/30 bg-info-subtle text-info-strong',
    icon: 'bg-brand',
    glyph: 'M12 8h.01M11 12h1v5h1',
  },
};

export function ToastProvider({ children, max = 5 }: { children: React.ReactNode; max?: number }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const timers = useRef<Map<number, number>>(new Map());

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
    const t = timers.current.get(id);
    if (t) {
      window.clearTimeout(t);
      timers.current.delete(id);
    }
  }, []);

  const push = useCallback<ToastContextType['push']>((message, type = 'info', options) => {
    const text = String(message ?? '').trim();
    if (!text) return 0;
    seq.current += 1;
    const id = seq.current;
    const duration = options?.duration ?? DEFAULT_DURATION[type];
    const item: ToastItem = { id, type, message: text, duration, action: options?.action };
    setItems((prev) => {
      const next = [item, ...prev];
      // 超出上限时丢掉最旧的（连同它的定时器）
      const overflow = next.slice(max);
      overflow.forEach((o) => {
        const t = timers.current.get(o.id);
        if (t) {
          window.clearTimeout(t);
          timers.current.delete(o.id);
        }
      });
      return next.slice(0, max);
    });
    if (duration > 0) {
      const timer = window.setTimeout(() => dismiss(id), duration);
      timers.current.set(id, timer);
    }
    return id;
  }, [dismiss, max]);

  // 卸载时清掉所有定时器，避免对已卸载组件 setState
  useEffect(() => () => {
    timers.current.forEach((t) => window.clearTimeout(t));
    timers.current.clear();
  }, []);

  const value = useMemo<ToastContextType>(() => ({
    push,
    success: (m, o) => push(m, 'success', o),
    error: (m, o) => push(m, 'error', o),
    info: (m, o) => push(m, 'info', o),
    warning: (m, o) => push(m, 'warning', o),
    dismiss,
    clear: () => setItems([]),
  }), [push, dismiss]);

  const host = (
    <div
      className="fixed top-4 right-4 z-toast flex flex-col gap-2 w-[min(92vw,22rem)] pointer-events-none"
      role="region"
      aria-label="通知"
    >
      {items.map((t) => {
        const s = STYLES[t.type];
        return (
          <div
            key={t.id}
            className={`pointer-events-auto flex items-start gap-3 rounded-xl border px-4 py-3 shadow-lg backdrop-blur-sm animate-toast-in ${s.wrap}`}
            role={t.type === 'error' ? 'alert' : 'status'}
            aria-live={t.type === 'error' ? 'assertive' : 'polite'}
          >
            <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${s.icon}`}>
              <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                <path strokeLinecap="round" strokeLinejoin="round" d={s.glyph} />
              </svg>
            </span>
            <p className="flex-1 text-sm leading-5 break-words">{t.message}</p>
            {/* 保留原生：行内链接按钮跟随 toast 的 currentColor 变色，
                Button 的 link 变体会强制 text-brand，在 warning/danger 底上失去对比度 */}
            {t.action && (
              <button
                type="button"
                onClick={() => { t.action?.onClick(); dismiss(t.id); }}
                className="shrink-0 text-sm font-medium underline underline-offset-2 opacity-80 hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
              >
                {t.action.label}
              </button>
            )}
            {/* 保留原生：图标关闭键靠 h-4/w-4 + tiny padding 贴合 20px 行高，
                Button 的最小尺寸是 h-8 + px-3，会把 toast 撑高 */}
            <button
              type="button"
              onClick={() => dismiss(t.id)}
              aria-label="关闭通知"
              className="shrink-0 opacity-50 hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        );
      })}
    </div>
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      {typeof document === 'undefined' ? null : createPortal(host, document.body)}
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}
