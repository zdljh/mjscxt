import React, { useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { projectsApi, novelsApi, tasksApi } from '@/api/client';
import { Card, Loading, EmptyState, Badge } from '@/components/ui';
import type { Project, Task } from '@/types';

export function OverviewPage() {
  const { t } = useApp();
  const [projects, setProjects] = useState<Project[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      projectsApi.list().then(d => setProjects(d.projects || [])),
      tasksApi.list().then(d => setTasks(d.items || [])),
    ]).finally(() => setLoading(false));
  }, []);

  const stats = [
    { label: t('analytics.projects'), value: projects.length, icon: '📁', color: 'blue' },
    { label: t('analytics.tasks'), value: tasks.length, icon: '⚡', color: 'purple' },
    { label: t('analytics.kwh'), value: '0.0', icon: '⚡', color: 'yellow' },
  ];

  const recentTasks = tasks.slice(-5).reverse();

  if (loading) return <Loading />;

  return (
    <div className="space-y-6 fade-in">
      <h2 className="text-2xl font-bold text-gray-900 dark:text-white">{t('analytics.title')}</h2>

      {/* Stats grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {stats.map((stat) => (
          <Card key={stat.label} className="hover:shadow-md transition-shadow">
            <div className="flex items-center gap-4">
              <div className={`w-12 h-12 rounded-xl flex items-center justify-center text-2xl bg-${stat.color}-100 dark:bg-${stat.color}-900/20`}>
                {stat.icon}
              </div>
              <div>
                <p className="text-sm text-gray-500 dark:text-gray-400">{stat.label}</p>
                <p className="text-2xl font-bold text-gray-900 dark:text-white">{stat.value}</p>
              </div>
            </div>
          </Card>
        ))}
      </div>

      {/* Recent tasks */}
      <Card title={t('tasks.title')}>
        {recentTasks.length === 0 ? (
          <EmptyState icon="📋" title={t('common.empty')} description={t('tasks.noTasks')} />
        ) : (
          <div className="space-y-3">
            {recentTasks.map((task) => (
              <div key={task.id} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-800/50 rounded-lg">
                <div className="flex items-center gap-3">
                  <span className="text-lg">{task.kind === 'keyframe' ? '🎨' : task.kind === 'video' ? '🎬' : '📋'}</span>
                  <div>
                    <p className="font-medium text-gray-900 dark:text-white text-sm">{task.label}</p>
                    <p className="text-xs text-gray-500 dark:text-gray-400">{task.project} • {new Date(task.created_at).toLocaleString()}</p>
                  </div>
                </div>
                <Badge variant={task.status === 'done' ? 'success' : task.status === 'running' ? 'warning' : task.status === 'failed' ? 'danger' : 'default'}>
                  {t(`mode.${task.status}`)}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Recent projects */}
      <Card title={t('project.listTitle')}>
        {projects.length === 0 ? (
          <EmptyState icon="📁" title={t('project.noProjects')} description={t('project.uploadFirst')} />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {projects.slice(0, 6).map((proj) => (
              <div
                key={proj.id}
                onClick={() => { window.location.hash = `/?p=${encodeURIComponent(proj.dir_key || proj.id)}`; }}
                className="p-4 border border-gray-200 dark:border-gray-700 rounded-lg hover:border-blue-500 dark:hover:border-blue-400 transition-colors cursor-pointer"
              >
                <div className="flex items-start justify-between mb-2">
                  <h4 className="font-medium text-gray-900 dark:text-white">{proj.name}</h4>
                  <Badge variant="info">{proj.episode_count} {t('ep.suffix')}</Badge>
                </div>
                <p className="text-xs text-gray-500 dark:text-gray-400">{t('canvas.style')}: {proj.config.style}</p>
                <p className="text-xs text-gray-400 mt-1">{new Date(proj.created_at).toLocaleDateString()}</p>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
