import React from 'react';

// ==========================================================================
// 线性图标集（Lucide 风格，零依赖手写）
// --------------------------------------------------------------------------
// 为什么不用 lucide-react：项目未装该依赖，且 Electron 离线打包对新增依赖敏感；
// 需要的图标只有十几个，手写 SVG 更安全也更轻。
// 统一约定：24x24 视窗、stroke=currentColor、粗细 1.75、圆头圆角 —— 保证全站
// 线条观感一致（此前是 emoji 与 SVG 混用，观感割裂）。
// ⚠️ 颜色一律继承 currentColor，不要在图标里写死色值。
// ==========================================================================

export type IconProps = {
  className?: string;
  /** 未传 className 时的默认尺寸 */
  size?: number;
  strokeWidth?: number;
};

function Svg({ children, className, size, strokeWidth = 1.75 }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

/** 概览 / 统计 */
export const BarChart3 = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 3v18h18" />
    <path d="M7 16V10M12 16V6M17 16v-4" />
  </Svg>
);

/** 分镜 / 影片 */
export const Clapperboard = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20.2 6 3 11l-.9-2.4A1.5 1.5 0 0 1 3.5 6.6l15.3-4.2a1.5 1.5 0 0 1 1.8 1.1z" />
    <path d="M3 11h18v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
    <path d="m7.5 11 5.3 10M16.5 11l-5.3 10" />
  </Svg>
);

/** 质检 / 通过 */
export const CheckCircle2 = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="m8.5 12.5 2.5 2.5 4.5-5" />
  </Svg>
);

/** 超分 / 放大查看 */
export const ZoomIn = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5M11 8v6M8 11h6" />
  </Svg>
);

/** 关系图 */
export const Network = (p: IconProps) => (
  <Svg {...p}>
    <rect x="9" y="2" width="6" height="6" rx="1.5" />
    <rect x="2" y="16" width="6" height="6" rx="1.5" />
    <rect x="16" y="16" width="6" height="6" rx="1.5" />
    <path d="M12 8v4M9 12H5v4M15 12h4v4" />
  </Svg>
);

/** 音频 */
export const Music = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9 18V5l11-2v13" />
    <circle cx="6" cy="18" r="3" />
    <circle cx="17" cy="16" r="3" />
  </Svg>
);

/** 输出 / 导出 */
export const Share2 = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="18" cy="5" r="3" />
    <circle cx="6" cy="12" r="3" />
    <circle cx="18" cy="19" r="3" />
    <path d="m8.6 10.6 6.8-4M8.6 13.4l6.8 4" />
  </Svg>
);

/** 警告 / 异常 */
export const AlertTriangle = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
    <path d="M12 9v4M12 17h.01" />
  </Svg>
);

/** 空列表 / 任务 */
export const ClipboardList = (p: IconProps) => (
  <Svg {...p}>
    <rect x="8" y="2" width="8" height="4" rx="1" />
    <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
    <path d="M8 11h8M8 15h5" />
  </Svg>
);

/** 项目 / 文件夹 */
export const FolderOpen = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 19V6a2 2 0 0 1 2-2h4l2 3h6a2 2 0 0 1 2 2v3" />
    <path d="M3 19l2.5-7.5A2 2 0 0 1 7.4 10h12.2a1 1 0 0 1 .95 1.32L19 19a2 2 0 0 1-2 1H5a2 2 0 0 1-2-1Z" />
  </Svg>
);

/** 素材库 */
export const BookOpen = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 7v14" />
    <path d="M3 18a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v1a2 2 0 0 0 2 2H7a4 4 0 0 1-4-4Z" />
    <path d="M21 18a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v1a2 2 0 0 1-2 2h6a4 4 0 0 0 4-4Z" />
    <path d="M6 11a7 7 0 0 1 6-7 7 7 0 0 1 6 7" />
  </Svg>
);

/** 记忆 / 知识 */
export const Brain = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 5a3 3 0 0 1 5.2-1.6A3.5 3.5 0 0 1 20.5 8a3 3 0 0 1 .5 4.5V20a2 2 0 0 1-2 2h-3l-2-3h-3l-2 3H6a2 2 0 0 1-2-2v-7.5A3 3 0 0 1 4.5 8a3.5 3.5 0 0 1 3.3-4.6A3 3 0 0 1 12 5Z" />
    <path d="M12 5v17" />
  </Svg>
);

/** 密钥 / 保险库 */
export const KeyRound = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="7.5" cy="15.5" r="4.5" />
    <path d="m10.7 12.3 8.3-8.3M19 4l2 2M17 7l2 2" />
  </Svg>
);

/** 搜索 */
export const Search = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </Svg>
);

/** 播放 */
export const Play = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 4.5v15l13-7.5z" />
  </Svg>
);

/** 下载 */
export const Download = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3v12M7 11l5 5 5-5" />
    <path d="M4 20h16" />
  </Svg>
);

/** 重试 */
export const RefreshCw = (p: IconProps) => (
  <Svg {...p}>
    <path d="M21 12a9 9 0 1 1-3.2-6.9" />
    <path d="M21 4v5h-5" />
  </Svg>
);

/** 新增 */
export const Plus = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 5v14M5 12h14" />
  </Svg>
);

/** 展开/收起箭头 */
export const ChevronRight = (p: IconProps) => (
  <Svg {...p}>
    <path d="m9 6 6 6-6 6" />
  </Svg>
);

export const ChevronDown = (p: IconProps) => (
  <Svg {...p}>
    <path d="m6 9 6 6 6-6" />
  </Svg>
);

/** 时钟 / 耗时 */
export const Clock = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3.5 2" />
  </Svg>
);

/** 关闭 */
export const X = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 6l12 12M18 6 6 18" />
  </Svg>
);
