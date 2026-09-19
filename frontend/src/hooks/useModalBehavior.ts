import { useEffect, useRef } from 'react';

/**
 * 弹窗通用行为（ESC 关闭 / 锁定背景滚动 / 焦点陷阱）
 *
 * 抽成 hook 的原因：项目里有两套弹窗实现 —— `components/ui` 的共享 `<Modal>`
 * 和 `ProjectWorkbenchPage` 里手写的 `AssetPreviewModal`（图片灯箱）。
 * 两边都需要同一套可访问性行为，写成 hook 才不会各改一份、越改越不一致。
 */

// 全局滚动锁计数：多层弹窗叠加时，只有最外层关闭才恢复滚动。
// （若每层各写一次 body.style.overflow，里层关闭就会把外层的锁一起解掉）
let scrollLockCount = 0;
let savedOverflow = '';

function lockBodyScroll() {
  if (typeof document === 'undefined') return;
  if (scrollLockCount === 0) {
    savedOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
  }
  scrollLockCount += 1;
}

function unlockBodyScroll() {
  if (typeof document === 'undefined') return;
  scrollLockCount = Math.max(0, scrollLockCount - 1);
  if (scrollLockCount === 0) {
    document.body.style.overflow = savedOverflow;
  }
}

/** 可聚焦元素的判定（与浏览器 Tab 顺序大致一致） */
const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function useModalBehavior(options: {
  isOpen: boolean;
  onClose: () => void;
  /** 关闭时把焦点还给打开它的元素（默认 true） */
  restoreFocus?: boolean;
  /** 为 true 时禁止一切隐式关闭（例如正在提交，避免误触丢失输入） */
  preventClose?: boolean;
  /** ESC 是否关闭（单独一个开关：表单类弹窗挡遮罩的同时也要挡 ESC，否则输入照样丢） */
  closeOnEsc?: boolean;
}) {
  const { isOpen, onClose, restoreFocus = true, preventClose = false, closeOnEsc = true } = options;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const lastActiveRef = useRef<HTMLElement | null>(null);
  // 用 ref 存最新回调，避免 onClose 换引用就重绑监听
  const onCloseRef = useRef(onClose);
  const preventRef = useRef(preventClose);
  const closeOnEscRef = useRef(closeOnEsc);
  onCloseRef.current = onClose;
  preventRef.current = preventClose;
  closeOnEscRef.current = closeOnEsc;

  useEffect(() => {
    if (!isOpen) return;

    // 记录打开前的焦点元素，关闭时归还（否则焦点会掉到 body，键盘用户失去位置）
    lastActiveRef.current = (typeof document !== 'undefined'
      ? (document.activeElement as HTMLElement | null)
      : null);

    lockBodyScroll();

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (preventRef.current || !closeOnEscRef.current) return;
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab') return;
      // 焦点陷阱：把 Tab 循环限制在弹窗内
      const root = containerRef.current;
      if (!root) return;
      const nodes = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter((el) => el.offsetParent !== null || el === document.activeElement);
      if (nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (active === first || !root.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !root.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown, true);

    // 初始焦点：优先 [data-autofocus]，否则第一个可聚焦元素
    const timer = window.setTimeout(() => {
      const root = containerRef.current;
      if (!root) return;
      const preferred = root.querySelector<HTMLElement>('[data-autofocus]');
      if (preferred) {
        preferred.focus();
        return;
      }
      const nodes = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (nodes.length > 0) nodes[0].focus();
    }, 0);

    return () => {
      document.removeEventListener('keydown', handleKeyDown, true);
      window.clearTimeout(timer);
      unlockBodyScroll();
      if (restoreFocus) {
        const el = lastActiveRef.current;
        if (el && typeof el.focus === 'function' && document.contains(el)) {
          el.focus();
        }
      }
    };
  }, [isOpen, restoreFocus]);

  return containerRef;
}
