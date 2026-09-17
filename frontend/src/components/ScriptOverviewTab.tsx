import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { episodesApi } from '@/api/client';

interface ScriptOverviewTabProps {
  projectKey: string;
  novelId?: string;
}

interface EpisodeInfo {
  episode_no: number;
  title?: string;
  status: 'pending' | 'producing' | 'done' | 'failed';
  shot_count: number;
  completed_shots: number;
  created_at?: string;
  description?: string;
  risk?: string;
  expensive?: boolean;
}

export function ScriptOverviewTab({ projectKey, novelId }: ScriptOverviewTabProps) {
  const { t } = useApp();
  const [episodes, setEpisodes] = useState<EpisodeInfo[]>([]);
  const [totalEpisodes, setTotalEpisodes] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!projectKey || !novelId) return;
    
    setLoading(true);
    episodesApi.list(novelId)
      .then(data => {
        setEpisodes(data.episodes || []);
        setTotalEpisodes(data.total || 0);
      })
      .catch(err => {
        setError(err instanceof Error ? err.message : '加载失败');
      })
      .finally(() => {
        setLoading(false);
      });
  }, [projectKey, novelId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-500">{t('common.loading')}</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">
        {error}
      </div>
    );
  }

  // 统计各状态集数
  const stats = episodes.reduce((acc, ep) => {
    acc[ep.status] = (acc[ep.status] || 0) + 1;
    return acc;
  }, {} as Record<string, number>);

  const completedCount = stats['done'] || 0;
  const failedCount = stats['failed'] || 0;
  const producingCount = stats['producing'] || 0;
  const pendingCount = stats['pending'] || 0;

  return (
    <div className="space-y-6">
      {/* 概览统计卡片 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="text-2xl font-bold text-gray-900 dark:text-white">{totalEpisodes}</div>
          <div className="text-sm text-gray-500 dark:text-gray-400">总集数</div>
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="text-2xl font-bold text-green-500">{completedCount}</div>
          <div className="text-sm text-gray-500 dark:text-gray-400">已完成</div>
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="text-2xl font-bold text-blue-500">{producingCount}</div>
          <div className="text-sm text-gray-500 dark:text-gray-400">生产中</div>
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="text-2xl font-bold text-red-500">{failedCount}</div>
          <div className="text-sm text-gray-500 dark:text-gray-400">失败</div>
        </div>
      </div>

      {/* 进度条 */}
      {totalEpisodes > 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">整体进度</span>
            <span className="text-sm text-gray-500">{Math.round((completedCount / totalEpisodes) * 100)}%</span>
          </div>
          <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
            <div
              className="bg-green-500 h-2 rounded-full transition-all"
              style={{ width: `${(completedCount / totalEpisodes) * 100}%` }}
            ></div>
          </div>
        </div>
      )}

      {/* 集数列表 */}
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700">
          <h4 className="font-semibold text-gray-900 dark:text-white">剧集列表</h4>
        </div>
        
        {episodes.length === 0 ? (
          <div className="p-8 text-center text-gray-500 dark:text-gray-400">
            暂无剧集数据，请先启动自动生产
          </div>
        ) : (
          <div className="divide-y divide-gray-200 dark:divide-gray-700">
            {episodes.map((ep) => (
              <div key={ep.episode_no} className="p-4 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <span className="flex items-center justify-center w-8 h-8 rounded-full bg-indigo-100 dark:bg-indigo-500/20 text-indigo-600 dark:text-indigo-400 text-sm font-semibold">
                      {ep.episode_no}
                    </span>
                    <div>
                      <p className="font-medium text-gray-900 dark:text-white">
                        第 {ep.episode_no} 集
                        {ep.title && <span className="ml-2 text-sm text-gray-500">{ep.title}</span>}
                      </p>
                      {ep.description && (
                        <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{ep.description}</p>
                      )}
                    </div>
                  </div>
                  
                  <div className="flex items-center gap-4">
                    <div className="text-right">
                      <div className="text-sm text-gray-600 dark:text-gray-400">
                        {ep.completed_shots} / {ep.shot_count} 镜头
                      </div>
                      {ep.created_at && (
                        <div className="text-xs text-gray-400">{ep.created_at.split('T')[0]}</div>
                      )}
                    </div>
                    
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                      ep.status === 'done' ? 'bg-green-100 text-green-700 dark:bg-green-500/20 dark:text-green-400' :
                      ep.status === 'producing' ? 'bg-blue-100 text-blue-700 dark:bg-blue-500/20 dark:text-blue-400' :
                      ep.status === 'failed' ? 'bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-400' :
                      'bg-gray-100 text-gray-700 dark:bg-gray-500/20 dark:text-gray-400'
                    }`}>
                      {ep.status === 'done' ? '✓ 完成' :
                       ep.status === 'producing' ? '▶ 生产中' :
                       ep.status === 'failed' ? '✗ 失败' :
                       '○ 待生产'}
                    </span>
                  </div>
                </div>
                
                {/* 进度条 */}
                {ep.shot_count > 0 && (
                  <div className="mt-3 w-full bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
                    <div
                      className={`h-1.5 rounded-full transition-all ${
                        ep.status === 'done' ? 'bg-green-500' :
                        ep.status === 'failed' ? 'bg-red-500' :
                        'bg-blue-500'
                      }`}
                      style={{ width: `${(ep.completed_shots / ep.shot_count) * 100}%` }}
                    ></div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 备注 */}
      <div className="text-xs text-gray-500 dark:text-gray-400 text-center">
        数据来源：continuity.py 连续剧情管理模块
      </div>
    </div>
  );
}
