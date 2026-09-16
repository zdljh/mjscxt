import React, { useState, useEffect, useRef } from 'react';
import { useApp } from '@/context/AppContext';
import { ttsApi } from '@/api/client';
import { useProjects } from '@/hooks/useProjects';

interface TTSLine {
  shot_id: string;
  seq: number;
  text: string;
  character: string;
  voice: string;
  url?: string;
  exists?: boolean;
}

interface TTSTask {
  task_id: string;
  status: string;
  progress: number;
  phase: string;
  message: string;
}

export function TtsPage() {
  const { t } = useApp();
  const [project, setProject] = useState('');
  const [env, setEnv] = useState<any>(null);
  const [plan, setPlan] = useState<any>(null);
  const [tasks, setTasks] = useState<TTSTask[]>([]);
  const [activeTask, setActiveTask] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string>('');
  const pollInterval = useRef<ReturnType<typeof setInterval> | null>(null);

  const { projects } = useProjects();

  useEffect(() => {
    if (projects.length > 0 && !project) {
      setProject(projects[0].dir_key || projects[0].name);
    }
  }, [projects]);

  useEffect(() => {
    return () => {
      if (pollInterval.current) clearInterval(pollInterval.current);
    };
  }, []);

  const fetchEnv = async () => {
    if (!project) return;
    setLoading(true);
    try {
      const data = await ttsApi.env(project);
      setEnv(data);
    } catch (err) {
      console.error('Failed to fetch TTS env:', err);
    } finally {
      setLoading(false);
    }
  };

  const fetchPlan = async () => {
    if (!project) return;
    setLoading(true);
    try {
      const data = await ttsApi.plan({ project_name: project });
      setPlan(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取计划失败');
    } finally {
      setLoading(false);
    }
  };

  const fetchTasks = async () => {
    try {
      const data = await ttsApi.tasks();
      setTasks(data.items || []);
    } catch (err) {
      console.error('Failed to fetch tasks:', err);
    }
  };

  const startPolling = (taskId: string) => {
    if (pollInterval.current) clearInterval(pollInterval.current);
    
    pollInterval.current = setInterval(async () => {
      try {
        const status = await ttsApi.status(taskId);
        setActiveTask(taskId);
        
        if (status.status === 'completed' || status.status === 'failed') {
          if (pollInterval.current) clearInterval(pollInterval.current);
          fetchTasks();
        }
      } catch (err) {
        console.error('Poll error:', err);
      }
    }, 2000);
  };

  const handleGenerate = async () => {
    if (!project) return;
    setGenerating(true);
    setError('');
    try {
      const result = await ttsApi.generate({ project_name: project });
      alert(t('tts.generateStarted', { defaultValue: '配音生成任务已启动' }) + `: ${result.task_id}`);
      startPolling(result.task_id);
      fetchTasks();
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成失败');
    } finally {
      setGenerating(false);
    }
  };

  const handlePreview = async (line: TTSLine) => {
    try {
      const result = await ttsApi.preview({
        project_name: project,
        text: line.text,
        character: line.character,
      });
      if (result.url) {
        window.open(result.url, '_blank');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '试听失败');
    }
  };

  return (
    <div className="space-y-6">
      <div className="card-tech p-6">
        <h2 className="text-2xl font-bold mb-4 gradient-text">{t('tts.title', { defaultValue: 'TTS 配音' })}</h2>
        
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
            {t('tts.checkEnv', { defaultValue: '检查环境' })}
          </button>
          <button
            onClick={fetchPlan}
            disabled={loading}
            className="btn-glow px-6 py-2 rounded-lg font-medium disabled:opacity-50"
          >
            {t('tts.getPlan', { defaultValue: '获取计划' })}
          </button>
        </div>

        {/* Environment Status */}
        {env && (
          <div className={`p-4 rounded-lg mb-4 ${env.available ? 'bg-green-500/10 border border-green-500/30' : 'bg-red-500/10 border border-red-500/30'}`}>
            <div className="flex items-center gap-2">
              <span className="text-2xl">{env.available ? '✅' : '❌'}</span>
              <span className="font-semibold">{env.available ? t('tts.envAvailable', { defaultValue: 'TTS 环境可用' }) : t('tts.envUnavailable', { defaultValue: 'TTS 环境不可用' })}</span>
            </div>
            {!env.available && env.reasons && (
              <ul className="mt-2 text-sm text-red-400">
                {env.reasons.map((r: string, i: number) => (
                  <li key={i}>• {r}</li>
                ))}
              </ul>
            )}
            {env.voices && env.voices.length > 0 && (
              <div className="mt-2 text-sm text-gray-400">
                {t('tts.voicesAvailable', { defaultValue: '可用音色' })}: {env.voices.length}
              </div>
            )}
          </div>
        )}

        {/* Plan Preview */}
        {plan && (
          <div className="mb-6">
            <h3 className="font-semibold text-gray-300 mb-3">
              {t('tts.planPreview', { defaultValue: '配音计划预览' })}
            </h3>
            <div className="grid grid-cols-3 gap-4 mb-4">
              <div className="bg-white/5 rounded-lg p-3 text-center">
                <div className="text-2xl font-bold text-indigo-400">{plan.line_count ?? (plan.lines?.length ?? 0)}</div>
                <div className="text-sm text-gray-400">{t('tts.totalLines', { defaultValue: '总台词数' })}</div>
              </div>
              <div className="bg-white/5 rounded-lg p-3 text-center">
                <div className="text-2xl font-bold text-green-400">{plan.characters?.length || 0}</div>
                <div className="text-sm text-gray-400">{t('tts.characters', { defaultValue: '涉及角色' })}</div>
              </div>
              <div className="bg-white/5 rounded-lg p-3 text-center">
                <div className="text-2xl font-bold text-blue-400">{plan.episode}</div>
                <div className="text-sm text-gray-400">{t('tts.episode', { defaultValue: '集数' })}</div>
              </div>
            </div>

            {/* Lines List */}
            <div className="space-y-2 max-h-64 overflow-y-auto">
              {plan.lines?.slice?.(0, 10)?.map((line: TTSLine) => (
                <div key={line.seq} className="flex items-center gap-3 p-2 bg-white/5 rounded">
                  <span className="w-8 text-gray-500 text-sm">#{line.seq}</span>
                  <span className="px-2 py-1 bg-indigo-500/20 text-indigo-400 rounded text-xs">
                    {line.character}
                  </span>
                  <span className="flex-1 text-sm text-gray-300 truncate">{line.text}</span>
                  <span className="text-xs text-gray-500">
                    {typeof line.voice === 'object' ? (line.voice as any)?.speaker || JSON.stringify(line.voice) : line.voice}
                  </span>
                  {line.url && (
                    <button
                      onClick={() => window.open(line.url, '_blank')}
                      className="text-green-400 hover:text-green-300 text-sm"
                    >
                      {line.exists ? t('tts.play', { defaultValue: '播放' }) : '—'}
                    </button>
                  )}
                  {!line.exists && (
                    <button
                      onClick={() => handlePreview(line)}
                      className="text-indigo-400 hover:text-indigo-300 text-sm"
                    >
                      {t('tts.preview', { defaultValue: '试听' })}
                    </button>
                  )}
                </div>
              ))}
              {plan.lines?.length > 10 && (
                <div className="text-center text-gray-500 text-sm py-2">
                  {t('tts.moreLines', { defaultValue: `还有 ${plan.lines.length - 10} 条台词...` })}
                </div>
              )}
            </div>

            {/* Generate Button */}
            <button
              onClick={handleGenerate}
              disabled={generating || !env?.available}
              className="btn-glow w-full mt-4 py-3 rounded-lg font-medium disabled:opacity-50"
              style={{ background: 'linear-gradient(135deg, #10b981 0%, #059669 100%)' }}
            >
              {generating ? t('common.generating', { defaultValue: '生成中...' }) : t('tts.generate', { defaultValue: '生成配音' })}
            </button>
          </div>
        )}

        {/* Active Tasks */}
        {tasks.length > 0 && (
          <div className="mb-6">
            <h3 className="font-semibold text-gray-300 mb-3">{t('tts.activeTasks', { defaultValue: '活跃任务' })}</h3>
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
                      className="h-full bg-gradient-to-r from-indigo-500 to-purple-500 transition-all"
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
