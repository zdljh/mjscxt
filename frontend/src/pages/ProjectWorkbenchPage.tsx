import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { projectsApi, keyframesApi, storyboardApi, videoApi, ttsApi, mixApi, qcApi, exportApi, autopilotApi, upscaleApi, chatApi, agentApi, episodesApi } from '@/api/client';
import { Button, Loading, EmptyState, Modal } from '@/components/ui';
// tab 图标統一走线性 SVG（方案 P2-10）：此前是 emoji，字号受系统字体影响且观感与全站割裂
import { BarChart3, CheckCircle2, Clapperboard, Music, Network, Share2, ZoomIn } from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';
import { GridPage } from '@/pages/GridPage';
import { RelationGraphTab } from '@/components/RelationGraphTab';
import { OutputReviewTab } from '@/components/OutputReviewTab';
import { AudioTab } from '@/components/AudioTab';
import type { Project, Deliverable, UpscaleEnv, UpscaleSource, UpscaleTask, UpscaleArtifact, AgentStep } from '@/types';

// ========== Workbench Tab Types ==========
// 注意：'chat' 已移除 —— AI 总控改成了右侧常驻面板，不再是标签页（见 ChatPanel）
type WorkbenchTab = 'overview' | 'storyboard' | 'qc' | 'upscale' | 'relation' | 'audio' | 'output';

interface AssetItem {
  name: string;
  url?: string;
  file?: string;
  size?: number;
  [key: string]: any;
}

interface ProjectAssets {
  success: boolean;
  project: string;
  gallery: Record<string, AssetItem[]>;
  storyboards: AssetItem[];
  videos: AssetItem[];
  final: AssetItem[];
  dub: AssetItem[];
  counts: Record<string, number>;
}

// ========== Main Workbench Page ==========
interface ProjectWorkbenchPageProps {
  projectKey: string;
}

export function ProjectWorkbenchPage({ projectKey }: ProjectWorkbenchPageProps) {
  const { t } = useApp();
  const [project, setProject] = useState<Project | null>(null);
  const [assets, setAssets] = useState<ProjectAssets | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<WorkbenchTab>('overview');
  // AI 总控默认展开为右侧常驻面板（不占标签页）；用户可收起，收起后右侧只剩一个竖条按钮
  const [chatOpen, setChatOpen] = useState(true);

  // 资产单独抽成函数：生产启动后可在不整页刷新的情况下重新拉取（缺陷 D3）
  const reloadAssets = React.useCallback(async () => {
    if (!projectKey) return;
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(projectKey)}/assets`);
      if (r.ok) setAssets(await r.json());
    } catch {
      /* 资产刷新失败不阻塞页面 */
    }
  }, [projectKey]);

  // Load project data
  useEffect(() => {
    if (!projectKey) return;
    setLoading(true);
    Promise.all([
      projectsApi.get(projectKey).then(d => setProject(d as Project)).catch(() => null),
      reloadAssets(),
    ]).finally(() => setLoading(false));
  }, [projectKey, reloadAssets]);

  const tabs: { id: WorkbenchTab; icon: React.ReactNode; label: string }[] = [
    { id: 'overview', icon: <BarChart3 className="h-4 w-4" />, label: t('wb.overview') },
    // 自动生产已移除独立标签页 —— 改为 AI总控 内的子功能，启动前AI会先与用户沟通风格
    // 分镜管理：合并 九宫格构图 + 关键帧生成 + 分镜序列 三个子标签（见 StoryboardHubTab）
    { id: 'storyboard', icon: <Clapperboard className="h-4 w-4" />, label: t('wb.storyboardHub') },
    { id: 'qc', icon: <CheckCircle2 className="h-4 w-4" />, label: t('wb.qc') },
    // 超分：后端 upscale_client 与其 8 个端点早已可用，但前端此前零引用 ——
    // 与已删除的孤儿页面同属「建好没入口」的能力，这里补上手工入口。
    { id: 'upscale', icon: <ZoomIn className="h-4 w-4" />, label: t('wb.upscale') },
    // 角色关系图：后端 API 早已完整实现，前端此前缺失可视化组件
    { id: 'relation', icon: <Network className="h-4 w-4" />, label: t('wb.relation') },
    // 声音处理：合并 TTS 配音 + 音画混音 + 音频质检
    { id: 'audio', icon: <Music className="h-4 w-4" />, label: t('wb.audio') },
    // 输出与验收：合并导出 + 成品验收
    { id: 'output', icon: <Share2 className="h-4 w-4" />, label: t('wb.output') },
    // AI总控 不再是标签页 —— 已改为右侧常驻面板（默认展开，见下方 ChatPanel）
  ];

  if (loading) return <Loading />;
  if (!project) return (
    <EmptyState
      icon="⚠️"
      title="项目未找到"
      description={projectKey}
    />
  );

  return (
    <div className="fade-in">
      {/* 主体：左列（项目头 + 统计 + 标签内容） + 右列「AI总控」常驻面板。
          头部与统计放进左列，右侧面板才能从顶部一直贯通到底部，不会变成悬空小盒。 */}
      <div className="flex items-start gap-4">
        <div className="flex-1 min-w-0 space-y-4">
          {/* Header */}
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-2xl font-bold text-gray-900">{project.name}</h2>
              <p className="text-sm text-gray-500 mt-1">
                风格: {project.config?.style} • {project.episode_count} {t('ep.suffix')}
              </p>
            </div>
            <Button
              variant="secondary"
              onClick={() => { window.location.hash = '#/projects'; }}
            >
              ← 返回项目列表
            </Button>
          </div>

          {/* Stats Bar —— 窄屏折成两行，避免 4 列挤压成一竖条（方案 P1-8） */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            {[
              { label: '角色', count: assets?.counts?.characters || 0, color: 'text-brand' },
              { label: '物品', count: assets?.counts?.items || 0, color: 'text-green-400' },
              { label: '场景', count: assets?.counts?.scenes || 0, color: 'text-yellow-400' },
              { label: '分镜', count: assets?.counts?.storyboards || 0, color: 'text-blue-400' },
            ].map((stat) => (
              <div key={stat.label} className="bg-white rounded-lg p-4 border border-gray-200">
                <div className={`text-2xl font-bold ${stat.color}`}>{stat.count}</div>
                <div className="text-sm text-gray-500">{stat.label}</div>
              </div>
            ))}
          </div>

          {/* Tab Navigation */}
          <div className="flex flex-wrap gap-2 border-b border-gray-200 pb-4">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                  activeTab === tab.id
                    ? 'bg-brand-subtle text-brand'
                    : 'text-ink-2 hover:bg-surface-2 hover:text-ink-1'
                }`}
              >
                <span className="flex shrink-0">{tab.icon}</span>
                <span>{tab.label}</span>
              </button>
            ))}
          </div>

          {/* Tab Content */}
          <div className="min-h-[400px]">
            {activeTab === 'overview' && (
              <OverviewTab
                assets={assets}
                projectKey={projectKey}
                novelId={project.novel_id}
                onRefreshAssets={reloadAssets}
              />
            )}
            {activeTab === 'storyboard' && (
              <StoryboardHubTab projectKey={projectKey} />
            )}
            {activeTab === 'qc' && (
              <QcTab projectKey={projectKey} />
            )}
            {activeTab === 'audio' && (
              <AudioTab projectKey={projectKey} />
            )}
            {activeTab === 'output' && (
              <OutputReviewTab projectKey={projectKey} assets={assets} />
            )}
            {activeTab === 'upscale' && (
              <UpscaleTab projectKey={projectKey} />
            )}
            {activeTab === 'relation' && (
              <RelationGraphTab projectKey={projectKey} />
            )}
          </div>
        </div>

        {/* AI总控：右侧常驻面板（默认展开，可收起为竖条） */}
        {chatOpen ? (
          <ChatPanel projectKey={projectKey} onClose={() => setChatOpen(false)} />
        ) : (
          <button
            onClick={() => setChatOpen(true)}
            title="展开 AI总控"
            className="sticky top-0 shrink-0 w-11 h-[calc(100vh-7rem)] min-h-[420px] flex flex-col items-center gap-3 py-4 rounded-xl border border-gray-200 bg-white text-gray-500 hover:text-brand hover:border-brand transition-colors"
          >
            <span className="w-7 h-7 rounded-lg bg-brand-subtle flex items-center justify-center text-sm">💬</span>
            <span className="text-xs tracking-wide" style={{ writingMode: 'vertical-rl' }}>AI总控</span>
          </button>
        )}
      </div>
    </div>
  );
}

// ========== 资源地址解析 ==========
// 后端 /api/projects/<pid>/assets 返回的 url 已带 "/api" 前缀，不能重复拼接
function assetSrc(url?: string | null): string | null {
  if (!url) return null;
  if (url.startsWith('http://') || url.startsWith('https://')) return url;
  if (url.startsWith('/')) return url;
  return `/api/${url}`;
}

// ========== 错误脱敏（缺陷 D5） ==========
// 前端错误框只展示人话：丢掉 traceback / 模块名 / 文件路径等实现细节
function sanitizeError(err: unknown, fallback = '操作失败，请稍后重试'): string {
  const raw = typeof err === 'string' ? err : (err as any)?.message || '';
  let text = String(raw || '').trim();
  if (!text) return fallback;
  const tb = text.indexOf('Traceback (most recent call last)');
  if (tb !== -1) text = text.slice(0, tb).trim();
  const lines = text.split(/\r?\n/).filter(Boolean);
  text = (lines[lines.length - 1] || '').trim();
  text = text
    .replace(/File\s+"[^"]*",\s*line\s*\d+/g, '')
    .replace(/\s*from\s+'[^']*'/g, '')
    .replace(/\s*\([^()]*\.py[^()]*\)/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (/\.py\b|\bimport\b|\bmodule\b|site-packages|[\\/]/.test(text)) return fallback;
  return text.length > 160 ? `${text.slice(0, 160)}…` : text;
}

// ========== Overview Tab ==========
function OverviewTab({
  assets,
  projectKey,
  novelId,
  onRefreshAssets,
}: {
  assets: ProjectAssets | null;
  projectKey: string;
  novelId?: string;
  onRefreshAssets: () => Promise<void> | void;
}) {
  const [preview, setPreview] = useState<{ item: AssetItem; type: 'character' | 'item' | 'scene' } | null>(null);

  // 剧本相关状态
  const [episodes, setEpisodes] = useState<any[]>([]);
  const [totalEpisodes, setTotalEpisodes] = useState(0);
  const [scriptLoading, setScriptLoading] = useState(true);
  const [scriptError, setScriptError] = useState('');
  const [selectedEpisode, setSelectedEpisode] = useState<number | null>(null);
  const [episodeDetail, setEpisodeDetail] = useState<any>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');

  // 加载剧集列表
  useEffect(() => {
    if (!novelId) return;
    setScriptLoading(true);
    episodesApi.list(novelId)
      .then(data => {
        setEpisodes(data.episodes || []);
        setTotalEpisodes(data.total || 0);
      })
      .catch(err => {
        setScriptError(err instanceof Error ? err.message : '加载失败');
      })
      .finally(() => {
        setScriptLoading(false);
      });
  }, [novelId]);

  // 加载单集详情
  // ⚠️ 后端 /api/episodes/<novel>/<ep> 的剧本正文嵌在 `script` 对象下（shots/characters/items/scenes），
  // 且列表行才带 status/completed_shots（_episode_progress 推导），详情接口本身不返回这两个字段。
  // 这里摊平成视图直接可读的结构，并从已加载的剧集列表补进度字段，避免详情恒显「暂无剧本内容」。
  const loadEpisodeDetail = async (episodeNo: number) => {
    if (!novelId) return;
    setDetailLoading(true);
    setDetailError('');
    try {
      const detail = await episodesApi.get(novelId, episodeNo);
      const script = (detail as any).script || {};
      const shots: any[] = script.shots || [];
      // 从剧集列表找本行进度（status / completed_shots / created_at）
      const row = episodes.find((e: any) => e.episode_no === episodeNo);
      const normalized: any = {
        ...detail,
        ...script,
        episode_no: detail.episode_no ?? episodeNo,
        title: detail.episode_title || detail.project_name || script.title,
        chapter_title: detail.episode_title,
        shots,
        shot_count: (detail as any).stats?.shot_count || script.shot_count || shots.length,
        status: row?.status ?? (detail as any).status ?? 'pending',
        completed_shots: row?.completed_shots ?? (detail as any).completed_shots ?? 0,
        created_at: row?.generated_at ?? (detail as any).created_at,
      };
      setEpisodeDetail(normalized);
      setSelectedEpisode(episodeNo);
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : '加载详情失败');
    } finally {
      setDetailLoading(false);
    }
  };

  // 返回列表
  const goBack = () => {
    setSelectedEpisode(null);
    setEpisodeDetail(null);
    setDetailError('');
  };

  const groups: { key: 'characters' | 'items' | 'scenes'; label: string; icon: string; type: 'character' | 'item' | 'scene' }[] = [
    { key: 'characters', label: '角色', icon: '👤', type: 'character' },
    { key: 'items', label: '物品', icon: '📦', type: 'item' },
    { key: 'scenes', label: '场景', icon: '🏞️', type: 'scene' },
  ];

  const total = groups.reduce((n, g) => n + (assets?.gallery?.[g.key]?.length || 0), 0);

  // 显示单集详情
  if (selectedEpisode !== null && episodeDetail && !detailError) {
    return (
      <div className="space-y-4">
        <button
          onClick={goBack}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-gray-600 hover:text-gray-900 transition-colors"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
          </svg>
          返回列表
        </button>

        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <div className="flex items-start justify-between">
            <div>
              <h3 className="text-xl font-bold text-gray-900">
                第 {episodeDetail.episode_no} 集
                {episodeDetail.title && <span className="ml-2 text-lg font-normal text-gray-500">{episodeDetail.title}</span>}
              </h3>
              {episodeDetail.chapter_title && (
                <p className="text-sm text-gray-500 mt-1">章节：{episodeDetail.chapter_title}</p>
              )}
            </div>
            <span className={`px-3 py-1 rounded-full text-xs font-medium ${
              episodeDetail.status === 'done' ? 'bg-green-100 text-green-700' :
              episodeDetail.status === 'producing' ? 'bg-blue-100 text-blue-700' :
              episodeDetail.status === 'failed' ? 'bg-red-100 text-red-700' :
              'bg-gray-100 text-gray-700'
            }`}>
              {episodeDetail.status === 'done' ? '✓ 完成' :
               episodeDetail.status === 'producing' ? '▶ 生产中' :
               episodeDetail.status === 'failed' ? '✗ 失败' :
               '○ 待生产'}
            </span>
          </div>

          <div className="mt-4 flex items-center gap-4 text-sm text-gray-500">
            <span>镜头进度：{episodeDetail.completed_shots} / {episodeDetail.shot_count}</span>
            {episodeDetail.created_at && (
              <span>创建时间：{episodeDetail.created_at.split('T')[0]}</span>
            )}
          </div>

          {episodeDetail.shot_count > 0 && (
            <div className="mt-3 w-full bg-gray-200 rounded-full h-2">
              <div
                className={`h-2 rounded-full transition-all ${
                  episodeDetail.status === 'done' ? 'bg-green-500' :
                  episodeDetail.status === 'failed' ? 'bg-red-500' :
                  'bg-blue-500'
                }`}
                style={{ width: `${(episodeDetail.completed_shots / episodeDetail.shot_count) * 100}%` }}
              ></div>
            </div>
          )}
        </div>

        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h4 className="font-semibold text-gray-900 mb-4">剧本内容</h4>

          {(episodeDetail.shots && episodeDetail.shots.length > 0) ? (
            <div className="space-y-4">
              {episodeDetail.shots.map((shot: any, idx: number) => (
                <div key={idx} className="border-l-4 border-brand pl-4 py-2">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    <span className="px-2 py-0.5 bg-brand-subtle text-brand text-xs font-medium rounded">
                      镜头 {shot.shot_id ?? idx + 1}
                    </span>
                    {shot.camera && (
                      <span className="text-xs text-gray-500">{shot.camera}</span>
                    )}
                    {shot.location && (
                      <span className="text-xs text-gray-500">· {shot.location}</span>
                    )}
                    {shot.duration != null && (
                      <span className="text-xs text-gray-400">· {shot.duration}s</span>
                    )}
                  </div>
                  {shot.description && (
                    <p className="text-sm text-gray-700">{shot.description}</p>
                  )}
                  {shot.dialogue_text && (
                    <p className="text-sm text-gray-800 mt-1 pl-2 border-l-2 border-gray-300">
                      {shot.dialogue_text}
                    </p>
                  )}
                  {shot.visual_detail && (
                    <p className="text-xs text-gray-500 mt-1">
                      视觉描述：{shot.visual_detail}
                    </p>
                  )}
                  {shot.audio_cues && (
                    <p className="text-xs text-gray-400 mt-1">
                      音效：{shot.audio_cues}
                    </p>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-gray-500 text-sm">
              本集剧本暂无镜头数据（可能尚未生成，或该集还在生产中）。
            </p>
          )}
        </div>
      </div>
    );
  }

  if (detailError) {
    return (
      <div className="space-y-4">
        <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
          {detailError}
        </div>
        <button onClick={goBack} className="text-sm text-brand hover:underline">
          返回列表
        </button>
      </div>
    );
  }

  if (detailLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-500">加载中...</div>
      </div>
    );
  }

  if (total === 0 && episodes.length === 0) {
    return (
      <div className="py-12">
        <div className="text-center text-gray-500">
          <div className="text-4xl mb-3">📁</div>
          <p className="font-medium">暂无资产</p>
          <p className="text-sm mt-2">角色 / 物品 / 场景 会在生产流程中自动生成</p>
          <p className="text-sm mt-3 text-brand">
            请通过右侧「AI总控」下达生产指令，AI会先与您沟通生产风格再启动
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* 资产展示 */}
      {total > 0 && (
        <>
          {groups.map((g) => {
            const list = assets?.gallery?.[g.key] || [];
            if (list.length === 0) return null;
            return (
              <div key={g.key}>
                <h3 className="text-sm font-semibold text-gray-500 mb-3">
                  {g.icon} {g.label} · {list.length}
                </h3>
                <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
                  {list.map((item, idx) => (
                    <AssetCard
                      key={`${g.key}-${idx}`}
                      item={item}
                      type={g.type}
                      onClick={() => setPreview({ item, type: g.type })}
                    />
                  ))}
                </div>
              </div>
            );
          })}
          <AssetPreviewModal preview={preview} onClose={() => setPreview(null)} />
        </>
      )}

      {/* 剧本概览 */}
      <div className="border-t border-gray-200 pt-8">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">
          📝 剧本概览
        </h3>

        {scriptLoading ? (
          <div className="flex items-center justify-center h-32">
            <div className="text-gray-500">加载中...</div>
          </div>
        ) : scriptError ? (
          <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">
            {scriptError}
          </div>
        ) : episodes.length === 0 ? (
          <div className="bg-white rounded-lg border border-gray-200 p-8 text-center text-gray-500">
            暂无剧集数据，请先启动自动生产
          </div>
        ) : (
          <>
            {/* 统计卡片 */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
              <div className="bg-white rounded-lg border border-gray-200 p-4">
                <div className="text-2xl font-bold text-gray-900">{totalEpisodes}</div>
                <div className="text-sm text-gray-500">总集数</div>
              </div>
              {(() => {
                const stats = episodes.reduce((acc: any, ep: any) => {
                  acc[ep.status] = (acc[ep.status] || 0) + 1;
                  return acc;
                }, {} as Record<string, number>);
                return [
                  { label: '已完成', count: stats['done'] || 0, color: 'text-green-500' },
                  { label: '生产中', count: stats['producing'] || 0, color: 'text-blue-500' },
                  { label: '失败', count: stats['failed'] || 0, color: 'text-red-500' },
                  { label: '待生产', count: stats['pending'] || 0, color: 'text-gray-500' },
                ].map(s => (
                  <div key={s.label} className="bg-white rounded-lg border border-gray-200 p-4">
                    <div className={`text-2xl font-bold ${s.color}`}>{s.count}</div>
                    <div className="text-sm text-gray-500">{s.label}</div>
                  </div>
                ));
              })()}
            </div>

            {/* 进度条 */}
            {totalEpisodes > 0 && (
              <div className="bg-white rounded-lg border border-gray-200 p-4 mb-4">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm font-medium text-gray-700">整体进度</span>
                  <span className="text-sm text-gray-500">{Math.round(((episodes.filter((e: any) => e.status === 'done').length) / totalEpisodes) * 100)}%</span>
                </div>
                <div className="w-full bg-gray-200 rounded-full h-2">
                  <div
                    className="bg-green-500 h-2 rounded-full transition-all"
                    style={{ width: `${((episodes.filter((e: any) => e.status === 'done').length) / totalEpisodes) * 100}%` }}
                  ></div>
                </div>
              </div>
            )}

            {/* 剧集列表 */}
            <div className="bg-white rounded-lg border border-gray-200">
              <div className="p-4 border-b border-gray-200">
                <h4 className="font-semibold text-gray-900">剧集列表</h4>
                <p className="text-xs text-gray-500 mt-1">点击集数查看剧本详情</p>
              </div>

              <div className="divide-y divide-gray-200">
                {episodes.map((ep: any) => (
                  <button
                    key={ep.episode_no}
                    onClick={() => loadEpisodeDetail(ep.episode_no)}
                    className="w-full p-4 hover:bg-gray-50 transition-colors text-left"
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-3">
                        <span className="flex items-center justify-center w-8 h-8 rounded-full bg-brand-subtle text-brand text-sm font-semibold">
                          {ep.episode_no}
                        </span>
                        <div>
                          <p className="font-medium text-gray-900">
                            第 {ep.episode_no} 集
                            {ep.chapter_title && <span className="ml-2 text-sm text-brand">《{ep.chapter_title}》</span>}
                          </p>
                          <p className="text-xs text-gray-500 mt-0.5">
                            章节 {ep.chapter_index ?? ep.episode_no}
                          </p>
                        </div>
                      </div>

                      <div className="flex items-center gap-4">
                        <div className="text-right">
                          <div className="text-sm text-gray-600">
                            {ep.completed_shots} / {ep.shot_count} 镜头
                          </div>
                          {ep.created_at && (
                            <div className="text-xs text-gray-400">{ep.created_at.split('T')[0]}</div>
                          )}
                        </div>

                        <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                          ep.status === 'done' ? 'bg-green-100 text-green-700' :
                          ep.status === 'producing' ? 'bg-blue-100 text-blue-700' :
                          ep.status === 'failed' ? 'bg-red-100 text-red-700' :
                          'bg-gray-100 text-gray-700'
                        }`}>
                          {ep.status === 'done' ? '✓ 完成' :
                           ep.status === 'producing' ? '▶ 生产中' :
                           ep.status === 'failed' ? '✗ 失败' :
                           '○ 待生产'}
                        </span>

                        <svg className="w-5 h-5 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                        </svg>
                      </div>
                    </div>

                    {ep.shot_count > 0 && (
                      <div className="mt-3 w-full bg-gray-200 rounded-full h-1.5">
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
                  </button>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ========== Asset Card ==========
function AssetCard({
  item,
  type,
  onClick,
}: {
  item: AssetItem;
  type: 'character' | 'item' | 'scene';
  onClick?: () => void;
}) {
  const imageUrl = assetSrc(item.thumb?.url || item.views?.[0]?.url);
  const [broken, setBroken] = useState(false);
  const fallbackIcon = type === 'character' ? '👤' : type === 'item' ? '📦' : '🏞️';

  return (
    <button
      type="button"
      onClick={onClick}
      className="text-left bg-white rounded-xl border border-gray-200 overflow-hidden hover:shadow-lg transition-shadow"
    >
      <div className="aspect-video bg-surface-2 flex items-center justify-center overflow-hidden">
        {imageUrl && !broken ? (
          <img
            src={imageUrl}
            alt={item.name}
            loading="lazy"
            className="w-full h-full object-cover"
            onError={() => setBroken(true)}
          />
        ) : (
          <span className="text-4xl">{fallbackIcon}</span>
        )}
      </div>
      <div className="p-3">
        <h4 className="font-medium text-gray-900 text-sm truncate">{item.name}</h4>
        <p className="text-xs text-gray-500 mt-1">
          {item.category || (item.view_count ? `${item.view_count} 个视角` : fallbackIcon === '👤' ? '角色' : fallbackIcon === '📦' ? '物品' : '场景')}
        </p>
      </div>
    </button>
  );
}

// ========== Asset Preview Modal ==========
// 薄封装：遮罩、头部、动画、ESC / 遮罩关闭、滚动锁定、焦点陷阱、层级全部由共享 Modal
// 负责（方案 P1-7）。此前这里是一份独立的自建弹层（bg-black/60 + p-4 头部 + z-modal），
// 与全站 Modal 的观感和层级都对不上。本组件只保留资产预览自己的业务：多视角切换 + 下载。
function AssetPreviewModal({
  preview,
  onClose,
}: {
  preview: { item: AssetItem; type: 'character' | 'item' | 'scene' } | null;
  onClose: () => void;
}) {
  const [active, setActive] = useState(0);
  const isOpen = !!preview;

  useEffect(() => { setActive(0); }, [preview]);

  const gallery = React.useMemo(() => {
    if (!preview) return [] as { url: string; view?: string; size?: number }[];
    return [
      preview.item.thumb,
      ...(preview.item.views || []),
    ].filter(Boolean) as { url: string; view?: string; size?: number }[];
  }, [preview]);

  // ← / → 在多个视角之间切换（图片浏览器的最低预期）
  useEffect(() => {
    if (!isOpen || gallery.length <= 1) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight') setActive((i) => (i + 1) % gallery.length);
      else if (e.key === 'ArrowLeft') setActive((i) => (i - 1 + gallery.length) % gallery.length);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [isOpen, gallery.length]);

  // 共享 Modal 已处理 isOpen=false 时不渲染，这里只需容忍 preview 为空时的取值
  const item = preview?.item;
  const current = gallery[active];
  const src = assetSrc(current?.url);

  const downloadCurrent = () => {
    if (!src) return;
    const a = document.createElement('a');
    a.href = src;
    a.download = `${item?.name || 'asset'}${current?.view ? '_' + current.view : ''}.png`;
    a.click();
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={item?.name || '资产预览'}
      size="xl"
      footer={
        <div className="flex w-full items-center gap-2">
          <Button size="sm" variant="secondary" onClick={downloadCurrent}>
            下载当前图
          </Button>
          {current?.size && (
            <span className="text-xs text-ink-3">{(current.size / 1024).toFixed(0)} KB</span>
          )}
          {gallery.length > 1 && (
            <span className="ml-auto text-xs text-ink-3">← → 切换视角</span>
          )}
        </div>
      }
    >
        <div className="space-y-4">
          {src ? (
            <img
              src={src}
              alt={`${item?.name || ''}${current?.view ? ` - ${current.view}` : ''}`}
              className="w-full rounded-md bg-surface-2"
            />
          ) : (
            <div className="py-16 text-center text-ink-2">图片不可用</div>
          )}

          {gallery.length > 1 && (
            <div className="flex flex-wrap gap-2" role="tablist" aria-label="视角切换">
              {gallery.map((g, i) => {
                const t = assetSrc(g.url);
                return (
                  <button
                    key={i}
                    type="button"
                    role="tab"
                    aria-selected={i === active}
                    aria-label={g.view || `视角 ${i + 1}`}
                    onClick={() => setActive(i)}
                    className={`h-14 w-20 overflow-hidden rounded border-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 ${
                      i === active ? 'border-brand' : 'border-transparent hover:border-line-strong'
                    }`}
                  >
                    {t && <img src={t} alt="" className="h-full w-full object-cover" />}
                  </button>
                );
              })}
            </div>
          )}
        </div>
    </Modal>
  );
}

// ========== QC Tab（功能质检） ==========
// 后端 /api/qc/project-summary 早已返回「引擎状态 + 统计 + 逐镜质检明细」，
// 但前端此前只有标签没有渲染 —— 点进去是空白。这里补齐只读总览 + 单镜重测。
function QcTab({ projectKey }: { projectKey: string }) {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [testing, setTesting] = useState<string | null>(null);

  const load = async () => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      setData((await qcApi.history(projectKey)) as any);
    } catch (e) {
      setError(sanitizeError(e, '获取质检状态失败'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [projectKey]);

  const runTest = async (shotId: string) => {
    setTesting(shotId);
    setError('');
    setNotice('');
    try {
      const r = await qcApi.test({ project: projectKey, shot_id: shotId });
      const verdict = r.verdict === 'pass' ? '通过' : r.verdict === 'fail' ? '未通过' : String(r.verdict || '未知');
      setNotice(`重测完成：${verdict}${typeof r.score === 'number' ? `（${Math.round(r.score * 100)} 分）` : ''}`);
      await load();
    } catch (e) {
      setError(sanitizeError(e, '重测失败'));
    } finally {
      setTesting(null);
    }
  };

  const verdictBadge = (v: string) => {
    if (v === 'pass') return 'bg-green-100 text-green-700';
    if (v === 'fail') return 'bg-red-100 text-red-700';
    return 'bg-gray-100 text-gray-700';
  };

  if (loading) return <Loading />;

  const cfg = data?.config || {};
  const stats = data?.stats || { total: 0, passed: 0, failed: 0, retry_count: 0 };
  const records: any[] = Array.isArray(data?.history) ? data.history : [];
  // 后端 history[].kind 是**质检品类**（图片/视频/尾帧/资产/音频/剧本/提示词），
  // 此前一律渲染成「图像」，资产与提示词的记录显示得驴唇不对马嘴。
  const KIND_LABEL: Record<string, string> = {
    video: '视频', image: '图像', keyframe: '尾帧', asset: '资产',
    audio: '音频', script: '剧本', prompt: '提示词',
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold text-gray-900">功能质检</h3>
        <Button size="sm" variant="secondary" onClick={load}>刷新</Button>
      </div>

      {notice && (
        <div className="p-3 bg-green-500/10 border border-green-500/30 rounded-lg text-green-600 text-sm">
          {notice}
        </div>
      )}
      {error && (
        <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">{error}</div>
      )}

      {/* 质检引擎状态 */}
      <div className="bg-white rounded-lg border border-gray-200 p-4">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-gray-900">质检引擎</span>
          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
            cfg.enabled ? 'bg-green-100 text-green-700'
                        : 'bg-gray-100 text-gray-600'
          }`}>
            {cfg.enabled ? '已启用' : '未启用'}
          </span>
          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
            cfg.ready ? 'bg-brand-subtle text-brand-hover'
                      : 'bg-yellow-100 text-yellow-700'
          }`}>
            {cfg.ready ? '就绪' : '未就绪'}
          </span>
        </div>
        <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div>
            <div className="text-gray-500 text-xs">模型</div>
            <div className="text-gray-900 truncate">{cfg.effective_model || cfg.model || '—'}</div>
          </div>
          <div>
            <div className="text-gray-500 text-xs">通过分数线</div>
            <div className="text-gray-900">{cfg.pass_score ?? '—'}</div>
          </div>
          <div>
            <div className="text-gray-500 text-xs">接口地址</div>
            <div className="text-gray-900 truncate">{cfg.effective_base_url || cfg.base_url || '—'}</div>
          </div>
          <div>
            <div className="text-gray-500 text-xs">API Key</div>
            <div className="text-gray-900">{cfg.has_api_key ? (cfg.api_key_masked || '已配置') : '未配置'}</div>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          {[
            { label: '剧本', on: cfg.script_enabled },
            { label: '图像', on: cfg.image_enabled },
            { label: '视频', on: cfg.video_enabled },
            { label: '音频', on: cfg.audio_enabled },
          ].map((k) => (
            <span key={k.label} className={`px-2 py-0.5 rounded ${
              k.on ? 'bg-brand-subtle text-brand'
                   : 'bg-gray-100 text-gray-500'
            }`}>
              {k.label}质检 {k.on ? '开' : '关'}
            </span>
          ))}
        </div>
      </div>

      {/* 统计 */}
      <div className="grid grid-cols-4 gap-4">
        {[
          { label: '质检总数', value: stats.total, color: 'text-gray-900' },
          { label: '通过', value: stats.passed, color: 'text-green-500' },
          { label: '未通过', value: stats.failed, color: 'text-red-500' },
          { label: '待重试', value: stats.retry_count, color: 'text-yellow-500' },
        ].map((s) => (
          <div key={s.label} className="bg-white rounded-lg border border-gray-200 p-4 text-center">
            <div className={`text-2xl font-bold ${s.color}`}>{s.value ?? 0}</div>
            <div className="text-xs text-gray-500 mt-1">{s.label}</div>
          </div>
        ))}
      </div>

      {/* 逐镜明细 */}
      {records.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-8 text-center text-gray-500">
          暂无质检记录。镜头在流水线跑到「质检」环节后会在此出现。
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 divide-y divide-gray-200">
          {records.map((r, i) => (
            <div key={`${r.shot_id}-${r.kind}-${i}`} className="p-3 flex items-center gap-3">
              <span className="font-mono text-sm text-gray-900">{r.shot_id}</span>
              <span className="text-xs px-2 py-0.5 rounded bg-gray-100 text-gray-600">
                {KIND_LABEL[r.kind] || r.kind || '质检'}
              </span>
              <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${verdictBadge(r.verdict)}`}>
                {r.verdict === 'pass' ? '通过' : r.verdict === 'fail' ? '未通过'
                  : r.verdict === 'error' ? '接口异常' : r.verdict === 'unknown' ? '待重试'
                  : String(r.verdict || '未知')}
              </span>
              {typeof r.score === 'number' && (
                // ⚠️ 后端 score 是 **0~100**（实测区间 15~98），不是 0~1 的比例。
                // 这里此前无条件 *100，会把 82 分显示成「8200」。
                <span className="text-xs text-gray-500">
                  得分 {Math.round(r.score <= 1 ? r.score * 100 : r.score)}
                </span>
              )}
              {r.timestamp && (
                <span className="text-xs text-gray-400 ml-auto">{String(r.timestamp).replace('T', ' ').slice(0, 19)}</span>
              )}
              {(r.kind === 'image' || r.kind === 'video') && (
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={testing === r.shot_id}
                  onClick={() => runTest(r.shot_id)}
                >
                  {testing === r.shot_id ? '重测中…' : '重测'}
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ========== 分镜管理（九宫格 + 关键帧 + 分镜序列 三合一） ==========
// 三者本是同一工序的三个阶段：先出构图草案 → 再定首尾关键帧 → 最后成型分镜序列。
// 拆成 3 个顶级标签会让用户在标签间来回跳，这里收成一个标签页 + 3 个子标签。
function StoryboardHubTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const [sub, setSub] = useState<'storyboard' | 'ninegrid' | 'keyframes'>('storyboard');

  const subs: { id: 'storyboard' | 'ninegrid' | 'keyframes'; icon: string; label: string; hint: string }[] = [
    { id: 'storyboard', icon: '🎬', label: t('wb.subStoryboard'), hint: '完整镜头列表' },
    { id: 'ninegrid', icon: '🎯', label: t('wb.subNinegrid'), hint: '镜头构图草案' },
    { id: 'keyframes', icon: '🖼️', label: t('wb.subKeyframes'), hint: '镜头首尾帧' },
  ];

  return (
    <div className="space-y-5">
      {/* 子标签导航 */}
      <div className="flex flex-wrap gap-2">
        {subs.map((s) => (
          <button
            key={s.id}
            onClick={() => setSub(s.id)}
            title={s.hint}
            className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm transition-all ${
              sub === s.id
                ? 'bg-brand text-white shadow'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
            }`}
          >
            <span>{s.icon}</span>
            <span>{s.label}</span>
          </button>
        ))}
      </div>

      {sub === 'storyboard' && <StoryboardTab projectKey={projectKey} />}
      {sub === 'ninegrid' && <GridPage projectKey={projectKey} />}
      {sub === 'keyframes' && <KeyframesTab projectKey={projectKey} />}
    </div>
  );
}

// ========== Keyframes Tab ==========
function KeyframesTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const toast = useToast();
  const [plan, setPlan] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');

  const fetchPlan = async () => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      const data = await keyframesApi.plan(projectKey);
      setPlan(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取失败');
    } finally {
      setLoading(false);
    }
  };

  const handleGenerate = async () => {
    if (!projectKey) return;
    setGenerating(true);
    setError('');
    try {
      const result = await keyframesApi.generate({ project_name: projectKey });
      toast.success(`尾帧生成任务已启动：${result.task_id}`);
      setTimeout(fetchPlan, 3000);
    } catch (err) {
      const msg = err instanceof Error ? err.message : '生成失败';
      setError(msg);
      toast.error(msg);
    } finally {
      setGenerating(false);
    }
  };

  useEffect(() => { fetchPlan(); }, [projectKey]);

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold">关键帧管理</h3>
        <Button size="sm" onClick={fetchPlan} disabled={loading}>刷新</Button>
      </div>

      {error && <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">{error}</div>}

      {plan && (
        <div className="grid grid-cols-4 gap-4">
          <div className="bg-white rounded-lg border border-gray-200 p-4 text-center">
            <div className="text-3xl font-bold text-brand">{plan.shot_count}</div>
            <div className="text-sm text-gray-500">总镜头数</div>
          </div>
          <div className="bg-white rounded-lg border border-gray-200 p-4 text-center">
            <div className="text-3xl font-bold text-green-400">{plan.start_frames_ready}</div>
            <div className="text-sm text-gray-500">首帧就绪</div>
          </div>
          <div className="bg-white rounded-lg border border-gray-200 p-4 text-center">
            <div className="text-3xl font-bold text-blue-400">{plan.end_frames_ready}</div>
            <div className="text-sm text-gray-500">尾帧就绪</div>
          </div>
          <div className="bg-white rounded-lg border border-gray-200 p-4 text-center">
            <div className="text-3xl font-bold text-yellow-400">{plan.to_generate}</div>
            <div className="text-sm text-gray-500">待生成</div>
          </div>
        </div>
      )}

      {plan && plan.to_generate > 0 && (
        <Button
          onClick={handleGenerate}
          disabled={generating}
          className="w-full bg-warning hover:bg-warning-strong"
        >
          {generating ? '生成中...' : '生成尾帧'}
        </Button>
      )}

      {plan && (
        <div className="space-y-2">
          <h4 className="font-semibold text-gray-700 mb-3">镜头列表</h4>
          {plan.plan?.map((shot: any) => (
            <div
              key={shot.seq}
              className={`flex items-center gap-4 p-3 rounded-lg ${
                shot.need_gen ? 'bg-yellow-500/10 border border-yellow-500/30' :
                shot.has_end ? 'bg-green-500/10 border border-green-500/30' :
                'bg-gray-50'
              }`}
            >
              <span className="w-12 font-mono text-gray-500">#{shot.seq}</span>
              <span className="flex-1">{shot.status || shot.shot_id}</span>
              <div className="flex gap-2">
                {shot.has_start && <span className="px-2 py-1 bg-green-500/20 text-green-400 rounded text-xs">首帧</span>}
                {shot.has_end && <span className="px-2 py-1 bg-blue-500/20 text-blue-400 rounded text-xs">尾帧</span>}
                {shot.need_gen && !shot.has_end && <span className="px-2 py-1 bg-yellow-500/20 text-yellow-400 rounded text-xs">待生成</span>}
              </div>
              {shot.url && (
                <a href={shot.url} target="_blank" rel="noopener noreferrer" className="text-brand hover:text-brand">查看</a>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ========== Storyboard Tab ==========
// 单镜重做闭环：后端 /api/storyboard/retry-shot（分镜图）与 /api/video/retry-shot（视频）
// 早已实现，但前端此前**零入口** —— 用户对某一镜不满意只能整集重跑。
// 这里把两个入口放到每张分镜卡上，并在视频重做成功后提示「同集成片已过期」。
function StoryboardTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const [cards, setCards] = useState<any[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  /** 正在重做的镜头：`${shot_id}:image` / `${shot_id}:video` */
  const [busy, setBusy] = useState<string | null>(null);
  /** 每镜的视频重做模式（reference=分镜图驱动 / keyframe=首尾帧插值） */
  const [videoMode, setVideoMode] = useState<Record<string, 'reference' | 'keyframe'>>({});
  const [notice, setNotice] = useState('');
  const [shotError, setShotError] = useState('');

  const fetchCanvas = async () => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      const data = await storyboardApi.canvas(projectKey);
      setCards(data.cards || []);
      setSummary((data as any).summary || null);
    } catch (err) {
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

  useEffect(() => { fetchCanvas(); }, [projectKey]);

  const handleRetryImage = async (card: any) => {
    const key = `${card.shot_id}:image`;
    setBusy(key);
    setShotError('');
    setNotice('');
    try {
      await storyboardApi.retryShot({ project_name: projectKey, shot_id: String(card.shot_id) });
      setNotice(`镜头 #${card.seq} 分镜图已重做`);
      await fetchCanvas();
    } catch (e) {
      setShotError(e instanceof Error ? e.message : '分镜图重做失败');
    } finally {
      setBusy(null);
    }
  };

  const handleRetryVideo = async (card: any) => {
    const key = `${card.shot_id}:video`;
    const mode = videoMode[String(card.shot_id)] || 'reference';
    if (mode === 'keyframe' && !card.keyframe?.end_exists) {
      setShotError(`镜头 #${card.seq} 没有尾帧，无法用「首尾帧插值」重做；请先到「关键帧」子标签生成尾帧`);
      return;
    }
    setBusy(key);
    setShotError('');
    setNotice('');
    try {
      const r = await videoApi.retryShot({
        project_name: projectKey,
        shot_id: card.shot_id,
        mode,
      });
      setNotice(
        `镜头 #${card.seq} 视频已重做（${r.mode === 'keyframe' ? '首尾帧插值' : '分镜图驱动'}，参考图 ${r.ref_count} 张，${r.duration}s）` +
        (r.deliverable_marked_stale ? '；该集成片已过期，请到「成品验收」前先重新混音合成' : '')
      );
      await fetchCanvas();
    } catch (e) {
      setShotError(e instanceof Error ? e.message : '视频重做失败');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold">分镜管理</h3>
        <Button size="sm" onClick={fetchCanvas} disabled={loading}>刷新</Button>
      </div>

      {summary && (
        <div className="flex flex-wrap gap-4 text-sm text-gray-600">
          <span>共 {summary.shot_count} 镜</span>
          <span>分镜图 {summary.storyboard_ready ?? 0}</span>
          <span>视频 {summary.video_ready ?? 0}</span>
          <span>尾帧 {summary.keyframe_end_ready ?? 0}</span>
          {(summary.qc_blocked ?? 0) > 0 && (
            <span className="text-amber-600">质检拦截 {summary.qc_blocked}</span>
          )}
        </div>
      )}

      {notice && (
        <div className="p-3 bg-green-500/10 border border-green-500/30 rounded-lg text-green-600 text-sm">
          {notice}
        </div>
      )}
      {shotError && (
        <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
          {shotError}
        </div>
      )}

      {error === 'no-data' && (
        <div className="py-12 text-center text-gray-500">
          <div className="text-4xl mb-3">🎬</div>
          <p>暂无分镜数据，请先进行剧本生成</p>
        </div>
      )}

      {error && error !== 'no-data' && (
        <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">{error}</div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {cards.map((card: any) => {
          const sid = String(card.shot_id);
          const imgBusy = busy === `${card.shot_id}:image`;
          const vidBusy = busy === `${card.shot_id}:video`;
          const mode = videoMode[sid] || 'reference';
          return (
            <div key={card.seq} className="bg-white rounded-lg border border-gray-200 p-4 flex flex-col">
              <div className="flex items-center justify-between mb-2">
                <span className="font-mono text-sm text-gray-500">#{card.seq}</span>
                <span className="text-xs text-gray-500">{card.camera}</span>
              </div>

              <div className="flex gap-3 mb-2">
                <div className="w-24 h-24 shrink-0 rounded bg-gray-100 border border-gray-200 overflow-hidden flex items-center justify-center text-xs text-gray-400">
                  {card.storyboard?.exists && card.storyboard?.url ? (
                    <img src={card.storyboard.url} alt={`镜头 ${card.seq} 分镜图`} className="w-full h-full object-cover" />
                  ) : (
                    <span>无分镜图</span>
                  )}
                </div>
                <div className="min-w-0 flex-1 text-xs space-y-1">
                  <p className="text-gray-700 line-clamp-3">{card.description}</p>
                  {card.dialogue_text && (
                    <p className="text-gray-500 italic line-clamp-2">{card.dialogue_text}</p>
                  )}
                  <p className={card.video?.exists ? 'text-green-600' : 'text-amber-600'}>
                    视频：{card.video?.exists ? '已生成' : '未生成'}
                  </p>
                  {card.consistency?.score != null && (
                    <p className="text-gray-500">一致性：{card.consistency.score}</p>
                  )}
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2 mt-auto pt-2">
                {card.video?.exists && card.video?.url && (
                  <a
                    href={card.video.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-brand hover:text-brand"
                  >
                    查看视频
                  </a>
                )}
                <select
                  value={mode}
                  onChange={(e) =>
                    setVideoMode((prev) => ({ ...prev, [sid]: e.target.value as 'reference' | 'keyframe' }))
                  }
                  className="text-xs rounded border border-gray-300 bg-white text-gray-700 px-1 py-1"
                  title="reference：用分镜图+主角锚点生成；keyframe：用首尾帧插值（需已有尾帧）"
                >
                  <option value="reference">分镜图驱动</option>
                  <option value="keyframe">首尾帧插值</option>
                </select>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => handleRetryImage(card)}
                  disabled={!!busy}
                >
                  {imgBusy ? '重做中…' : '重做分镜图'}
                </Button>
                <Button
                  size="sm"
                  onClick={() => handleRetryVideo(card)}
                  disabled={!!busy}
                >
                  {vidBusy ? '重做中…' : '重做视频'}
                </Button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ========== 超分（FlashVSR） ==========
// 接口：
// GET /api/upscale/env 链路自检（ComfyUI 在线 / 模型 / 节点）
// GET /api/upscale/sources 候选输入视频（成片 / 片段 / 已有超分 / ComfyUI）
// POST /api/upscale/video {project_name, video_path, scale, attach_audio} -> task_id
// GET /api/upscale/status/<id> 轮询进度
// GET /api/upscale/list 该项目已生成的超分产物
//
// ⚠️ attach_audio 必须传 true：后端 TE-Speed 链路默认 attach_audio=False，
// 对「成片」超分会把已合成的 TTS 配音整轨丢掉（backend 侧该参数此前也不在白名单，
// 已一并补上）。
//
// ⚠️ 可下载性取决于 URL 前缀：只有 /api/upscale/<project>/<name> 支持 ?download=1；
// ComfyUI 侧来源走 /api/upscale/comfyview 是 302 重定向，不能直接当附件下载。
function UpscaleTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const [env, setEnv] = useState<UpscaleEnv | null>(null);
  const [sources, setSources] = useState<UpscaleSource[]>([]);
  const [artifacts, setArtifacts] = useState<UpscaleArtifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('');
  const [scale, setScale] = useState<2 | 3 | 4>(2);
  const [task, setTask] = useState<UpscaleTask | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [playing, setPlaying] = useState<string | null>(null);
  /**
   * 是否把 ComfyUI 侧素材也列进候选。
   *
   * 后端 /api/upscale/sources 会把 **全局** COMFYUI_OUTPUT_DIR 下所有 mp4 都返回
   * （不分项目，实测该项目能列出 300 条），混进下拉框会把本项目的成片淹没，
   * 原生 select 里 300+ 选项也几乎没法选。故默认只显示项目自己的产物。
   */
  const [showComfy, setShowComfy] = useState(false);
  /** 自动生产是否带超分（项目级计划，存于 autopilot plan） */
  const [planOn, setPlanOn] = useState<boolean | null>(null);
  const [planScale, setPlanScale] = useState<2 | 3 | 4>(2);
  const [savingPlan, setSavingPlan] = useState(false);
  /** 轮询定时器句柄（卸载/切任务时必须清掉，否则会一直打后端） */
  const pollRef = React.useRef<number | null>(null);

  const stopPoll = React.useCallback(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const loadArtifacts = React.useCallback(async () => {
    try {
      const d = await upscaleApi.list(projectKey);
      setArtifacts(d.items || []);
    } catch {
      setArtifacts([]);
    }
  }, [projectKey]);

  const load = React.useCallback(async () => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      // env 失败不视为致命：把原因展示出来比整页报错更有用
      const [envRes, srcRes, planRes] = await Promise.allSettled([
        upscaleApi.env(),
        upscaleApi.sources(projectKey),
        autopilotApi.plan(projectKey),
      ]);
      if (envRes.status === 'fulfilled') setEnv(envRes.value);
      else setEnv(null);
      if (planRes.status === 'fulfilled') {
        const pl = (planRes.value?.plan || {}) as Record<string, unknown>;
        setPlanOn(pl.enable_upscale !== false);
        const s = Number(pl.upscale_scale);
        setPlanScale(s === 3 || s === 4 ? (s as 3 | 4) : 2);
      } else {
        setPlanOn(null);
      }
      if (srcRes.status === 'fulfilled') {
        const items = srcRes.value.items || [];
        setSources(items);
        // 默认优先选「成片」（自动生产刚出的成品），省掉一次手动选择
        setSelected((prev) => prev || (items.find((i) => i.kind === '成片') || items[0])?.path || '');
      } else {
        setError(srcRes.reason instanceof Error ? srcRes.reason.message : t('upscale.loadFailed'));
        setSources([]);
      }
      await loadArtifacts();
    } finally {
      setLoading(false);
    }
  }, [projectKey, loadArtifacts, t]);

  useEffect(() => { void load(); }, [load]);

  // 组件卸载时停掉轮询
  useEffect(() => () => stopPoll(), [stopPoll]);

  /** 开始轮询某个任务；done/error 时自动停机并刷新产物列表 */
  const startPoll = React.useCallback((taskId: string) => {
    stopPoll();
    pollRef.current = window.setInterval(async () => {
      try {
        const st = await upscaleApi.status(taskId);
        setTask(st);
        if (st.status === 'done' || st.status === 'error') {
          stopPoll();
          if (st.status === 'done') await loadArtifacts();
        }
      } catch (e) {
        stopPoll();
        setError(e instanceof Error ? e.message : t('upscale.statusFailed'));
      }
    }, 2500);
  }, [stopPoll, loadArtifacts, t]);

  const submit = async () => {
    if (!selected) return;
    setSubmitting(true);
    setError('');
    setPlaying(null);
    try {
      const res = await upscaleApi.submit({
        project_name: projectKey,
        video_path: selected,
        scale,
        // 成片含配音，必须保留音轨
        attach_audio: true,
      });
      setTask({ task_id: res.task_id, status: 'pending', progress: 0, message: '' });
      startPoll(res.task_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('upscale.submitFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  /** 保存「自动生产时是否带超分」到项目计划（后端只认 PLAN_DEFAULTS 里的字段） */
  const savePlan = async (patch: { enable_upscale?: boolean; upscale_scale?: 2 | 3 | 4 }) => {
    setSavingPlan(true);
    setError('');
    try {
      const res = await autopilotApi.setPlan(projectKey, patch);
      const pl = (res?.plan || {}) as Record<string, unknown>;
      setPlanOn(pl.enable_upscale !== false);
      const s = Number(pl.upscale_scale);
      setPlanScale(s === 3 || s === 4 ? (s as 3 | 4) : 2);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('upscale.planSaveFailed'));
    } finally {
      setSavingPlan(false);
    }
  };

  if (loading) return <Loading />;

  const ready = !!env?.available;
  const busy = task?.status === 'pending' || task?.status === 'running';
  /** 本项目自身产物（成片 / 片段 / 已有超分）；ComfyUI 侧是全局素材池，默认折叠 */
  const visibleSources = showComfy
    ? sources
    : sources.filter((s) => !s.kind.startsWith('ComfyUI'));
  const source = sources.find((s) => s.path === selected);
  const srcUrl = source?.url || '';
  // 仅 /api/upscale/<project>/<name> 支持 ?download=1（comfyview 是 302，不能当附件）
  const downloadUrl = (u?: string) =>
    u && u.startsWith('/api/upscale/') && !u.includes('comfyview') ? `${u}?download=1` : '';

  const statusText = () => {
    if (!task) return '';
    if (task.status === 'pending') return t('upscale.queued');
    if (task.status === 'running') return t('upscale.running');
    if (task.status === 'done') return t('upscale.done');
    return t('upscale.failed');
  };

  const res = task?.result;
  const fmtResolution = (v?: { width?: number; height?: number }) =>
    v?.width && v?.height ? `${v.width}×${v.height}` : '—';

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-lg font-semibold text-gray-900">{t('upscale.title')}</h3>
          <p className="text-sm text-gray-500 mt-0.5">{t('upscale.subtitle')}</p>
        </div>
        <Button size="sm" variant="secondary" onClick={load} disabled={loading || busy}>
          {t('common.refresh')}
        </Button>
      </div>

      {/* 链路自检 */}
      <div
        className={`p-3 rounded-lg border text-sm ${
          ready
            ? 'bg-green-50 border-green-200 text-green-700'
            : 'bg-amber-50 border-amber-200 text-amber-700'
        }`}
      >
        <div className="flex items-center justify-between gap-3">
          <span className="font-medium">
            {ready ? t('upscale.envOk') : t('upscale.envBad')}
          </span>
          <span className="text-xs">
            {t('upscale.engine')}:{' '}
            {env?.default_engine === 'legacy-flashvsr'
              ? t('upscale.engineLegacy')
              : t('upscale.engineTe')}
          </span>
        </div>
        {env && (
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
            <span>{t('upscale.comfyOnline')}: {env.comfy_online ? '✓' : '✗'}</span>
            <span>{t('upscale.modelReady')}: {env.model_ready ? '✓' : '✗'}</span>
            <span>{t('upscale.teReady')}: {env.te_ready ? '✓' : '✗'}</span>
            <span>{t('upscale.legacyReady')}: {env.legacy_ready ? '✓' : '✗'}</span>
          </div>
        )}
        {!ready && (env?.reasons?.length ?? 0) > 0 && (
          <div className="mt-2">
            <p className="text-xs opacity-90">{t('upscale.envHint')}</p>
            <ul className="mt-1 list-disc list-inside text-xs opacity-90 space-y-0.5">
              {(env?.reasons || []).map((r) => <li key={r}>{r}</li>)}
            </ul>
          </div>
        )}
        {/* 说明流水线默认开启超分且失败即跳过，避免用户以为「没超分 = 坏了」 */}
        <p className="mt-2 text-xs opacity-80">{t('upscale.pipelineTip')}</p>
      </div>

      {/* 自动生产是否带超分 —— 超分默认开启，且单集耗时会明显变长，
          必须给一个真正的关闭入口（之前 enable_upscale 不在 PLAN_DEFAULTS 里，
          接口会把该字段过滤掉，等于关不掉）。 */}
      {planOn !== null && (
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="text-sm font-medium text-gray-900">
                {t('upscale.planToggle')}
              </p>
              <p className="text-xs text-gray-500 mt-1">
                {t('upscale.planToggleHint')}
              </p>
            </div>
            <div className="flex items-center gap-3 shrink-0">
              <span className={`text-xs font-medium ${planOn ? 'text-green-600' : 'text-gray-500'}`}>
                {planOn ? t('upscale.on') : t('upscale.off')}
              </span>
              <button
                role="switch"
                aria-checked={planOn}
                disabled={savingPlan}
                onClick={() => savePlan({ enable_upscale: !planOn })}
                className={`relative w-11 h-6 rounded-full transition-colors disabled:opacity-50 ${
                  planOn ? 'bg-brand' : 'bg-gray-300'
                }`}
              >
                <span
                  className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
                    planOn ? 'translate-x-5' : ''
                  }`}
                />
              </button>
            </div>
          </div>

          {/* 自动生产的超分倍率（与手工超分独立配置） */}
          <div className="mt-3 pt-3 border-t border-gray-100 flex items-center gap-3">
            <span className="text-xs text-gray-600">{t('upscale.planScale')}</span>
            <div className="flex gap-2">
              {([2, 3, 4] as const).map((n) => (
                <button
                  key={n}
                  disabled={savingPlan || !planOn}
                  onClick={() => savePlan({ upscale_scale: n })}
                  className={`px-3 py-1 rounded-md text-xs font-medium transition-all disabled:opacity-50 ${
                    planScale === n
                      ? 'bg-brand text-white'
                      : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                  }`}
                >
                  {t('upscale.scaleTimes', { n })}
                </button>
              ))}
            </div>
            <span className="text-xs text-gray-400">{t('upscale.planScaleHint')}</span>
          </div>
        </div>
      )}

      {error && (
        <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
          {error}
        </div>
      )}

      {/* 选择源 + 倍率 + 发起 */}
      <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
        {sources.length === 0 ? (
          <div className="text-center py-8">
            <div className="text-4xl mb-3">🔍</div>
            <h4 className="text-base font-medium text-gray-900 mb-1">{t('upscale.sourceEmpty')}</h4>
            <p className="text-sm text-gray-500">{t('upscale.sourceEmptyTip')}</p>
          </div>
        ) : (
          <>
            <div className="grid md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1.5">
                  {t('upscale.selectSource')}
                </label>
                <select
                  value={selected}
                  onChange={(e) => { setSelected(e.target.value); setPlaying(null); }}
                  disabled={busy}
                  className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg bg-white text-gray-900"
                >
                  {Array.from(new Set(visibleSources.map((s) => s.kind))).map((kind) => (
                    <optgroup key={kind} label={kind}>
                      {visibleSources.filter((s) => s.kind === kind).map((s) => (
                        <option key={s.path} value={s.path}>
                          {s.name} · {s.size_mb} MB
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
                {/* ComfyUI 侧是全局素材池（不分项目），默认折叠避免淹没本项目成片 */}
                <label className="mt-2 flex items-center gap-2 text-xs text-gray-500 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={showComfy}
                    onChange={(e) => setShowComfy(e.target.checked)}
                    className="rounded border-gray-300"
                  />
                  {t('upscale.showComfy', {
                    n: sources.length - sources.filter((s) => !s.kind.startsWith('ComfyUI')).length,
                  })}
                </label>
                {source && (
                  <p className="text-xs text-gray-500 mt-1.5">
                    {source.mtime} · {source.size_mb} MB
                  </p>
                )}
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1.5">
                  {t('upscale.scale')}
                </label>
                <div className="flex gap-2">
                  {([2, 3, 4] as const).map((n) => (
                    <button
                      key={n}
                      onClick={() => setScale(n)}
                      disabled={busy}
                      className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                        scale === n
                          ? 'bg-brand text-white shadow-lg'
                          : 'bg-gray-100 text-gray-600 hover:bg-gray-200 hover:text-gray-900'
                      }`}
                    >
                      {t('upscale.scaleTimes', { n })}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-gray-500 mt-1.5">
                  {t('upscale.scaleHint')}
                </p>
              </div>
            </div>

            {/* 源预览：确认选中的是哪一个视频 */}
            {srcUrl && playing === 'source' && (
              <video src={srcUrl} controls className="w-full rounded-lg bg-black" />
            )}

            <div className="flex gap-2 flex-wrap">
              <Button onClick={submit} disabled={!ready || !selected || submitting || busy}>
                {busy ? t('upscale.running') : t('upscale.start')}
              </Button>
              {srcUrl && (
                <Button
                  variant="secondary"
                  onClick={() => setPlaying(playing === 'source' ? null : 'source')}
                >
                  {playing === 'source' ? t('common.close') : t('upscale.preview')}
                </Button>
              )}
              {!ready && (
                <span className="text-xs text-amber-600 self-center">
                  {t('upscale.notReadyTip')}
                </span>
              )}
            </div>
          </>
        )}
      </div>

      {/* 任务进度 / 结果 */}
      {task && (
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
          <div className="flex items-center justify-between gap-3">
            <span className="font-semibold text-gray-900">{statusText()}</span>
            <span className="text-xs text-gray-500">
              {t('upscale.progress')} {task.progress || 0}%
            </span>
          </div>

          <div className="h-2 rounded-full bg-gray-100 overflow-hidden">
            <div
              className={`h-full transition-all ${
                task.status === 'error' ? 'bg-red-500' : 'bg-brand'
              }`}
              style={{ width: `${Math.min(100, task.progress || 0)}%` }}
            />
          </div>

          {task.message && (
            <p className="text-xs text-gray-500">{task.message}</p>
          )}
          {task.status === 'error' && task.error && (
            <p className="text-xs text-red-600 break-all">{task.error}</p>
          )}

          {task.status === 'done' && res && (
            <div className="pt-3 border-t border-gray-200 space-y-3">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                <div>
                  <div className="text-gray-500">{t('upscale.before')}</div>
                  <div className="font-medium text-gray-900">{fmtResolution(res.before)}</div>
                </div>
                <div>
                  <div className="text-gray-500">{t('upscale.after')}</div>
                  <div className="font-medium text-gray-900">{fmtResolution(res.after)}</div>
                </div>
                <div>
                  <div className="text-gray-500">{t('upscale.elapsed')}</div>
                  <div className="font-medium text-gray-900">
                    {res.elapsed_sec != null ? `${res.elapsed_sec}s` : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-gray-500">{t('upscale.resolution')}</div>
                  <div className="font-medium text-gray-900">
                    {res.after?.size_mb != null ? `${res.after.size_mb} MB` : '—'}
                  </div>
                </div>
              </div>

              {/* 音轨保留情况：无声超分是这里最容易踩的坑 */}
              <p className={`text-xs ${res.after?.has_audio ? 'text-green-600' : 'text-amber-600'}`}>
                {res.after?.has_audio ? t('upscale.audioKept') : t('upscale.audioLost')}
              </p>

              {res.output_url && (
                <>
                  <video src={res.output_url} controls className="w-full rounded-lg bg-black" />
                  <div className="flex gap-2">
                    <a
                      href={downloadUrl(res.output_url) || res.output_url}
                      className="inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 transition-colors"
                    >
                      {t('common.download')}
                    </a>
                    <span className="text-xs text-gray-500 self-center">
                      {t('upscale.confirmClose')}
                    </span>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      )}

      {/* 已生成的超分产物 */}
      {artifacts.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <h4 className="text-sm font-semibold text-gray-900 mb-3">
            {t('upscale.artifacts')}（{artifacts.length}）
          </h4>
          <div className="space-y-2">
            {artifacts.map((a) => (
              <div
                key={a.name}
                className="flex items-center justify-between gap-3 py-2 border-b border-gray-100 last:border-0"
              >
                <div className="min-w-0">
                  <p className="text-sm text-gray-900 truncate">{a.name}</p>
                  <p className="text-xs text-gray-500">{a.mtime} · {a.size_mb} MB</p>
                </div>
                <div className="flex gap-2 shrink-0">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => setPlaying(playing === a.name ? null : a.name)}
                  >
                    {playing === a.name ? t('common.close') : t('upscale.preview')}
                  </Button>
                  {downloadUrl(a.url) && (
                    <a
                      href={downloadUrl(a.url)}
                      className="inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 transition-colors"
                    >
                      {t('common.download')}
                    </a>
                  )}
                </div>
              </div>
            ))}
          </div>
          {playing && artifacts.some((a) => a.name === playing) && (
            <video
              src={artifacts.find((a) => a.name === playing)?.url}
              controls
              className="w-full mt-3 rounded-lg bg-black"
            />
          )}
        </div>
      )}
    </div>
  );
}

// ========== AI总控（项目内右侧常驻面板） ==========
// 原先它是工作台里的第 10 个标签页，排在最后、还会换行，用户反馈「进去后找不到了」。
// 现改为右侧常驻、可折叠：与标签内容并排，切换标签页时对话不丢失。
function ChatPanel({ projectKey, onClose }: { projectKey: string; onClose: () => void }) {
  const [messages, setMessages] = useState<any[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  // 自主执行模式：默认开启。指令交给总控模型自己决策并调用工具，全过程无需人工确认。
  const [autoMode, setAutoMode] = useState(true);
  const [run, setRun] = useState<{ steps: AgentStep[]; status: string } | null>(null);
  const [toolCount, setToolCount] = useState(0);
  const [killOn, setKillOn] = useState(false);
  const messagesEndRef = React.useRef<HTMLDivElement>(null);

  // 加载该项目的历史对话
  const loadHistory = async () => {
    try {
      const d = await chatApi.history(projectKey);
      setMessages(d.messages || []);
    } catch (err) {
      console.error('加载对话历史失败:', err);
    }
  };

  useEffect(() => { loadHistory(); }, [projectKey]);

  // 拉取总控可用工具数与急停状态（失败不影响对话，静默降级）
  useEffect(() => {
    agentApi.tools()
      .then((d) => { setToolCount(d.count || 0); setKillOn(!!d.kill?.on); })
      .catch(() => {});
  }, []);

  const toggleKill = async () => {
    try {
      const d = await agentApi.setKill(!killOn, !killOn ? '前端手动急停' : '');
      setKillOn(!!d.kill?.on);
    } catch (err) {
      setError(err instanceof Error ? err.message : '急停失败');
    }
  };

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setSending(true);
    setError('');
    // 乐观渲染用户消息
    setMessages(prev => [...prev, { role: 'user', content: text, timestamp: new Date().toISOString() }]);
    setInput('');
    try {
      // 纯聊天模式：走老链路，只做对话 + 抽取创作设定
      if (!autoMode) {
        const data = await chatApi.send(text, projectKey);
        if (data.success && data.reply) {
          setMessages(prev => [...prev, { role: 'assistant', content: data.reply, timestamp: new Date().toISOString() }]);
        } else {
          setError('AI 未返回内容');
        }
        return;
      }

      // 自主执行模式：下发任务 → 轮询 → 逐步展示「它自己做了什么」
      const started = await agentApi.send(text, projectKey);
      if (!started.success || !started.job_id) {
        setError('总控未能启动任务');
        return;
      }
      setRun({ steps: [], status: 'running' });
      const deadline = Date.now() + 35 * 60 * 1000; // 兜底，避免异常时永久轮询
      for (;;) {
        await new Promise(r => setTimeout(r, 1200));
        const job = await agentApi.job(started.job_id);
        setRun({ steps: job.steps || [], status: job.status || 'running' });
        if (job.status !== 'running') {
          if (job.reply) {
            setMessages(prev => [...prev, { role: 'assistant', content: job.reply, timestamp: new Date().toISOString() }]);
          } else if (job.error) {
            setError(job.error);
          }
          break;
        }
        if (Date.now() > deadline) { setError('总控执行超时（已超过 35 分钟）'); break; }
      }
      setRun(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : '发送失败');
      setRun(null);
    } finally {
      setSending(false);
    }
  };

  return (
    <aside
      // h-[calc(100vh-7rem)] = 视口高 −（顶栏 ~63px + main 上下 padding 48px），
      // 让面板与左列内容等高、上下贯通；sticky 使其随页面滚动保持停靠。
      className="w-[340px] shrink-0 flex flex-col rounded-xl border border-gray-200 bg-white overflow-hidden sticky top-0 h-[calc(100vh-7rem)] min-h-[420px]"
    >
      {/* 头部：与工作台其他面板一致的白底 + 灰边 + indigo 强调 */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="w-7 h-7 rounded-lg bg-brand-subtle flex items-center justify-center text-sm shrink-0">
            💬
          </span>
          <div className="min-w-0">
            <h3 className="font-semibold text-gray-900 leading-tight">AI总控</h3>
            <div className="flex items-center gap-1.5 mt-0.5">
              <p className="text-[11px] text-gray-500 leading-tight">
                {autoMode ? `自主执行 · ${toolCount || '…'} 个功能` : '仅对话'}
              </p>
              <button
                onClick={() => setAutoMode(v => !v)}
                title={autoMode ? '切回纯聊天（不执行动作）' : '切到自主执行（总控自己干活）'}
                className={`text-[10px] leading-none px-1.5 py-0.5 rounded border transition-colors ${
                  autoMode
                    ? 'border-brand/30 text-brand bg-brand-subtle'
                    : 'border-gray-300 text-gray-500'
                }`}
              >
                {autoMode ? '自主' : '聊天'}
              </button>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-0.5 shrink-0">
          {autoMode && (
            <button
              onClick={toggleKill}
              title={killOn ? '解除急停' : '急停：立即中止总控的一切动作'}
              className={`p-1.5 rounded-lg transition-colors ${
                killOn
                  ? 'text-red-600 bg-red-50'
                  : 'text-gray-500 hover:text-red-600 hover:bg-red-50'
              }`}
            >
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
            </button>
          )}
          <button
            onClick={loadHistory}
            title="刷新对话"
            className="p-1.5 rounded-lg text-gray-500 hover:text-brand hover:bg-brand-subtle transition-colors"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
          <button
            onClick={onClose}
            title="收起面板"
            className="p-1.5 rounded-lg text-gray-500 hover:text-brand hover:bg-brand-subtle transition-colors"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 5l7 7-7 7M5 5l7 7-7 7" />
            </svg>
          </button>
        </div>
      </div>

      {error && (
        <div className="mx-3 mt-3 p-2 bg-red-500/10 border border-red-500/30 rounded-lg text-red-500 text-xs shrink-0">
          {error}
        </div>
      )}

      {/* 消息区：浅色底以区别于面板头部/输入区，形成「对话」区域感。
          注意：空态与消息列表要二选一渲染 —— 若把滚动哨兵 <div> 和 h-full 的空态
          放在同一个 space-y-3 容器里，哨兵会额外吃到 12px margin 而撑出滚动条。 */}
      <div className="flex-1 min-h-0 overflow-y-auto bg-gray-50">
        {messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center px-4">
            <div className="w-11 h-11 rounded-xl bg-white border border-gray-200 flex items-center justify-center text-lg mb-3">
              💬
            </div>
            <p className="text-sm text-gray-600">
              {autoMode ? '说一句话，总控自己决定并执行' : '和总控聊聊创作想法'}
            </p>
            <p className="text-xs text-gray-500 mt-1 mb-4">
              {autoMode ? '无需确认，它会直接动手；点右上角方块可随时急停' : '当前只聊天，不会改动任何产物'}
            </p>
            <div className="w-full space-y-1.5">
              {(autoMode
                ? ['看看现在生产到哪了', '把第 3 镜重新生成一次', '把最新成片做 2 倍超分', '这一集节奏太慢，重新调整分镜']
                : ['这一集节奏太慢，帮我调整分镜', '主角的服装换成深蓝色']
              ).map((ex) => (
                <button
                  key={ex}
                  onClick={() => setInput(ex)}
                  className="w-full text-left text-xs px-3 py-2 rounded-lg bg-white border border-gray-200 text-gray-500 hover:border-brand hover:text-brand transition-colors"
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="p-3 space-y-3">
            {messages.map((msg: any, idx: number) => (
              <div
                key={idx}
                className={`px-3 py-2 rounded-lg text-sm ${
                  msg.role === 'user'
                    ? 'bg-brand text-white ml-6 rounded-br-sm'
                    : 'bg-white border border-gray-200 text-gray-700 mr-6 rounded-bl-sm'
                }`}
              >
                <p className="whitespace-pre-wrap break-words">{msg.content}</p>
              </div>
            ))}
            {/* 自主执行过程：把总控「自己调了哪些功能、成功没有」透明地摊开 */}
            {run && (
              <div className="mr-6 px-3 py-2 rounded-lg border border-gray-200 bg-white space-y-1.5">
                <div className="flex items-center gap-2 text-xs text-gray-500">
                  {run.status === 'running' ? (
                    <span className="w-3 h-3 border-2 border-brand/40 border-t-brand rounded-full animate-spin inline-block shrink-0" />
                  ) : (
                    <span className="w-1.5 h-1.5 rounded-full bg-gray-400 inline-block shrink-0" />
                  )}
                  总控执行中 · 已完成 {run.steps.length} 步
                </div>
                {run.steps.map((s: AgentStep, i: number) => (
                  <div key={i} className="flex items-start gap-1.5 text-[11px] leading-snug">
                    <span className={`shrink-0 ${s.blocked ? 'text-amber-500' : s.ok ? 'text-green-500' : 'text-red-500'}`}>
                      {s.blocked ? '⊘' : s.ok ? '✓' : '✕'}
                    </span>
                    <span className="font-mono text-gray-500 shrink-0">{s.tool}</span>
                    <span className="text-gray-600 break-all">
                      {s.summary}
                      {s.cached ? '（复用缓存）' : ''}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {sending && !run && (
              <div className="bg-white border border-gray-200 mr-6 px-3 py-2 rounded-lg text-sm text-gray-500 flex items-center gap-2">
                <span className="w-3 h-3 border-2 border-brand/40 border-t-brand rounded-full animate-spin inline-block" />
                思考中...
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* 输入区 */}
      <div className="p-3 border-t border-gray-200 shrink-0 bg-white">
        <div className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') sendMessage(); }}
            placeholder="输入消息..."
            className="flex-1 min-w-0 px-3 py-2 text-sm rounded-lg border border-gray-200 bg-white text-gray-900 placeholder-gray-400 focus:ring-2 focus:ring-brand/30 focus:border-brand outline-none transition-colors"
          />
          <button
            onClick={sendMessage}
            disabled={sending || !input.trim()}
            className="px-3 py-2 rounded-lg text-sm font-medium bg-brand text-white hover:bg-brand-hover disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
          >
            发送
          </button>
        </div>
      </div>
    </aside>
  );
}
