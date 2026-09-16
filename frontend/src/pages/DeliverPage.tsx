import React, { useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { projectsApi, autopilotApi } from '@/api/client';
import { Card, Button, Loading, EmptyState } from '@/components/ui';
import type { Project } from '@/types';

export function DeliverPage() {
  const { t } = useApp();
  const [projects, setProjects] = useState<Project[]>([]);
  const [deliverables, setDeliverables] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [downloading, setDownloading] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      projectsApi.list().then(d => setProjects((d as any).projects || [])),
      autopilotApi.deliverables().then(d => setDeliverables((d as any).items || [])).catch(() => []),
    ]).finally(() => setLoading(false));
  }, []);

  const handleDownload = async (proj: Project) => {
    const projectName = proj.dir_key || proj.name;
    setDownloading(projectName);
    try {
      // Try common video file patterns
      const candidates = [
        `${projectName}/${projectName}.mp4`,
        `${projectName}/final.mp4`,
        `${projectName}/output.mp4`,
      ];
      for (const candidate of candidates) {
        const url = `/api/final/${encodeURIComponent(candidate)}`;
        const resp = await fetch(url);
        if (resp.ok) {
          const blob = await resp.blob();
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = `${projectName}.mp4`;
          a.click();
          URL.revokeObjectURL(a.href);
          return;
        }
      }
      // Fallback: open project output dir
      window.open(`/api/final/${encodeURIComponent(projectName)}/`, '_blank');
    } catch (e) {
      console.error('Download failed', e);
      alert((t('common.download') || '下载') + ' failed');
    } finally {
      setDownloading(null);
    }
  };

  if (loading) return <Loading />;

  return (
    <div className="space-y-6 fade-in">
      <h2 className="text-2xl font-bold text-gray-900 dark:text-white">{t('deliver.title')}</h2>

      {projects.length === 0 ? (
        <EmptyState icon="📦" title={t('deliver.noItems')} description={t('deliver.noItemsHint')} />
      ) : (
        <div className="space-y-4">
          {projects.map((proj) => (
            <Card key={proj.id}>
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="font-medium text-gray-900 dark:text-white">{proj.name}</h3>
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    {proj.episode_count} {t('ep.suffix')} • {proj.config.style}
                  </p>
                </div>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => handleDownload(proj)}
                    disabled={downloading === proj.dir_key}
                  >
                    {downloading === proj.dir_key ? t('common.loading') : t('common.download')}
                  </Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
