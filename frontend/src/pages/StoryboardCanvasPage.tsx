import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { storyboardApi } from '@/api/client';
import { useProjects } from '@/hooks/useProjects';

interface StoryboardCard {
  shot_id: string;
  seq: number;
  camera: string;
  duration: string;
  location: string;
  emotion: string;
  description: string;
  dialogue_text: string;
  storyboard?: {
    exists: boolean;
    url: string;
    success?: boolean;
    blocked?: boolean;
    error?: string;
  };
  video?: {
    exists: boolean;
    url: string;
  };
  keyframe?: {
    start: boolean;
    end_exists: boolean;
    end_url: string;
  };
  consistency?: Record<string, unknown>;
}

export function StoryboardCanvasPage() {
  const { t } = useApp();
  const [project, setProject] = useState('');
  const [cards, setCards] = useState<StoryboardCard[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>('');
  const [dragging, setDragging] = useState<number | null>(null);

  const { projects } = useProjects();

  useEffect(() => {
    if (projects.length > 0 && !project) {
      setProject(projects[0].dir_key || projects[0].name);
    }
  }, [projects]);

  const fetchCanvas = async () => {
    if (!project) return;
    setLoading(true);
    setError('');
    try {
      const data = await storyboardApi.canvas(project);
      setCards(data.cards || []);
    } catch (err) {
      // 404 表示项目没有镜头数据（非系统错误），显示友好空状态
      const msg = err instanceof Error ? err.message : '获取失败';
      if (msg.includes('404')) {
        setError('no-data');
      } else {
        setError(msg);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleDragStart = (index: number) => {
    setDragging(index);
  };

  const handleDragOver = (e: React.DragEvent, index: number) => {
    e.preventDefault();
    if (dragging === null || dragging === index) return;
    
    const newCards = [...cards];
    const [removed] = newCards.splice(dragging, 1);
    newCards.splice(index, 0, removed);
    setCards(newCards);
    setDragging(index);
  };

  const handleDragEnd = async () => {
    if (dragging === null) return;
    
    try {
      await storyboardApi.reorder({
        project_name: project,
        order: cards.map((c) => c.shot_id),
      });
    } catch (err) {
      console.error('Reorder failed:', err);
    }
    setDragging(null);
  };

  return (
    <div className="space-y-6">
      <div className="card-tech p-6">
        <h2 className="text-2xl font-bold mb-4 gradient-text">{t('storyboard.canvas', { defaultValue: '分镜画布' })}</h2>
        
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
            onClick={fetchCanvas}
            disabled={loading || !project}
            className="btn-glow px-6 py-2 rounded-lg font-medium disabled:opacity-50"
          >
            {loading ? t('common.loading', { defaultValue: '加载中...' }) : t('storyboard.refresh', { defaultValue: '刷新' })}
          </button>
        </div>

        {/* Summary Stats */}
        {cards.length > 0 && (
          <div className="grid grid-cols-4 gap-4 mb-6">
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-indigo-400">{cards.length}</div>
              <div className="text-sm text-gray-400">{t('storyboard.totalShots', { defaultValue: '总镜头数' })}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-green-400">{cards.filter(c => c.storyboard?.exists).length}</div>
              <div className="text-sm text-gray-400">{t('storyboard.storyboardReady', { defaultValue: '分镜就绪' })}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-blue-400">{cards.filter(c => c.video?.exists).length}</div>
              <div className="text-sm text-gray-400">{t('storyboard.videoReady', { defaultValue: '视频就绪' })}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-4 text-center">
              <div className="text-3xl font-bold text-yellow-400">{cards.filter(c => c.storyboard?.blocked).length}</div>
              <div className="text-sm text-gray-400">{t('storyboard.qcBlocked', { defaultValue: '质检拦截' })}</div>
            </div>
          </div>
        )}

        {/* Error */}
        {error && error !== 'no-data' && (
          <div className="bg-red-500/20 border border-red-500/50 rounded-lg p-4 mb-4">
            {error}
          </div>
        )}

        {/* Empty state for no shots */}
        {error === 'no-data' && (
          <div className="text-center py-12 text-gray-400">
            <div className="text-4xl mb-3">🎬</div>
            <p className="text-lg font-medium text-gray-300 mb-1">{t('storyboard.noShots', { defaultValue: '该项目暂无镜头数据' })}</p>
            <p className="text-sm">{t('storyboard.noShotsHint', { defaultValue: '请先生成剧本，再运行资产生产流程后返回此处刷新' })}</p>
          </div>
        )}

        {/* Drag and Drop Canvas */}
        {cards.length > 0 && (
          <div className="space-y-3">
            <h3 className="font-semibold text-gray-300 mb-3">{t('storyboard.dragDrop', { defaultValue: '拖拽排序' })}</h3>
            {cards.map((card, index) => (
              <div
                key={card.shot_id}
                draggable
                onDragStart={() => handleDragStart(index)}
                onDragOver={(e) => handleDragOver(e, index)}
                onDragEnd={handleDragEnd}
                className={`flex gap-4 p-4 rounded-lg bg-white/5 border border-white/10 cursor-move hover:border-indigo-500/50 transition-all ${
                  dragging === index ? 'opacity-50' : ''
                }`}
              >
                {/* Thumbnail */}
                <div className="w-32 h-20 bg-gray-800 rounded-lg overflow-hidden flex-shrink-0">
                  {card.storyboard?.url ? (
                    <img src={card.storyboard.url} alt={`Shot ${card.seq}`} className="w-full h-full object-cover" />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center text-gray-500">
                      {t('storyboard.noPreview', { defaultValue: '无预览' })}
                    </div>
                  )}
                </div>

                {/* Info */}
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-2">
                    <span className="px-2 py-1 bg-indigo-500/20 text-indigo-400 rounded text-sm font-mono">
                      #{card.seq}
                    </span>
                    {card.camera && (
                      <span className="text-sm text-gray-400">{card.camera}</span>
                    )}
                    {card.duration && (
                      <span className="text-sm text-gray-400">{card.duration}</span>
                    )}
                  </div>
                  
                  {card.location && (
                    <p className="text-sm text-gray-300 mb-1">
                      <span className="text-gray-500">{t('storyboard.location', { defaultValue: '场景' })}:</span> {card.location}
                    </p>
                  )}
                  
                  {card.emotion && (
                    <p className="text-sm text-gray-300 mb-1">
                      <span className="text-gray-500">{t('storyboard.emotion', { defaultValue: '情绪' })}:</span> {card.emotion}
                    </p>
                  )}
                  
                  {card.description && (
                    <p className="text-sm text-gray-400 line-clamp-2">{card.description}</p>
                  )}

                  {card.dialogue_text && (
                    <p className="mt-2 text-sm italic text-yellow-400/80">
                      "{card.dialogue_text}"
                    </p>
                  )}
                </div>

                {/* Status Badges */}
                <div className="flex flex-col gap-1">
                  {card.storyboard?.exists && (
                    <span className={`px-2 py-1 rounded text-xs ${
                      card.storyboard.blocked 
                        ? 'bg-red-500/20 text-red-400' 
                        : 'bg-green-500/20 text-green-400'
                    }`}>
                      {card.storyboard.blocked ? '⚠️ QC' : '✓ SB'}
                    </span>
                  )}
                  {card.video?.exists && (
                    <span className="px-2 py-1 bg-blue-500/20 text-blue-400 rounded text-xs">
                      ✓ Video
                    </span>
                  )}
                  {card.keyframe?.end_exists && (
                    <span className="px-2 py-1 bg-purple-500/20 text-purple-400 rounded text-xs">
                      ✓ KF
                    </span>
                  )}
                </div>

                {/* Actions */}
                <div className="flex flex-col gap-2">
                  {card.storyboard?.url && (
                    <a href={card.storyboard.url} target="_blank" rel="noopener noreferrer" 
                       className="text-indigo-400 hover:text-indigo-300 text-sm">
                      {t('common.view', { defaultValue: '查看' })}
                    </a>
                  )}
                  {card.video?.url && (
                    <a href={card.video.url} target="_blank" rel="noopener noreferrer"
                       className="text-blue-400 hover:text-blue-300 text-sm">
                      {t('storyboard.play', { defaultValue: '播放' })}
                    </a>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
