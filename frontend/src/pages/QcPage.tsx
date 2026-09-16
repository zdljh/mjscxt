import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { qcApi } from '@/api/client';
import { useProjects } from '@/hooks/useProjects';

interface QCItem {
  shot_id: string;
  seq: number;
  score: number;
  verdict: 'pass' | 'fail' | 'retry';
  timestamp: string;
  error?: string;
}

export function QcPage() {
  const { t } = useApp();
  const [project, setProject] = useState('');
  const [config, setConfig] = useState<any>(null);
  const [history, setHistory] = useState<QCItem[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>('');

  const { projects } = useProjects();

  useEffect(() => {
    if (projects.length > 0 && !project) {
      setProject(projects[0].dir_key || projects[0].name);
    }
  }, [projects]);

  const fetchData = async () => {
    if (!project) return;
    setLoading(true);
    setError('');
    try {
      const data = await qcApi.history(project);
      if (data.success) {
        setConfig(data.config || null);
        setHistory((data.history as QCItem[]) || []);
        setStats(data.stats || null);
      } else {
        setConfig(null);
        setHistory([]);
        setStats(null);
      }
    } catch (err) {
      setConfig(null);
      setHistory([]);
      setStats(null);
      setError(err instanceof Error ? err.message : '获取失败');
    } finally {
      setLoading(false);
    }
  };

  const handleSyncFromAI = async () => {
    try {
      await qcApi.syncFromAI();
      fetchData();
    } catch (err) {
      setError(err instanceof Error ? err.message : '同步失败');
    }
  };

  const handleResetEndpoint = async () => {
    try {
      await qcApi.resetEndpoint();
      fetchData();
    } catch (err) {
      setError(err instanceof Error ? err.message : '重置失败');
    }
  };

  return (
    <div className="space-y-6">
      <div className="card-tech p-6">
        <h2 className="text-2xl font-bold mb-4 gradient-text">{t('qc.title', { defaultValue: '质量检查' })}</h2>
        
        {/* Project Selector */}
        <div className="flex gap-4 mb-6">
          <select
            value={project}
            onChange={(e) => setProject(e.target.value)}
            className="input-tech flex-1"
          >
            {projects.map((p) => (
              <option key={p.id} value={p.dir_key || p.name}>
                {p.name}
              </option>
            ))}
          </select>
          <button
            onClick={fetchData}
            disabled={loading}
            className="btn-glow px-6 py-2 rounded-lg font-medium disabled:opacity-50"
          >
            {loading ? t('common.loading', { defaultValue: '加载中...' }) : t('qc.refresh', { defaultValue: '刷新' })}
          </button>
        </div>

        {/* Config */}
        {config && (
          <div className="bg-white/5 rounded-lg p-4 mb-6">
            <h3 className="font-semibold text-gray-300 mb-3">{t('qc.config', { defaultValue: '质检配置' })}</h3>
            <div className="grid grid-cols-4 gap-4">
              <div>
                <span className="text-sm text-gray-500">{t('qc.enabled', { defaultValue: '启用质检' })}</span>
                <div className={`text-lg font-bold ${config.enabled ? 'text-green-400' : 'text-gray-500'}`}>
                  {config.enabled ? '✓' : '✗'}
                </div>
              </div>
              <div>
                <span className="text-sm text-gray-500">{t('qc.maxRetries', { defaultValue: '最大重试' })}</span>
                <div className="text-lg font-bold text-indigo-400">{config.max_retries}</div>
              </div>
              <div>
                <span className="text-sm text-gray-500">{t('qc.threshold', { defaultValue: '及格线' })}</span>
                <div className="text-lg font-bold text-yellow-400">{config.threshold}</div>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={handleSyncFromAI}
                  className="btn-glow px-3 py-1 text-sm rounded"
                  style={{ background: 'linear-gradient(135deg, #10b981 0%, #059669 100%)' }}
                >
                  {t('qc.syncAI', { defaultValue: 'AI同步' })}
                </button>
                <button
                  onClick={handleResetEndpoint}
                  className="btn-glow px-3 py-1 text-sm rounded"
                  style={{ background: 'linear-gradient(135deg, #6366f1 0%, #4f46e5 100%)' }}
                >
                  {t('qc.resetEndpoint', { defaultValue: '重置端点' })}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Stats */}
        {stats && (
          <div className="grid grid-cols-4 gap-4 mb-6">
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-gray-400">{stats.total}</div>
              <div className="text-sm text-gray-500">{t('qc.totalChecks', { defaultValue: '总检查数' })}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-green-400">{stats.passed}</div>
              <div className="text-sm text-gray-500">{t('qc.passed', { defaultValue: '通过' })}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-red-400">{stats.failed}</div>
              <div className="text-sm text-gray-500">{t('qc.failed', { defaultValue: '失败' })}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-yellow-400">{stats.retry_count}</div>
              <div className="text-sm text-gray-500">{t('qc.retries', { defaultValue: '重试次数' })}</div>
            </div>
          </div>
        )}

        {/* History */}
        {history.length > 0 && (
          <div>
            <h3 className="font-semibold text-gray-300 mb-3">{t('qc.history', { defaultValue: '检查历史' })}</h3>
            <div className="space-y-2">
              {history.map((item) => (
                <div
                  key={item.seq}
                  className={`flex items-center gap-4 p-3 rounded-lg ${
                    item.verdict === 'pass' ? 'bg-green-500/10 border border-green-500/30' :
                    item.verdict === 'fail' ? 'bg-red-500/10 border border-red-500/30' :
                    'bg-yellow-500/10 border border-yellow-500/30'
                  }`}
                >
                  <span className="w-12 font-mono text-gray-400">#{item.seq}</span>
                  <span className="flex-1">{item.shot_id}</span>
                  <div className="flex items-center gap-3">
                    <span className="text-lg font-bold text-white">{item.score}</span>
                    <span className={`px-2 py-1 rounded text-xs ${
                      item.verdict === 'pass' ? 'bg-green-500/20 text-green-400' :
                      item.verdict === 'fail' ? 'bg-red-500/20 text-red-400' :
                      'bg-yellow-500/20 text-yellow-400'
                    }`}>
                      {item.verdict === 'pass' ? t('qc.pass', { defaultValue: '通过' }) :
                       item.verdict === 'fail' ? t('qc.fail', { defaultValue: '失败' }) :
                       t('qc.retry', { defaultValue: '重试' })}
                    </span>
                  </div>
                  <span className="text-xs text-gray-500">{item.timestamp}</span>
                  {item.error && (
                    <span className="text-xs text-red-400 max-w-48 truncate">{item.error}</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="bg-red-500/20 border border-red-500/50 rounded-lg p-4 mt-4">
            {error}
          </div>
        )}
      </div>
    </div>
  );
}
