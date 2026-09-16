import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { exportApi, projectsApi } from '@/api/client';
import { Project } from '@/types';

type ExportFormat = 'fcpml' | 'edl' | 'json';

interface ExportedFile {
  // 后端可能返回 fcpml/edl/json 之外的格式（如 jianying/srt），此处放宽为 string，
  // 仅在做下载路由拼接时收敛回已知格式。
  format: string;
  filename: string;
  exists: boolean;
}

export function ExportPage() {
  const { t } = useApp();
  const [project, setProject] = useState('');
  const [formats, setFormats] = useState<ExportFormat[]>(['fcpml', 'edl', 'json']);
  const [files, setFiles] = useState<ExportedFile[]>([]);
  const [generating, setGenerating] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>('');
  const [projectList, setProjectList] = useState<Project[]>([]);

  useEffect(() => {
    if (projectList.length === 0) {
      projectsApi.list().then(d => {
        const list = (d as unknown as { projects?: Project[] }).projects || [];
        setProjectList(list);
        if (list.length > 0 && !project) {
          setProject(list[0].dir_key);
        }
      }).catch(() => {});
    }
  }, []);

  const loadFiles = async (proj: string) => {
    if (!proj) return;
    setLoading(true);
    setError('');
    try {
      const data = await exportApi.listFiles(proj);
      setFiles(data.files || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (project) loadFiles(project);
  }, [project]);

  const toggleFormat = (fmt: ExportFormat) => {
    setFormats(prev =>
      prev.includes(fmt) ? prev.filter(f => f !== fmt) : [...prev, fmt]
    );
  };

  const handleGenerate = async () => {
    if (!project) return;
    setGenerating(true);
    setError('');
    try {
      const data = await exportApi.generate(project, formats);
      setFiles(data.files || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : '导出失败');
    } finally {
      setGenerating(false);
    }
  };

  const handleDownload = (fmt: ExportFormat, filename: string) => {
    window.open(`/api/export/${encodeURIComponent(project)}/${fmt}`, '_blank');
  };

  const formatLabel = (fmt: ExportFormat) => t(`export.${fmt}` as any) || fmt.toUpperCase();

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold text-gray-900 dark:text-white">{t('export.title')}</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{t('export.subtitle')}</p>
        </div>
        <div className="text-xs text-gray-400 dark:text-gray-500">{t('export.desc')}</div>
      </div>

      {/* Project Selector */}
      <div className="card-glass p-4 space-y-4">
        <div className="flex items-center gap-4 flex-wrap">
          <label className="text-sm font-medium text-gray-700 dark:text-gray-300 whitespace-nowrap">
            {t('export.selectProject')}
          </label>
          <select
            value={project}
            onChange={e => setProject(e.target.value)}
            className="input-field flex-1 min-w-[200px]"
          >
            {projectList.map((p: Project) => (
              <option key={p.dir_key} value={p.dir_key}>
                {p.name} ({p.dir_key})
              </option>
            ))}
          </select>
        </div>

        {/* Format Selection */}
        <div>
          <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{t('export.formats')}：</span>
          <div className="flex gap-2 mt-2 flex-wrap">
            {(['fcpml', 'edl', 'json'] as ExportFormat[]).map(fmt => (
              <button
                key={fmt}
                onClick={() => toggleFormat(fmt)}
                className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                  formats.includes(fmt)
                    ? 'bg-blue-600 text-white shadow-md'
                    : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700'
                }`}
              >
                {formatLabel(fmt)}
              </button>
            ))}
          </div>
        </div>

        {/* Generate Button */}
        <div className="flex items-center gap-3">
          <button
            onClick={handleGenerate}
            disabled={generating || !project || formats.length === 0}
            className="btn-primary px-5 py-2 rounded-lg font-medium disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {generating ? (
              <span className="flex items-center gap-2">
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin inline-block" />
                {t('common.generating')}
              </span>
            ) : t('export.generate')}
          </button>
          {error && <span className="text-sm text-red-500">{error}</span>}
        </div>
      </div>

      {/* Exported Files */}
      <div className="card-glass p-4">
        <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">{t('export.files')}：</h3>
        {loading ? (
          <div className="flex justify-center py-8">
            <span className="w-6 h-6 border-2 border-blue-500/30 border-t-blue-500 rounded-full animate-spin inline-block" />
          </div>
        ) : files.length === 0 ? (
          <div className="text-center py-8 text-gray-400 dark:text-gray-500 text-sm">
            {t('export.noFiles')}
          </div>
        ) : (
          <div className="space-y-2">
            {files.map((file, idx) => (
              <div
                key={idx}
                className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-800/50 rounded-lg"
              >
                <div className="flex items-center gap-3">
                  <span className={`px-2 py-0.5 rounded text-xs font-mono ${
                    file.format === 'fcpml' ? 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300' :
                    file.format === 'edl' ? 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300' :
                    'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                  }`}>
                    {file.format.toUpperCase()}
                  </span>
                  <span className="text-sm text-gray-700 dark:text-gray-300 font-mono">{file.filename}</span>
                  {file.exists && (
                    <span className="text-xs text-green-600 dark:text-green-400">✓</span>
                  )}
                </div>
                <button
                  onClick={() => handleDownload(file.format as ExportFormat, file.filename)}
                  disabled={!file.exists}
                  className="btn-secondary text-xs px-3 py-1 rounded-lg disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {t('export.download')}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Info */}
      <div className="card-glass p-4">
        <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">{t('export.noteTitle')}</h3>
        <ul className="text-sm text-gray-500 dark:text-gray-400 space-y-1 list-disc list-inside">
          <li>{t('export.note1')}</li>
          <li>{t('export.note2')}</li>
          <li>{t('export.note3')}</li>
        </ul>
      </div>
    </div>
  );
}
