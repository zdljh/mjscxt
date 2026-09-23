import React, { useCallback, useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { projectsApi, tasksApi } from '@/api/client';
import { Card, EmptyState, Badge, Skeleton, ErrorState } from '@/components/ui';
import { BarChart3, ClipboardList, Clapperboard, FolderOpen, Play, ZoomIn } from '@/components/ui/icons';
import type { IconProps } from '@/components/ui/icons';
import type { Project, Task } from '@/types';

/**
 * 统计卡的语义配色：必须写成**完整类名**的静态映射。
 * 原先是 `bg-${stat.color}-100` —— Tailwind 在构建期静态扫描源码，拼出来的类名
 * 永远不会被生成，统计卡底色实际上是失效的（只有圆角生效）。
 */
const STAT_TONE: Record<string, string> = {
  blue: 'bg-info-subtle text-info-strong',
  green: 'bg-success-subtle text-success-strong',
  amber: 'bg-warning-subtle text-warning-strong',
  yellow: 'bg-warning-subtle text-warning-strong',
  red: 'bg-danger-subtle text-danger-strong',
  purple: 'bg-brand-subtle text-brand',
};

/** 任务类型 → 图标（此前是 emoji，与线性图标集观感割裂） */
const TASK_KIND_ICON: Record<string, React.ComponentType<IconProps>> = {
  keyframe: ZoomIn,
  video: Play,
};

export function OverviewPage() {
  const { t } = useApp();
  const [projects, setProjects] = useState<Project[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  // C1（2026-09-23 收口）：原实现只有 `.finally` 没有 `.catch` —— 两个接口任一失败会
  // **静默落到空态**（用户看到「还没有项目 / 暂无任务」，误以为真没数据，且没有重试入口）。
  // 现捕获错误 → 整页错误态 + 可重试；「重试必须能真正重新取数」，故取数抽成 reload。
  const reload = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      const [p, tk] = await Promise.all([
        projectsApi.list().then(d => d.projects || []),
        tasksApi.list().then(d => d.items || []),
      ]);
      setProjects(p);
      setTasks(tk);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  const stats = [
    { label: t('analytics.projects'), value: projects.length, icon: FolderOpen, color: 'blue' },
    { label: t('analytics.tasks'), value: tasks.length, icon: Clapperboard, color: 'purple' },
    { label: t('analytics.kwh'), value: '0.0', icon: BarChart3, color: 'yellow' },
  ];

  const recentTasks = tasks.slice(-5).reverse();

  // 加载态：沿用统计卡 3 列 + 两个内容卡片的形态
  if (loading) {
    return (
      <div className="space-y-6 fade-in" role="status" aria-live="polite" aria-label={t('common.loading')}>
        <Skeleton className="h-8 w-32" />
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-[88px] rounded-lg" />
          ))}
        </div>
        <Skeleton className="h-44 rounded-lg" />
        <Skeleton className="h-44 rounded-lg" />
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="space-y-6 fade-in">
        <ErrorState
          title={t('project.loadingFailed')}
          description={loadError}
          onRetry={reload}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6 fade-in">
      <h2 className="text-2xl font-bold text-ink-1">{t('analytics.title')}</h2>

      {/* Stats grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {stats.map((stat) => {
          const StatIcon = stat.icon;
          return (
            <Card key={stat.label} className="hover:shadow-md transition-shadow">
              <div className="flex items-center gap-4">
                <div className={`w-12 h-12 rounded-xl flex items-center justify-center ${STAT_TONE[stat.color] ?? 'bg-surface-2 text-ink-2'}`}>
                  <StatIcon className="h-6 w-6" />
                </div>
                <div>
                  <p className="text-sm text-ink-2">{stat.label}</p>
                  <p className="text-2xl font-bold text-ink-1">{stat.value}</p>
                </div>
              </div>
            </Card>
          );
        })}
      </div>

      {/* Recent tasks */}
      <Card title={t('tasks.title')}>
        {recentTasks.length === 0 ? (
          <EmptyState icon={<ClipboardList className="h-10 w-10" />} title={t('common.empty')} description={t('tasks.noTasks')} />
        ) : (
          <div className="space-y-3">
            {recentTasks.map((task) => {
              const TaskIcon = TASK_KIND_ICON[task.kind] ?? ClipboardList;
              return (
                <div key={task.id} className="flex items-center justify-between p-3 bg-surface-2 rounded-lg">
                  <div className="flex items-center gap-3">
                    <TaskIcon className="h-5 w-5 text-ink-2" />
                    <div>
                      <p className="font-medium text-ink-1 text-sm">{task.label}</p>
                      <p className="text-xs text-ink-2">{task.project} • {new Date(task.created_at).toLocaleString()}</p>
                    </div>
                  </div>
                  <Badge variant={task.status === 'done' ? 'success' : task.status === 'running' ? 'warning' : task.status === 'failed' ? 'danger' : 'default'}>
                    {t(`mode.${task.status}`)}
                  </Badge>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      {/* Recent projects */}
      <Card title={t('project.listTitle')}>
        {projects.length === 0 ? (
          <EmptyState icon={<FolderOpen className="h-10 w-10" />} title={t('project.noProjects')} description={t('project.uploadFirst')} />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {projects.slice(0, 6).map((proj) => (
              <div
                key={proj.id}
                role="button"
                tabIndex={0}
                onClick={() => { window.location.hash = `/?p=${encodeURIComponent(proj.dir_key || proj.id)}`; }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    window.location.hash = `/?p=${encodeURIComponent(proj.dir_key || proj.id)}`;
                  }
                }}
                className="p-4 border border-line rounded-lg hover:border-brand transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2"
              >
                <div className="flex items-start justify-between mb-2">
                  <h4 className="font-medium text-ink-1">{proj.name}</h4>
                  <Badge variant="info">{proj.episode_count} {t('ep.suffix')}</Badge>
                </div>
                <p className="text-xs text-ink-2">{t('canvas.style')}: {proj.config.style}</p>
                <p className="text-xs text-ink-3 mt-1">{new Date(proj.created_at).toLocaleDateString()}</p>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
