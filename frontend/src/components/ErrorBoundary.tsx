import React, { Component, ErrorInfo, ReactNode } from 'react';
import { Button } from '@/components/ui';

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
            <div className="text-6xl mb-4">⚠️</div>
            <h2 className="text-xl font-bold text-ink-1 mb-2">
              页面出错了
            </h2>
            <p className="text-ink-2 mb-4">
              发生了意外错误，请尝试刷新页面
            </p>
            {import.meta.env.DEV && this.state.error && (
              <details className="text-left text-sm text-ink-2 mb-4">
                <summary className="cursor-pointer hover:text-ink-1">
                  错误详情
                </summary>
                <pre className="mt-2 p-3 bg-surface-2 rounded overflow-auto text-xs">
                  {this.state.error.toString()}
                </pre>
              </details>
            )}
            <div className="flex gap-3 justify-center">
              <Button variant="brand" onClick={() => window.location.reload()}>
                刷新页面
              </Button>
              <Button
                variant="secondary"
                onClick={() => {
                  window.location.hash = '#/';
                  window.location.reload();
                }}
              >
                返回首页
              </Button>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
