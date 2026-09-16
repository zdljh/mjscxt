import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { episodesApi } from '@/api/client';
import { useProjects } from '@/hooks/useProjects';

interface Episode {
  episode_no: number;
  title?: string;
  status: 'pending' | 'producing' | 'done' | 'failed';
  shot_count: number;
  completed_shots: number;
  created_at?: string;
  updated_at?: string;
}

export function EpisodesPage() {
  const { t } = useApp();
  const [project, setProject] = useState('');
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string>('');
  const [novelId, setNovelId] = useState<string>('');

  const { projects } = useProjects();

  useEffect(() => {
    if (projects.length > 0 && !project) {
      setProject(projects[0].dir_key || projects[0].name);
    }
  }, [projects]);

  const fetchEpisodes = async () => {
    if (!novelId) return;
    setLoading(true);
    setError('');
    try {
      const data = await episodesApi.list(novelId);
      setEpisodes(data.episodes || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取失败');
    } finally {
      setLoading(false);
    }
  };

  const handleGenerate = async () => {
    if (!novelId) return;
    setGenerating(true);
    setError('');
    try {
      const result = await episodesApi.generate(novelId);
      alert(t('episodes.generateStarted', { defaultValue: '剧集生成已启动' }));
      setTimeout(fetchEpisodes, 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成失败');
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="card-tech p-6">
        <h2 className="text-2xl font-bold mb-4 gradient-text">{t('episodes.title', { defaultValue: '剧集管理' })}</h2>
        
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
          {/* Note: In a real app, you'd need to extract novel_id from project */}
          <input
            type="text"
            placeholder={t('episodes.novelId', { defaultValue: '输入小说ID' })}
            value={novelId}
            onChange={(e) => setNovelId(e.target.value)}
            className="input-tech w-48"
          />
          <button
            onClick={fetchEpisodes}
            disabled={loading || !novelId}
            className="btn-glow px-6 py-2 rounded-lg font-medium disabled:opacity-50"
          >
            {loading ? t('common.loading', { defaultValue: '加载中...' }) : t('episodes.list', { defaultValue: '列出剧集' })}
          </button>
        </div>

        {/* Generate Button */}
        <button
          onClick={handleGenerate}
          disabled={generating || !novelId}
          className="btn-glow w-full py-3 rounded-lg font-medium mb-6 disabled:opacity-50"
          style={{ background: 'linear-gradient(135deg, #f59e0b 0%, #ef4444 100%)' }}
        >
          {generating ? t('common.generating', { defaultValue: '生成中...' }) : t('episodes.generate', { defaultValue: '生成剧集' })}
        </button>

        {/* Error */}
        {error && (
          <div className="bg-red-500/20 border border-red-500/50 rounded-lg p-4 mb-4">
            {error}
          </div>
        )}

        {/* Episodes List */}
        {episodes.length > 0 && (
          <div className="space-y-3">
            <h3 className="font-semibold text-gray-300 mb-3">{t('episodes.list', { defaultValue: '剧集列表' })}</h3>
            {episodes.map((ep) => (
              <div
                key={ep.episode_no}
                className={`flex items-center gap-4 p-4 rounded-lg ${
                  ep.status === 'done' ? 'bg-green-500/10 border border-green-500/30' :
                  ep.status === 'failed' ? 'bg-red-500/10 border border-red-500/30' :
                  ep.status === 'producing' ? 'bg-yellow-500/10 border border-yellow-500/30' :
                  'bg-white/5 border border-white/10'
                }`}
              >
                <div className="w-16 h-16 bg-gray-800 rounded-lg flex items-center justify-center text-2xl">
                  🎬
                </div>
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-lg">第 {ep.episode_no} 集</span>
                    {ep.title && <span className="text-gray-400">- {ep.title}</span>}
                  </div>
                  <div className="text-sm text-gray-500 mt-1">
                    {ep.completed_shots}/{ep.shot_count} {t('episodes.shotsCompleted', { defaultValue: '镜头完成' })}
                  </div>
                  {ep.updated_at && (
                    <div className="text-xs text-gray-600 mt-1">{ep.updated_at}</div>
                  )}
                </div>
                <div className={`px-3 py-1 rounded-full text-sm font-medium ${
                  ep.status === 'done' ? 'bg-green-500/20 text-green-400' :
                  ep.status === 'failed' ? 'bg-red-500/20 text-red-400' :
                  ep.status === 'producing' ? 'bg-yellow-500/20 text-yellow-400' :
                  'bg-gray-500/20 text-gray-400'
                }`}>
                  {ep.status === 'done' ? t('episodes.done', { defaultValue: '已完成' }) :
                   ep.status === 'failed' ? t('episodes.failed', { defaultValue: '失败' }) :
                   ep.status === 'producing' ? t('episodes.producing', { defaultValue: '生产中' }) :
                   t('episodes.pending', { defaultValue: '待生产' })}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Empty State */}
        {!loading && episodes.length === 0 && (
          <div className="text-center py-12 text-gray-500">
            <div className="text-5xl mb-4">📺</div>
            <p>{t('episodes.noEpisodes', { defaultValue: '暂无剧集数据' })}</p>
            <p className="text-sm mt-2">{t('episodes.selectNovel', { defaultValue: '请输入小说ID并点击"列出剧集"' })}</p>
          </div>
        )}
      </div>
    </div>
  );
}
