import React, { useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { projectsApi } from '@/api/client';
import { Card, Button, Loading, EmptyState } from '@/components/ui';
import type { Project, Novel } from '@/types';

export function UploadPage() {
  const { t } = useApp();
  const [novels, setNovels] = useState<Novel[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const fileInputRef = React.useRef<HTMLInputElement>(null);

  useEffect(() => {
    Promise.all([
      projectsApi.list().then(d => setProjects(d.projects || [])),
    ]).catch(console.error).finally(() => {});
  }, []);

  const handleFileChange = async (file: File | undefined) => {
    if (!file) return;
    setUploading(true);
    try {
      const resp = await fetch('/api/novels', {
        method: 'POST',
        body: file,
      });
      const data = await resp.json();
      console.log('Upload result:', data);
      // Reload novels
      const d = await projectsApi.list();
      setProjects(d.projects || []);
    } catch (e) {
      console.error('Upload failed', e);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    handleFileChange(file);
  };

  return (
    <div className="space-y-6 fade-in">
      <h2 className="text-2xl font-bold text-gray-900 dark:text-white">{t('navUpload')}</h2>

      {/* Upload zone */}
      <Card>
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          onClick={() => fileInputRef.current?.click()}
          className={`border-2 border-dashed rounded-xl p-12 text-center cursor-pointer transition-colors ${
            dragOver
              ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/10'
              : 'border-gray-300 dark:border-gray-600 hover:border-blue-400'
          }`}
        >
          <div className="text-5xl mb-4">📤</div>
          <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">
            {t('auto.dragHint')}
          </h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
            {t('auto.dragHintHint')}
          </p>
          <Button disabled={uploading}>
            {uploading ? t('common.loading') : '选择文件'}
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".txt,.docx,.pdf,.epub"
            className="hidden"
            onChange={(e) => handleFileChange(e.target.files?.[0])}
          />
        </div>
      </Card>

      {/* Projects list */}
      <Card title={t('auto.uploadedNovels')}>
        {projects.length === 0 ? (
          <EmptyState icon="📚" title={t('auto.noNovels')} description="" />
        ) : (
          <div className="space-y-3">
            {projects.map((proj) => (
              <div
                key={proj.id}
                onClick={() => { window.location.hash = `/?p=${encodeURIComponent(proj.dir_key || proj.id)}`; }}
                className="flex items-center justify-between p-4 bg-gray-50 dark:bg-gray-800/50 rounded-lg cursor-pointer hover:border-blue-500 dark:hover:border-blue-400 border border-transparent transition-colors"
              >
                <div className="flex items-center gap-3">
                  <span className="text-2xl">📖</span>
                  <div>
                    <p className="font-medium text-gray-900 dark:text-white">{proj.name}</p>
                    <p className="text-sm text-gray-500 dark:text-gray-400">
                      {proj.config.style} • {proj.episode_count} {t('ep.suffix')}
                    </p>
                  </div>
                </div>
                <span className="text-xs text-gray-400">{new Date(proj.created_at).toLocaleDateString()}</span>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
