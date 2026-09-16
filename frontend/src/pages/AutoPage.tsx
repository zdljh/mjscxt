import React, { useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { projectsApi } from '@/api/client';
import { Card, Button, Loading } from '@/components/ui';
import type { Project } from '@/types';

export function AutoPage() {
  const { t } = useApp();
  const [projects, setProjects] = useState<Project[]>([]);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    projectsApi.list()
      .then(d => setProjects(d.projects || []))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  const handleStart = async (proj: Project) => {
    setRunningId(proj.id);
    setError(null);
    try {
      // 调用自动生产启动接口
      const resp = await fetch('/api/autonomous/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_name: proj.dir_key || proj.id,
          novel_id: proj.novel_id,
          style: proj.config?.style || '3D动漫渲染',
        }),
      });
      const data = await resp.json();
      if (!data.success) {
        setError(data.error || '启动失败');
      }
    } catch (e) {
      console.error('Start failed:', e);
      setError((e as Error).message);
    } finally {
      setRunningId(null);
    }
  };

  if (loading) return <Loading />;

  return (
    <div className="space-y-6 fade-in">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold text-gray-900 dark:text-white">{t('auto.title')}</h2>
      </div>

      {error && (
        <div className="p-4 bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 rounded-lg">
          {error}
        </div>
      )}

      <Card>
        <div className="text-center py-12">
          <div className="text-6xl mb-4">🤖</div>
          <h3 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">
            {t('auto.subtitle')}
          </h3>
          <p className="text-gray-500 dark:text-gray-400 mb-6 max-w-md mx-auto">
            {t('auto.autoDesc') || '选择项目后点击"启动生产"，系统将自动完成剧本→资产→分镜→视频全流程'}
          </p>
          {projects.length === 0 ? (
            <p className="text-gray-400">{t('project.noProjects')}</p>
          ) : (
            <div className="space-y-3 max-w-lg mx-auto">
              {projects.map((proj) => (
                <div
                  key={proj.id}
                  onClick={() => handleStart(proj)}
                  className="flex items-center justify-between p-4 bg-gray-50 dark:bg-gray-800/50 rounded-lg cursor-pointer hover:border-blue-500 border border-transparent transition-colors"
                >
                  <div>
                    <p className="font-medium text-gray-900 dark:text-white">{proj.name}</p>
                    <p className="text-sm text-gray-500 dark:text-gray-400">
                      {proj.episode_count} {t('ep.suffix')} • {proj.config.style}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    {runningId === proj.id ? (
                      <span className="text-sm text-blue-600 dark:text-blue-400 animate-pulse">
                        {t('mode.running')}
                      </span>
                    ) : (
                      <Button size="sm">{t('auto.start')}</Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}
