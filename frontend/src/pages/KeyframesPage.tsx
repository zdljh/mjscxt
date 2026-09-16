import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { keyframesApi } from '@/api/client';
import { useProjects } from '@/hooks/useProjects';

interface KeyframeShot {
  shot_id: string;
  seq: number;
  has_start: boolean;
  has_end: boolean;
  need_gen: boolean;
  url?: string;
  status?: string;
}

interface KeyframePlan {
  plan: KeyframeShot[];
  shot_count: number;
  start_frames_ready: number;
  end_frames_ready: number;
  to_generate: number;
}

export function KeyframesPage() {
  const { t } = useApp();
  const [project, setProject] = useState('');
  const [plan, setPlan] = useState<KeyframePlan | null>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string>('');

  const { projects } = useProjects();

  useEffect(() => {
    if (projects.length > 0 && !project) {
      setProject(projects[0].dir_key || projects[0].name);
    }
  }, [projects]);

  const fetchPlan = async () => {
    if (!project) return;
    setLoading(true);
    setError('');
    try {
      const data = await keyframesApi.plan(project);
      setPlan(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取失败');
    } finally {
      setLoading(false);
    }
  };

  const handleGenerate = async () => {
    if (!project) return;
    setGenerating(true);
    setError('');
    try {
      const result = await keyframesApi.generate({ project_name: project });
      alert(t('keyframes.generateStarted', { defaultValue: '尾帧生成任务已启动' }) + `: ${result.task_id}`);
      // Refresh plan after some time
      setTimeout(fetchPlan, 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成失败');
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="card-tech p-6">
        <h2 className="text-2xl font-bold mb-4 gradient-text">{t('keyframes.title', { defaultValue: '关键帧管理' })}</h2>
        
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
            onClick={fetchPlan}
            disabled={loading || !project}
            className="btn-glow px-6 py-2 rounded-lg font-medium disabled:opacity-50"
          >
            {loading ? t('common.loading', { defaultValue: '加载中...' }) : t('keyframes.refresh', { defaultValue: '刷新' })}
          </button>
        </div>

        {/* Stats */}
        {plan && (
          <div className="grid grid-cols-4 gap-4 mb-6">
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-indigo-400">{plan.shot_count}</div>
              <div className="text-sm text-gray-400">{t('keyframes.totalShots', { defaultValue: '总镜头数' })}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-green-400">{plan.start_frames_ready}</div>
              <div className="text-sm text-gray-400">{t('keyframes.startReady', { defaultValue: '首帧就绪' })}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-blue-400">{plan.end_frames_ready}</div>
              <div className="text-sm text-gray-400">{t('keyframes.endReady', { defaultValue: '尾帧就绪' })}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-yellow-400">{plan.to_generate}</div>
              <div className="text-sm text-gray-400">{t('keyframes.toGenerate', { defaultValue: '待生成' })}</div>
            </div>
          </div>
        )}

        {/* Generate Button */}
        {plan && plan.to_generate > 0 && (
          <button
            onClick={handleGenerate}
            disabled={generating}
            className="btn-glow w-full py-3 rounded-lg font-medium mb-6"
            style={{ background: 'linear-gradient(135deg, #f59e0b 0%, #ef4444 100%)' }}
          >
            {generating ? t('common.generating', { defaultValue: '生成中...' }) : t('keyframes.generate', { defaultValue: '生成尾帧' })}
          </button>
        )}

        {/* Error */}
        {error && (
          <div className="bg-red-500/20 border border-red-500/50 rounded-lg p-4 mb-4">
            {error}
          </div>
        )}

        {/* Shot List */}
        {plan && (
          <div className="space-y-2">
            <h3 className="font-semibold text-gray-300 mb-3">{t('keyframes.shotList', { defaultValue: '镜头列表' })}</h3>
            {plan.plan.map((shot) => (
              <div
                key={shot.seq}
                className={`flex items-center gap-4 p-3 rounded-lg ${
                  shot.need_gen ? 'bg-yellow-500/10 border border-yellow-500/30' :
                  shot.has_end ? 'bg-green-500/10 border border-green-500/30' :
                  'bg-white/5'
                }`}
              >
                <span className="w-12 font-mono text-gray-400">#{shot.seq}</span>
                <span className="flex-1">{shot.status || shot.shot_id}</span>
                <div className="flex gap-2">
                  {shot.has_start && (
                    <span className="px-2 py-1 bg-green-500/20 text-green-400 rounded text-xs">
                      {t('keyframes.startReady', { defaultValue: '首帧' })}
                    </span>
                  )}
                  {shot.has_end && (
                    <span className="px-2 py-1 bg-blue-500/20 text-blue-400 rounded text-xs">
                      {t('keyframes.endReady', { defaultValue: '尾帧' })}
                    </span>
                  )}
                  {shot.need_gen && !shot.has_end && (
                    <span className="px-2 py-1 bg-yellow-500/20 text-yellow-400 rounded text-xs">
                      {t('keyframes.needsGen', { defaultValue: '待生成' })}
                    </span>
                  )}
                </div>
                {shot.url && (
                  <a
                    href={shot.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-indigo-400 hover:text-indigo-300"
                  >
                    {t('common.view', { defaultValue: '查看' })}
                  </a>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
