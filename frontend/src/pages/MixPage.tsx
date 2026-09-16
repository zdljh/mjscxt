import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { mixApi } from '@/api/client';
import { useProjects } from '@/hooks/useProjects';

interface MixTask {
  task_id: string;
  status: string;
  progress: number;
  phase: string;
  message: string;
}

export function MixPage() {
  const { t } = useApp();
  const [project, setProject] = useState('');
  const [env, setEnv] = useState<any>(null);
  const [plan, setPlan] = useState<any>(null);
  const [tasks, setTasks] = useState<MixTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string>('');

  const { projects } = useProjects();

  useEffect(() => {
    if (projects.length > 0 && !project) {
      setProject(projects[0].dir_key || projects[0].name);
    }
  }, [projects]);

  const fetchEnv = async () => {
    setLoading(true);
    try {
      const data = await mixApi.env();
      setEnv(data);
    } catch (err) {
      console.error('Failed to fetch mix env:', err);
    } finally {
      setLoading(false);
    }
  };

  const fetchPlan = async () => {
    if (!project) return;
    setLoading(true);
    try {
      const data = await mixApi.plan({ project_name: project });
      setPlan(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取计划失败');
    } finally {
      setLoading(false);
    }
  };

  const fetchTasks = async () => {
    try {
      const data = await mixApi.tasks();
      setTasks(data.items || []);
    } catch (err) {
      console.error('Failed to fetch tasks:', err);
    }
  };

  const handleGenerate = async () => {
    if (!project) return;
    setGenerating(true);
    setError('');
    try {
      const result = await mixApi.generate({ project_name: project });
      alert(t('mix.generateStarted', { defaultValue: '混音任务已启动' }) + `: ${result.task_id}`);
      fetchTasks();
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成失败');
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="card-tech p-6">
        <h2 className="text-2xl font-bold mb-4 gradient-text">{t('mix.title', { defaultValue: '音画混音' })}</h2>
        
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
            onClick={fetchEnv}
            disabled={loading}
            className="btn-glow px-6 py-2 rounded-lg font-medium disabled:opacity-50"
          >
            {t('mix.checkEnv', { defaultValue: '检查环境' })}
          </button>
          <button
            onClick={fetchPlan}
            disabled={loading}
            className="btn-glow px-6 py-2 rounded-lg font-medium disabled:opacity-50"
          >
            {t('mix.getPlan', { defaultValue: '获取计划' })}
          </button>
        </div>

        {/* Environment Status */}
        {env && (
          <div className={`p-4 rounded-lg mb-4 ${env.available ? 'bg-green-500/10 border border-green-500/30' : 'bg-red-500/10 border border-red-500/30'}`}>
            <div className="flex items-center gap-2">
              <span className="text-2xl">{env.available ? '✅' : '❌'}</span>
              <span className="font-semibold">
                {env.available ? t('mix.envAvailable', { defaultValue: '混音环境可用' }) : t('mix.envUnavailable', { defaultValue: '混音环境不可用' })}
              </span>
            </div>
            {!env.available && env.reasons && (
              <ul className="mt-2 text-sm text-red-400">
                {env.reasons.map((r: string, i: number) => (
                  <li key={i}>• {r}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* Plan Preview */}
        {plan && (
          <div className="mb-6">
            <h3 className="font-semibold text-gray-300 mb-3">{t('mix.planPreview', { defaultValue: '混音计划预览' })}</h3>
            <div className="grid grid-cols-3 gap-4 mb-4">
              <div className="bg-white/5 rounded-lg p-3 text-center">
                <div className="text-2xl font-bold text-indigo-400">{plan.video_count || 0}</div>
                <div className="text-sm text-gray-400">{t('mix.videoCount', { defaultValue: '视频片段' })}</div>
              </div>
              <div className="bg-white/5 rounded-lg p-3 text-center">
                <div className="text-2xl font-bold text-green-400">{plan.audio_count || 0}</div>
                <div className="text-sm text-gray-400">{t('mix.audioCount', { defaultValue: '音频片段' })}</div>
              </div>
              <div className="bg-white/5 rounded-lg p-3 text-center">
                <div className="text-2xl font-bold text-yellow-400">{plan.to_generate || 0}</div>
                <div className="text-sm text-gray-400">{t('mix.toGenerate', { defaultValue: '待混音' })}</div>
              </div>
            </div>

            {/* Generate Button */}
            <button
              onClick={handleGenerate}
              disabled={generating || !env?.available}
              className="btn-glow w-full py-3 rounded-lg font-medium disabled:opacity-50"
              style={{ background: 'linear-gradient(135deg, #8b5cf6 0%, #6366f1 100%)' }}
            >
              {generating ? t('common.generating', { defaultValue: '混音中...' }) : t('mix.generate', { defaultValue: '开始混音' })}
            </button>
          </div>
        )}

        {/* Tasks */}
        {tasks.length > 0 && (
          <div className="mb-6">
            <h3 className="font-semibold text-gray-300 mb-3">{t('mix.activeTasks', { defaultValue: '活跃任务' })}</h3>
            {tasks.map((task) => (
              <div key={task.task_id} className="p-3 bg-white/5 rounded-lg mb-2">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-sm text-gray-400">{task.task_id}</span>
                  <span className={`px-2 py-1 rounded text-xs ${
                    task.status === 'running' ? 'bg-yellow-500/20 text-yellow-400' :
                    task.status === 'completed' ? 'bg-green-500/20 text-green-400' :
                    'bg-red-500/20 text-red-400'
                  }`}>
                    {task.status}
                  </span>
                </div>
                <div className="mt-2 text-sm text-gray-300">{task.phase || task.message}</div>
                {task.status === 'running' && (
                  <div className="mt-2 h-2 bg-gray-700 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-gradient-to-r from-purple-500 to-indigo-500 transition-all"
                      style={{ width: `${task.progress}%` }}
                    />
                  </div>
                )}
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
