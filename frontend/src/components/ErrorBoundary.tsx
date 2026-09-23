import React, { Component, ErrorInfo, ReactNode } from 'react';
import { Button } from '@/components/ui';
import { AlertTriangle } from '@/components/ui/icons';
import { t } from '@/i18n';

// 本组件是 class 组件，不能用 useApp() hook，故使用模块级 t()。
// 语言包由 AppProvider 在 App 层异步加载，ErrorBoundary 的渲染时机可能早于语言包就绪，
// 此时 t() 会回落成 key 本身（i18n/index.ts 的既有设计），属预期行为、不是 bug。

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
    error: null,
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('ErrorBoundary caught an error:', error, errorInfo);
    this.props.onError?.(error, errorInfo);
  }

  public render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }

      return (
        <div className="min-h-screen flex items-center justify-center bg-surface-2">
          <div className="text-center p-8 bg-surface rounded-lg shadow-lg max-w-md mx-4">
            <div className="mb-4 flex justify-center">
              <AlertTriangle className="h-16 w-16 text-danger" />
            </div>
            <h2 className="text-xl font-bold text-ink-1 mb-2">
              {t('error.boundaryTitle')}
            </h2>
            <p className="text-ink-2 mb-4">
              {t('error.boundaryDesc')}
            </p>
            {import.meta.env.DEV && this.state.error && (
              <details className="text-left text-sm text-ink-2 mb-4">
                <summary className="cursor-pointer hover:text-ink-1">
                  {t('error.boundaryDetails')}
                </summary>
                <pre className="mt-2 p-3 bg-surface-2 rounded overflow-auto text-xs">
                  {this.state.error.toString()}
                </pre>
              </details>
            )}
            <div className="flex gap-3 justify-center">
              <Button variant="brand" onClick={() => window.location.reload()}>
                {t('error.reload')}
              </Button>
              <Button
                variant="secondary"
                onClick={() => {
                  window.location.hash = '#/';
                  window.location.reload();
                }}
              >
                {t('error.backHome')}
              </Button>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
