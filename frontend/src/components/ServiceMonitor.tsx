import React, { useState, useEffect } from 'react';

interface ServiceStatus {
  flask: 'ok' | 'error' | 'checking';
  comfyui: 'ok' | 'error' | 'checking' | 'unknown';
  lastCheck: string;
}

export function ServiceMonitor() {
  const [status, setStatus] = useState<ServiceStatus>({
    flask: 'checking',
    comfyui: 'unknown',
    lastCheck: '',
  });

  useEffect(() => {
    const checkServices = async () => {
      const now = new Date().toLocaleTimeString('zh-CN');

      // Flask 与 ComfyUI 统一走后端 /api/status 一次查完。
      // 缺陷 D6：此前浏览器直接 fetch http://127.0.0.1:8188/system_stats，
      // 属于跨源请求（Origin=http://127.0.0.1:5000 ≠ Host=127.0.0.1:8188），
      // ComfyUI 会以 "non matching host and origin" 返回 403，且每 30s 刷一次，
      // 在 ComfyUI 日志里形成高频告警。改为由后端代查即可彻底消除。
      try {
        const response = await fetch('/api/status', { signal: AbortSignal.timeout(4000) });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: any = await response.json().catch(() => ({}));
        const comfy = data?.comfyui || {};
        setStatus({
          flask: 'ok',
          comfyui: comfy.error ? 'error' : 'ok',
          lastCheck: now,
        });
      } catch {
        setStatus(prev => ({ ...prev, flask: 'error', lastCheck: now }));
      }
    };

    checkServices();
    const interval = setInterval(checkServices, 30000); // Check every 30 seconds

    return () => clearInterval(interval);
  }, []);

  if (status.flask === 'ok') return null;

  return (
    <div className="fixed bottom-4 right-4 z-dropdown">
      <div className={`flex items-center gap-2 px-4 py-2 rounded-lg shadow-lg ${
        status.flask === 'error' 
          ? 'bg-danger text-white' 
          : 'bg-warning text-white'
      }`}>
        <span className="w-2 h-2 rounded-full bg-surface animate-pulse"></span>
        <span className="text-sm font-medium">
          {status.flask === 'error' ? '服务连接失败' : '检查中...'}
        </span>
        {/* 保留原生：该按钮叠在 danger/warning 实色横幅上，
            ghost 变体的 text-ink-2 / hover:bg-surface-2 会在实色底上失去对比度 */}
        <button
          onClick={() => window.location.reload()}
          className="ml-2 px-2 py-1 bg-surface/20 rounded hover:bg-surface/30 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
        >
          重试
        </button>
      </div>
    </div>
  );
}
