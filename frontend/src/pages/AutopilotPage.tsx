import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { autopilotApi } from '@/api/client';
import { useProjects } from '@/hooks/useProjects';

interface AutopilotProgress {
  project: string;
  total_episodes: number;
  done: number;
  failed: number;
  current_episode?: number;
  exceptions?: Array<{ episode: number; error: string }>;
}

export function AutopilotPage() {
  const { t } = useApp();
  const [status, setStatus] = useState<any>(null);
  const [progress, setProgress] = useState<AutopilotProgress | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>('');
  const [activeProject, setActiveProject] = useState<string>('');

  const { projects } = useProjects();

  useEffect(() => {
    if (projects.length > 0 && !activeProject) {
      setActiveProject(projects[0].dir_key || projects[0].name);
    }
  }, [projects]);

  const fetchStatus = async () => {
    setLoading(true);
    try {
      const data = await autopilotApi.status();
      setStatus(data);
      
      if (activeProject) {
        const prog = await autopilotApi.progressByProject(activeProject);
        setProgress(prog);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取失败');
    } finally {
      setLoading(false);
    }
  };

  const handleEnable = async () => {
    try {
      await autopilotApi.enable();
      fetchStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : '启用失败');
    }
  };

  const handleDisable = async () => {
    try {
      await autopilotApi.disable();
      fetchStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : '禁用失败');
    }
  };

  const handlePause = async () => {
    try {
      await autopilotApi.pause();
      fetchStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : '暂停失败');
    }
  };

  const handleResume = async () => {
    try {
      await autopilotApi.resume();
      fetchStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : '恢复失败');
    }
  };

  return (
    <div className="space-y-6">
      <div className="card-tech p-6">
        <h2 className="text-2xl font-bold mb-4 gradient-text">{t('autopilot.title', { defaultValue: '自动巡航' })}</h2>
        
        {/* Project Selector */}
        <div className="flex gap-4 mb-6">
          <select
            value={activeProject}
            onChange={(e) => setActiveProject(e.target.value)}
            className="input-tech flex-1"
          >
            {projects.map((p) => (
              <option key={p.id} value={p.dir_key || p.name}>
                {p.name}
              </option>
            ))}
          </select>
          <button
            onClick={fetchStatus}
            disabled={loading}
            className="btn-glow px-6 py-2 rounded-lg font-medium disabled:opacity-50"
          >
            {loading ? t('common.loading', { defaultValue: '加载中...' }) : t('autopilot.refresh', { defaultValue: '刷新' })}
          </button>
        </div>

        {/* Status Overview */}
        {status && (
          <div className="grid grid-cols-3 gap-4 mb-6">
            <div className={`p-4 rounded-lg text-center ${
              status.running ? 'bg-green-500/10 border border-green-500/30' :
              status.paused ? 'bg-yellow-500/10 border border-yellow-500/30' :
              'bg-gray-500/10 border border-gray-500/30'
            }`}>
              <div className="text-4xl mb-2">{status.running ? '🚀' : status.paused ? '⏸️' : '⏹️'}</div>
              <div className="font-bold text-lg">
                {status.running ? t('autopilot.running', { defaultValue: '运行中' }) :
                 status.paused ? t('autopilot.paused', { defaultValue: '已暂停' }) :
                 t('autopilot.stopped', { defaultValue: '已停止' })}
              </div>
            </div>
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-green-400">{status.totals?.episodes_done || 0}</div>
              <div className="text-sm text-gray-400">{t('autopilot.episodesDone', { defaultValue: '已完成剧集' })}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-red-400">{status.totals?.episodes_failed || 0}</div>
              <div className="text-sm text-gray-400">{t('autopilot.episodesFailed', { defaultValue: '失败剧集' })}</div>
            </div>
          </div>
        )}

        {/* Progress Bar */}
        {progress && (
          <div className="mb-6">
            <div className="flex justify-between text-sm text-gray-400 mb-2">
              <span>{t('autopilot.progress', { defaultValue: '进度' })}</span>
              <span>{progress.done}/{progress.total_episodes}</span>
            </div>
            <div className="h-4 bg-gray-700 rounded-full overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-indigo-500 to-purple-500 transition-all"
                style={{ width: `${progress.total_episodes > 0 ? (progress.done / progress.total_episodes * 100) : 0}%` }}
              />
            </div>
            {progress.current_episode && (
              <div className="mt-2 text-sm text-gray-400">
                {t('autopilot.currentEpisode', { defaultValue: '当前剧集' })}: #{progress.current_episode}
              </div>
            )}
          </div>
        )}

        {/* Control Buttons */}
        <div className="grid grid-cols-4 gap-4 mb-6">
          {!status?.running && !status?.paused && (
            <button
              onClick={handleEnable}
              className="btn-glow py-3 rounded-lg font-medium"
              style={{ background: 'linear-gradient(135deg, #10b981 0%, #059669 100%)' }}
            >
              {t('autopilot.enable', { defaultValue: '启用' })}
            </button>
          )}
          {status?.running && (
            <button
              onClick={handlePause}
              className="btn-glow py-3 rounded-lg font-medium"
              style={{ background: 'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)' }}
            >
              {t('autopilot.pause', { defaultValue: '暂停' })}
            </button>
          )}
          {status?.paused && (
            <button
              onClick={handleResume}
              className="btn-glow py-3 rounded-lg font-medium"
              style={{ background: 'linear-gradient(135deg, #3b82f6 0%, #2563eb 100%)' }}
            >
              {t('autopilot.resume', { defaultValue: '恢复' })}
            </button>
          )}
          {(status?.running || status?.paused) && (
            <button
              onClick={handleDisable}
              className="btn-glow py-3 rounded-lg font-medium"
              style={{ background: 'linear-gradient(135deg, #ef4444 0%, #dc2626 100%)' }}
            >
              {t('autopilot.disable', { defaultValue: '停止' })}
            </button>
          )}
        </div>

        {/* Exceptions */}
        {progress?.exceptions && progress.exceptions.length > 0 && (
          <div className="mb-6">
            <h3 className="font-semibold text-red-400 mb-3">{t('autopilot.exceptions', { defaultValue: '异常列表' })}</h3>
            {progress.exceptions.map((ex, i) => (
              <div key={i} className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg mb-2">
                <div className="flex justify-between">
                  <span className="font-mono text-sm">第 {ex.episode} 集</span>
                </div>
                <div className="text-sm text-red-300 mt-1">{ex.error}</div>
              </div>
            ))}
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="bg-red-500/20 border border-red-500/50 rounded-lg p-4">
            {error}
          </div>
        )}
      </div>
    </div>
  );
}
