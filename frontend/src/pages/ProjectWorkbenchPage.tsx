import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { t } from '@/i18n';
import { projectsApi, keyframesApi, storyboardApi, videoApi, ttsApi, mixApi, qcApi, exportApi, autopilotApi, upscaleApi, chatApi, agentApi, episodesApi } from '@/api/client';
import { Button, Input, EmptyState, ErrorState, Skeleton, Modal } from '@/components/ui';
// tab 图标統一走线性 SVG（方案 P2-10）：此前是 emoji，字号受系统字体影响且观感与全站割裂
import {
  AlertTriangle, BarChart3, Box, Check, CheckCircle2, Clapperboard, ClipboardCheck, ClipboardList, FileText,
  FolderOpen, ImageIcon, MessageSquare, Mountain, Music, Network, Share2, Target, User, X, ZoomIn,
} from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';
import { GridPage } from '@/pages/GridPage';
import { RelationGraphTab } from '@/components/RelationGraphTab';
import { OutputReviewTab } from '@/components/OutputReviewTab';
import { AudioTab } from '@/components/AudioTab';
import type { Project, Deliverable, UpscaleEnv, UpscaleSource, UpscaleTask, UpscaleArtifact, AgentStep, AutopilotCurrent } from '@/types';

// 焦点环：与 components/ui/index.tsx 里的 FOCUS_RING 逐字一致。
// index.css 有全局 :focus-visible outline 兜底，这里显式加 focus:outline-none 把它压掉，
// 否则 outline + ring 会叠成双环。凡因形状/类型原因换不成共享组件的原生控件，统一补这一串。
const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

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

  if (loading) return (
    // 骨架沿用真实内容的外层布局（左列 + 右侧常驻面板），避免「白屏 → 内容」的高度跳变
    <div className="fade-in" role="status" aria-live="polite" aria-label={t('common.loading')}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <div className="flex-1 min-w-0 space-y-4">
          <div className="flex items-center justify-between">
            <div className="min-w-0 space-y-2">
              <Skeleton className="h-7 w-48" />
              <Skeleton className="h-4 w-64" />
            </div>
            <Skeleton className="h-9 w-32" />
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-20 rounded-lg" />
            ))}
          </div>
          <div className="flex flex-wrap gap-2 border-b border-line pb-4">
            {[0, 1, 2, 3, 4, 5, 6].map((i) => (
              <Skeleton key={i} className="h-9 w-24 rounded-lg" />
            ))}
          </div>
          <Skeleton className="h-64 rounded-lg" />
        </div>
        <Skeleton className="min-h-[420px] w-full rounded-xl lg:h-[calc(100vh-7rem)] lg:w-[340px] lg:shrink-0" />
      </div>
    </div>
  );
  if (!project) return (
    <EmptyState
      icon={<AlertTriangle className="h-10 w-10" />}
      title={t('wb.projectNotFound')}
      description={projectKey}
    />
  );

  return (
    <div className="fade-in">
      {/* 主体：左列（项目头 + 统计 + 标签内容） + 右列「AI总控」常驻面板。
          头部与统计放进左列，右侧面板才能从顶部一直贯通到底部，不会变成悬空小盒。
          ⚠️ < lg 时改为上下堆叠：面板固定 340px，375 视口扣掉侧边栏后只剩 311px，
          横排必然把页面撑出横向滚动条。 */}
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <div className="flex-1 min-w-0 space-y-4">
          {/* Header */}
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-2xl font-bold text-ink-1">{project.name}</h2>
              <p className="text-sm text-ink-2 mt-1">
                {t('project.style')}: {project.config?.style} • {project.episode_count} {t('ep.suffix')}
              </p>
            </div>
            <Button
              variant="secondary"
              onClick={() => { window.location.hash = '#/projects'; }}
            >
              ← {t('wb.backToProjects')}
            </Button>
          </div>

          {/* Stats Bar —— 窄屏折成两行，避免 4 列挤压成一竖条（方案 P1-8） */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            {[
              { label: t('wb.characters'), count: assets?.counts?.characters || 0, color: 'text-brand' },
              { label: t('wb.items'), count: assets?.counts?.items || 0, color: 'text-success-strong' },
              { label: t('wb.scenes'), count: assets?.counts?.scenes || 0, color: 'text-warning-strong' },
              { label: t('wb.storyboard'), count: assets?.counts?.storyboards || 0, color: 'text-info-strong' },
            ].map((stat) => (
              <div key={stat.label} className="bg-surface rounded-lg p-4 border border-line">
                <div className={`text-2xl font-bold ${stat.color}`}>{stat.count}</div>
                <div className="text-sm text-ink-2">{stat.label}</div>
              </div>
            ))}
          </div>

          {/* Tab Navigation */}
          <div className="flex flex-wrap gap-2 border-b border-line pb-4">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${FOCUS_RING} ${
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
            title={t('wb.expandChat')}
            className={`sticky top-0 shrink-0 w-11 h-[calc(100vh-7rem)] min-h-[420px] flex flex-col items-center gap-3 py-4 rounded-xl border border-line bg-surface text-ink-2 hover:text-brand hover:border-brand transition-colors ${FOCUS_RING}`}
          >
            <span className="w-7 h-7 rounded-lg bg-brand-subtle flex items-center justify-center"><MessageSquare className="h-4 w-4" /></span>
            <span className="text-xs tracking-wide" style={{ writingMode: 'vertical-rl' }}>{t('wb.aiControl')}</span>
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
function sanitizeError(err: unknown, fallback = t('wb.actionFailed')): string {
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

// 生产流水线步骤序列：与后端 pipeline.STEP_SEQUENCE 对齐（技术标识符 → i18n 键）。
const PRODUCTION_STEPS: { id: string; labelKey: string }[] = [
  { id: 'script', labelKey: 'wb.stepScript' },
  { id: 'assets', labelKey: 'wb.stepAssets' },
  { id: 'storyboard', labelKey: 'wb.stepStoryboard' },
  { id: 'keyframe', labelKey: 'wb.stepKeyframe' },
  { id: 'video', labelKey: 'wb.stepVideo' },
  { id: 'final', labelKey: 'wb.stepFinal' },
  { id: 'tts', labelKey: 'wb.stepTts' },
  { id: 'mix', labelKey: 'wb.stepMix' },
  { id: 'upscale', labelKey: 'wb.stepUpscale' },
];

/** 生产进度卡片：轮询 /api/autopilot/status，把「当前正在生产哪一集、当前步骤、百分比、
 *  超时告警」实时展示出来。此前这些数据后端都有，但前端从未渲染 —— 用户生产时只能干等。
 *  （报告 P1-5「无进度反馈」+ 建议 3「生产过程可视化」的落地） */
function ProductionProgress({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const [cur, setCur] = useState<AutopilotCurrent | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setInterval> | null = null;
    const poll = async () => {
      try {
        const st = await autopilotApi.status(projectKey);
        if (!alive) return;
        setCur((st.current as AutopilotCurrent) || null);
      } catch {
        // 轮询失败静默：状态刷新是锦上添花，不能因一次失败打断整页
      }
    };
    poll();
    timer = setInterval(poll, 3000);
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
    };
  }, [projectKey]);

  if (!cur) return null;

  const percent = Math.max(0, Math.min(100, Number(cur.percent) || 0));
  const stepsDone = Array.isArray(cur.steps_done) ? cur.steps_done : [];
  const stalled = Number(cur.step_stalled_sec) || 0;
  const stalledMin = Math.floor(stalled / 60);
  const stalledSec = stalled % 60;
  const currentStepId = (cur.step || '').split(':')[0];
  const currentStep = PRODUCTION_STEPS.find(s => s.id === currentStepId);

  return (
    <div
      className="bg-surface rounded-lg border border-line p-4 mb-4"
      role="status"
      aria-live="polite"
      aria-label={t('wb.productionProgress')}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-semibold text-ink-1 flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-info animate-pulse" />
          {t('wb.productionProgress')}
        </span>
        {cur.episode != null && (
          <span className="text-sm text-ink-2">{t('wb.producingEpisode', { n: cur.episode })}</span>
        )}
      </div>

      {/* 进度条 */}
      <div className="w-full bg-line rounded-full h-2 mb-3">
        <div
          className="bg-brand h-2 rounded-full transition-all"
          style={{ width: `${percent}%` }}
        />
      </div>

      {/* 步骤链 */}
      <div className="flex flex-wrap items-center gap-1.5 mb-2">
        {PRODUCTION_STEPS.map((s, i) => {
          const done = stepsDone.includes(s.id);
          const active = s.id === currentStepId;
          return (
            <React.Fragment key={s.id}>
              {i > 0 && <span className="text-ink-3 text-xs">→</span>}
              <span
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs ${
                  done
                    ? 'bg-success-subtle text-success-strong'
                    : active
                      ? 'bg-brand-subtle text-brand font-medium'
                      : 'bg-surface-2 text-ink-3'
                }`}
              >
                {done && <Check className="h-3 w-3" />}
                {t(s.labelKey)}
              </span>
            </React.Fragment>
          );
        })}
      </div>

      {/* 当前步骤消息 + 超时告警 */}
      {cur.message && <p className="text-xs text-ink-2 mb-1">{cur.message}</p>}
      {stalled >= 900 && (
        <div className="flex items-start gap-2 mt-2 p-2.5 rounded bg-warning-subtle text-warning-strong text-xs">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
          <div>
            <p className="font-medium">
              {t('wb.currentStep')}
              {currentStep ? `：${t(currentStep.labelKey)}` : ''} ·{' '}
              {t('wb.stepStalled', { min: stalledMin, sec: stalledSec })}
            </p>
            <p className="text-ink-2 mt-0.5">{t('wb.stallHint')}</p>
          </div>
        </div>
      )}
    </div>
  );
}

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
  const { t } = useApp();
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
  /** 记下最近一次请求的集号：详情加载失败后 ErrorState 的「重试」要能定位回同一集 */
  const lastDetailEpRef = React.useRef<number | null>(null);

  // 加载剧集列表
  // 抽成具名函数：错误态需要「重试」入口，而 useEffect 无法被手动重新触发。
  // 取数逻辑与原实现逐字一致，仅补一次错误清理，避免重试成功后旧的失败文案残留。
  const fetchEpisodes = React.useCallback(() => {
    if (!novelId) return;
    setScriptLoading(true);
    setScriptError('');
    episodesApi.list(novelId)
      .then(data => {
        setEpisodes(data.episodes || []);
        setTotalEpisodes(data.total || 0);
      })
      .catch(err => {
        setScriptError(err instanceof Error ? err.message : t('project.loadingFailed'));
      })
      .finally(() => {
        setScriptLoading(false);
      });
  }, [novelId, t]);

  useEffect(() => { fetchEpisodes(); }, [fetchEpisodes]);

  // 加载单集详情
  // ⚠️ 后端 /api/episodes/<novel>/<ep> 的剧本正文嵌在 `script` 对象下（shots/characters/items/scenes），
  // 且列表行才带 status/completed_shots（_episode_progress 推导），详情接口本身不返回这两个字段。
  // 这里摊平成视图直接可读的结构，并从已加载的剧集列表补进度字段，避免详情恒显「暂无剧本内容」。
  const loadEpisodeDetail = async (episodeNo: number) => {
    if (!novelId) return;
    lastDetailEpRef.current = episodeNo;
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
      setDetailError(err instanceof Error ? err.message : t('wb.loadDetailFailed'));
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

  const groups: { key: 'characters' | 'items' | 'scenes'; label: string; icon: React.ReactNode; type: 'character' | 'item' | 'scene' }[] = [
    { key: 'characters', label: t('wb.characters'), icon: <User className="h-4 w-4" />, type: 'character' },
    { key: 'items', label: t('wb.items'), icon: <Box className="h-4 w-4" />, type: 'item' },
    { key: 'scenes', label: t('wb.scenes'), icon: <Mountain className="h-4 w-4" />, type: 'scene' },
  ];

  const total = groups.reduce((n, g) => n + (assets?.gallery?.[g.key]?.length || 0), 0);

  // 显示单集详情
  if (selectedEpisode !== null && episodeDetail && !detailError) {
    return (
      <div className="space-y-4">
        <Button variant="ghost" onClick={goBack}>
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
          </svg>
          {t('wb.backToList')}
        </Button>

        <div className="bg-surface rounded-lg border border-line p-6">
          <div className="flex items-start justify-between">
            <div>
              <h3 className="text-xl font-bold text-ink-1">
                {t('wb.episodeNo', { n: episodeDetail.episode_no })}
                {episodeDetail.title && <span className="ml-2 text-lg font-normal text-ink-2">{episodeDetail.title}</span>}
              </h3>
              {episodeDetail.chapter_title && (
                <p className="text-sm text-ink-2 mt-1">{t('wb.chapterLabel')}{episodeDetail.chapter_title}</p>
              )}
            </div>
            <span className={`px-3 py-1 rounded-full text-xs font-medium inline-flex items-center gap-1 ${
              episodeDetail.status === 'done' ? 'bg-success-subtle text-success-strong' :
              episodeDetail.status === 'producing' ? 'bg-info-subtle text-info-strong' :
              episodeDetail.status === 'failed' ? 'bg-danger-subtle text-danger-strong' :
              'bg-surface-2 text-ink-1'
            }`}>
              {episodeDetail.status === 'done' ? (<><Check className="h-3.5 w-3.5" /> {t('wb.done')}</>) :
               episodeDetail.status === 'producing' ? t('wb.producing') :
               episodeDetail.status === 'failed' ? (<><X className="h-3.5 w-3.5" /> {t('episodes.failed')}</>) :
               t('ep.pending')}
            </span>
          </div>

          <div className="mt-4 flex items-center gap-4 text-sm text-ink-2">
            <span>{t('wb.shotProgress', { done: episodeDetail.completed_shots, total: episodeDetail.shot_count })}</span>
            {episodeDetail.created_at && (
              <span>{t('wb.createdAt', { time: episodeDetail.created_at.split('T')[0] })}</span>
            )}
          </div>

          {episodeDetail.shot_count > 0 && (
            <div className="mt-3 w-full bg-line rounded-full h-2">
              <div
                className={`h-2 rounded-full transition-all ${
                  episodeDetail.status === 'done' ? 'bg-success' :
                  episodeDetail.status === 'failed' ? 'bg-danger' :
                  'bg-brand'
                }`}
                style={{ width: `${(episodeDetail.completed_shots / episodeDetail.shot_count) * 100}%` }}
              ></div>
            </div>
          )}
        </div>

        <div className="bg-surface rounded-lg border border-line p-6">
          <h4 className="font-semibold text-ink-1 mb-4">{t('wb.scriptContent')}</h4>

          {(episodeDetail.shots && episodeDetail.shots.length > 0) ? (
            <div className="space-y-4">
              {episodeDetail.shots.map((shot: any, idx: number) => (
                <div key={idx} className="border-l-4 border-brand pl-4 py-2">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    <span className="px-2 py-0.5 bg-brand-subtle text-brand text-xs font-medium rounded">
                      {t('wb.shotN', { n: shot.shot_id ?? idx + 1 })}
                    </span>
                    {shot.camera && (
                      <span className="text-xs text-ink-2">{shot.camera}</span>
                    )}
                    {shot.location && (
                      <span className="text-xs text-ink-2">· {shot.location}</span>
                    )}
                    {shot.duration != null && (
                      <span className="text-xs text-ink-3">· {shot.duration}s</span>
                    )}
                  </div>
                  {shot.description && (
                    <p className="text-sm text-ink-1">{shot.description}</p>
                  )}
                  {shot.dialogue_text && (
                    <p className="text-sm text-ink-1 mt-1 pl-2 border-l-2 border-line-strong">
                      {shot.dialogue_text}
                    </p>
                  )}
                  {shot.visual_detail && (
                    <p className="text-xs text-ink-2 mt-1">
                      {t('wb.visualDetail', { text: shot.visual_detail })}
                    </p>
                  )}
                  {shot.audio_cues && (
                    <p className="text-xs text-ink-3 mt-1">
                      {t('wb.audioCues', { text: shot.audio_cues })}
                    </p>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-ink-2 text-sm">
              {t('wb.noShotsInEpisode')}
            </p>
          )}
        </div>
      </div>
    );
  }

  if (detailError) {
    // 硬失败：单集详情整块取不到数据，用 ErrorState 顶掉内容区（不是把已渲染内容盖掉）
    return (
      <div className="space-y-4">
        <ErrorState
          title={t('project.loadingFailed')}
          description={detailError}
          onRetry={() => {
            const n = lastDetailEpRef.current;
            if (n != null) loadEpisodeDetail(n);
          }}
        />
        <Button variant="link" className="text-sm" onClick={goBack}>
          {t('wb.backToList')}
        </Button>
      </div>
    );
  }

  if (detailLoading) {
    return (
      <div className="space-y-4" role="status" aria-live="polite" aria-label={t('common.loading')}>
        <Skeleton className="h-9 w-28" />
        <Skeleton className="h-40 rounded-lg" />
        <Skeleton className="h-64 rounded-lg" />
      </div>
    );
  }

  if (total === 0 && episodes.length === 0) {
    return (
      <EmptyState
        icon={<FolderOpen className="h-10 w-10" />}
        title={t('wb.noAssets')}
        description={t('wb.noAssetsHint')}
        action={
          <p className="text-sm text-brand">
            {t('wb.noAssetsAction')}
          </p>
        }
      />
    );
  }

  return (
    <div className="space-y-8">
      {/* 生产进度（实时）：把 autopilot 当前正在生产的一集/步骤/百分比/超时告警展示出来 */}
      <ProductionProgress projectKey={projectKey} />

      {/* 资产展示 */}
      {total > 0 && (
        <>
          {groups.map((g) => {
            const list = assets?.gallery?.[g.key] || [];
            if (list.length === 0) return null;
            return (
              <div key={g.key}>
                <h3 className="text-sm font-semibold text-ink-2 mb-3 flex items-center gap-1.5">
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
      <div className="border-t border-line pt-8">
        <h3 className="text-lg font-semibold text-ink-1 mb-4 flex items-center gap-2">
          <FileText className="h-5 w-5" /> {t('wb.script')}
        </h3>

        {scriptLoading ? (
          // 骨架对齐真实区块：4 张统计卡 → 进度条 → 剧集列表卡
          <div role="status" aria-live="polite" aria-label={t('common.loading')}>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-20 rounded-lg" />
              ))}
            </div>
            <Skeleton className="mt-4 h-16 rounded-lg" />
            <Skeleton className="mt-4 h-48 rounded-lg" />
          </div>
        ) : scriptError ? (
          <ErrorState
            title={t('project.loadingFailed')}
            description={scriptError}
            onRetry={fetchEpisodes}
          />
        ) : episodes.length === 0 ? (
          <EmptyState
            icon={<ClipboardList className="h-10 w-10" />}
            title={t('episodes.noEpisodes')}
            description={t('wb.startAutoFirst')}
          />
        ) : (
          <>
            {/* 统计卡片 */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
              <div className="bg-surface rounded-lg border border-line p-4">
                <div className="text-2xl font-bold text-ink-1">{totalEpisodes}</div>
                <div className="text-sm text-ink-2">{t('wb.totalEpisodes')}</div>
              </div>
              {(() => {
                const stats = episodes.reduce((acc: any, ep: any) => {
                  acc[ep.status] = (acc[ep.status] || 0) + 1;
                  return acc;
                }, {} as Record<string, number>);
                return [
                  { label: t('episodes.done'), count: stats['done'] || 0, color: 'text-success-strong' },
                  { label: t('episodes.producing'), count: stats['producing'] || 0, color: 'text-info-strong' },
                  { label: t('episodes.failed'), count: stats['failed'] || 0, color: 'text-danger-strong' },
                  { label: t('episodes.pending'), count: stats['pending'] || 0, color: 'text-ink-2' },
                ].map(s => (
                  <div key={s.label} className="bg-surface rounded-lg border border-line p-4">
                    <div className={`text-2xl font-bold ${s.color}`}>{s.count}</div>
                    <div className="text-sm text-ink-2">{s.label}</div>
                  </div>
                ));
              })()}
            </div>

            {/* 进度条 */}
            {totalEpisodes > 0 && (
              <div className="bg-surface rounded-lg border border-line p-4 mb-4">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm font-medium text-ink-1">{t('wb.overallProgress')}</span>
                  <span className="text-sm text-ink-2">{Math.round(((episodes.filter((e: any) => e.status === 'done').length) / totalEpisodes) * 100)}%</span>
                </div>
                <div className="w-full bg-line rounded-full h-2">
                  <div
                    className="bg-success h-2 rounded-full transition-all"
                    style={{ width: `${((episodes.filter((e: any) => e.status === 'done').length) / totalEpisodes) * 100}%` }}
                  ></div>
                </div>
              </div>
            )}

            {/* 剧集列表 */}
            <div className="bg-surface rounded-lg border border-line">
              <div className="p-4 border-b border-line">
                <h4 className="font-semibold text-ink-1">{t('wb.episodeList')}</h4>
                <p className="text-xs text-ink-2 mt-1">{t('wb.clickEpisodeHint')}</p>
              </div>

              {/* P2-15：补列表语义 —— 此前是一串裸 div/button，读屏不会播报
                  「列表，共 N 项」。这里刻意**不用** <table>：它是可点击的导航列表，
                  不是行列数据，套表格语义反而会误导读屏。 */}
              <ul className="divide-y divide-line">
                {episodes.map((ep: any) => (
                  <li key={ep.episode_no}>
                    <button
                      onClick={() => loadEpisodeDetail(ep.episode_no)}
                      className={`w-full p-4 hover:bg-surface-2 transition-colors text-left ${FOCUS_RING}`}
                    >
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          <span className="flex items-center justify-center w-8 h-8 rounded-full bg-brand-subtle text-brand text-sm font-semibold">
                            {ep.episode_no}
                          </span>
                          <div>
                            <p className="font-medium text-ink-1">
                              {t('wb.episodeNo', { n: ep.episode_no })}
                              {ep.chapter_title && <span className="ml-2 text-sm text-brand">《{ep.chapter_title}》</span>}
                            </p>
                            <p className="text-xs text-ink-2 mt-0.5">
                              {t('wb.chapterNo', { n: ep.chapter_index ?? ep.episode_no })}
                            </p>
                          </div>
                        </div>

                        <div className="flex items-center gap-4">
                          <div className="text-right">
                            <div className="text-sm text-ink-2">
                              {t('wb.shotsRatio', { done: ep.completed_shots, total: ep.shot_count })}
                            </div>
                            {ep.created_at && (
                              <div className="text-xs text-ink-3">{ep.created_at.split('T')[0]}</div>
                            )}
                          </div>

                          <span className={`px-2 py-1 rounded-full text-xs font-medium inline-flex items-center gap-1 ${
                            ep.status === 'done' ? 'bg-success-subtle text-success-strong' :
                            ep.status === 'producing' ? 'bg-info-subtle text-info-strong' :
                            ep.status === 'failed' ? 'bg-danger-subtle text-danger-strong' :
                            'bg-surface-2 text-ink-1'
                          }`}>
                            {ep.status === 'done' ? (<><Check className="h-3.5 w-3.5" /> {t('wb.done')}</>) :
                             ep.status === 'producing' ? t('wb.producing') :
                             ep.status === 'failed' ? (<><X className="h-3.5 w-3.5" /> {t('episodes.failed')}</>) :
                             t('ep.pending')}
                          </span>

                          <svg className="w-5 h-5 text-ink-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                          </svg>
                        </div>
                      </div>

                      {ep.shot_count > 0 && (
                        <div className="mt-3 w-full bg-line rounded-full h-1.5">
                          <div
                            className={`h-1.5 rounded-full transition-all ${
                              ep.status === 'done' ? 'bg-success' :
                              ep.status === 'failed' ? 'bg-danger' :
                              'bg-brand'
                            }`}
                            style={{ width: `${(ep.completed_shots / ep.shot_count) * 100}%` }}
                          ></div>
                        </div>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
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
  const { t } = useApp();
  const imageUrl = assetSrc(item.thumb?.url || item.views?.[0]?.url);
  const [broken, setBroken] = useState(false);
  const FallbackIcon = type === 'character' ? User : type === 'item' ? Box : Mountain;
  // 缩略图容器比例必须跟随资产实际画幅（后端 style_kit.ASSET_BASE_RATIO 写死）：
  // 角色三视图设定图与道具图是 1:1、场景原画是 16:9。此前一律 aspect-video + object-cover，
  // 一张 1:1（或更早的 3:4 竖幅）三视图放进 16:9 容器会被裁掉上下两边 ——
  // 人物头顶与脚底同时被切，看起来就是「三视图比例不对」。
  const thumbAspect = type === 'scene' ? 'aspect-video' : 'aspect-square';

  return (
    <button
      type="button"
      onClick={onClick}
      className={`text-left bg-surface rounded-xl border border-line overflow-hidden hover:shadow-lg transition-shadow ${FOCUS_RING}`}
    >
      <div className={`${thumbAspect} bg-surface-2 flex items-center justify-center overflow-hidden`}>
        {imageUrl && !broken ? (
          <img
            src={imageUrl}
            alt={item.name}
            loading="lazy"
            className="w-full h-full object-cover"
            onError={() => setBroken(true)}
          />
        ) : (
          <FallbackIcon className="h-9 w-9 text-ink-3" />
        )}
      </div>
      <div className="p-3">
        <h4 className="font-medium text-ink-1 text-sm truncate">{item.name}</h4>
        <p className="text-xs text-ink-2 mt-1">
          {item.category || (item.view_count ? t('wb.viewCount', { n: item.view_count }) : type === 'character' ? t('wb.characters') : type === 'item' ? t('wb.items') : t('wb.scenes'))}
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
  const { t } = useApp();
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
      title={item?.name || t('wb.assetPreview')}
      size="xl"
      footer={
        <div className="flex w-full items-center gap-2">
          <Button size="sm" variant="secondary" onClick={downloadCurrent}>
            {t('wb.downloadCurrent')}
          </Button>
          {current?.size && (
            <span className="text-xs text-ink-3">{(current.size / 1024).toFixed(0)} KB</span>
          )}
          {gallery.length > 1 && (
            <span className="ml-auto text-xs text-ink-3">{t('wb.switchView')}</span>
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
            <EmptyState icon={<ImageIcon className="h-10 w-10" />} title={t('wb.imageUnavailable')} />
          )}

          {gallery.length > 1 && (
            <div className="flex flex-wrap gap-2" role="tablist" aria-label={t('wb.viewSwitch')}>
              {gallery.map((g, i) => {
                const thumb = assetSrc(g.url);
                return (
                  <button
                    key={i}
                    type="button"
                    role="tab"
                    aria-selected={i === active}
                    aria-label={g.view || t('wb.viewN', { n: i + 1 })}
                    onClick={() => setActive(i)}
                    className={`h-14 w-20 overflow-hidden rounded border-2 ${FOCUS_RING} ${
                      i === active ? 'border-brand' : 'border-transparent hover:border-line-strong'
                    }`}
                  >
                    {thumb && <img src={thumb} alt="" className="h-full w-full object-cover" />}
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
  const { t } = useApp();
  const toast = useToast();
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [testing, setTesting] = useState<string | null>(null);
  // D2（2026-09-23）：质检配置**前端零入口** —— qcApi 的 updateConfig / clearConfig /
  // resetEndpoint / syncFromAI 此前在这页一个都没被调用（全站只有 AudioTab 调
  // updateConfig 改音频阈值），QcTab 是纯只读看板。而 qc_client._empty_config() 的
  // enabled 默认是 **False** → 新环境部署后质检**静默全关**，用户只看到「未启用」
  // 却找不到任何开关。这里补齐：总开关 / 分品类开关 / 合格线与重试 / 参考图对照 /
  // 端点三态动作。
  // 草稿 cfgDraft 与展示用的 data.config 分离：避免「一边编辑一边被 load() 覆盖」，
  // 「单镜重测」触发的重载也不会冲掉正在编辑的内容。
  const [cfgDraft, setCfgDraft] = useState<any>(null);
  const [cfgSaving, setCfgSaving] = useState(false);
  const [cfgBusy, setCfgBusy] = useState('');
  const [confirmClearOpen, setConfirmClearOpen] = useState(false);

  // refreshDraft=true：用服务端配置重建草稿（首次加载 / 手动刷新 / 保存与端点动作之后）
  const load = async (refreshDraft = false) => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      const resp: any = await qcApi.history(projectKey);
      setData(resp);
      const next = resp?.config ? { ...resp.config } : null;
      setCfgDraft((prev: any) => (refreshDraft || !prev ? next : prev));
    } catch (e) {
      setError(sanitizeError(e, t('qc.loadFailed')));
    } finally {
      setLoading(false);
    }
  };

  // 切项目必须丢弃上一项目的草稿（否则会把 A 项目的配置保存到 B 项目）
  useEffect(() => { setCfgDraft(null); void load(true); }, [projectKey]);

  const runTest = async (shotId: string) => {
    setTesting(shotId);
    setError('');
    setNotice('');
    try {
      const r = await qcApi.test({ project: projectKey, shot_id: shotId });
      const verdict = r.verdict === 'pass' ? t('qc.passed') : r.verdict === 'fail' ? t('qc.notPassed') : String(r.verdict || t('qc.unknownVerdict'));
      setNotice(`${t('qc.retestDone', { verdict })}${typeof r.score === 'number' ? t('qc.retestScore', { n: Math.round(r.score * 100) }) : ''}`);
      await load();
    } catch (e) {
      setError(sanitizeError(e, t('qc.retestFailed')));
    } finally {
      setTesting(null);
    }
  };

  // ---- D2：质检配置写操作 ----
  // ⚠️ 只提交本面板管辖的字段：save_config 按 CONFIG_KEYS 白名单**合并**，未提交的键保持
  // 不动 —— 这样 AudioTab 改过的音频阈值、以及 prompt 类配置不会被这里的草稿覆盖。
  const saveCfg = async () => {
    if (!cfgDraft) return;
    setCfgSaving(true);
    setError('');
    setNotice('');
    try {
      const resp: any = await qcApi.updateConfig({
        enabled: !!cfgDraft.enabled,
        script_enabled: !!cfgDraft.script_enabled,
        image_enabled: !!cfgDraft.image_enabled,
        video_enabled: !!cfgDraft.video_enabled,
        audio_enabled: !!cfgDraft.audio_enabled,
        keyframe_qc_enabled: !!cfgDraft.keyframe_qc_enabled,
        image_ref_compare: !!cfgDraft.image_ref_compare,
        pass_score: Number(cfgDraft.pass_score),
        max_retries: Number(cfgDraft.max_retries),
        video_frame_count: Number(cfgDraft.video_frame_count),
        timeout: Number(cfgDraft.timeout),
      } as any);
      // 后端在「总开关开了、但接口信息不全」时会回 warning：此时生成流程会**静默跳过**
      // 质检，必须原样透出给用户，否则又是一个「以为在质检其实没检」。
      setNotice(t('qc.cfgSaved') + (resp?.warning ? `；⚠️ ${resp.warning}` : ''));
      toast.success(t('qc.cfgSaved'));
      await load(true);
    } catch (e) {
      const msg = sanitizeError(e, t('qc.cfgSaveFailed'));
      setError(msg);
      toast.error(msg);
    } finally {
      setCfgSaving(false);
    }
  };

  const doSyncFromAi = async () => {
    setCfgBusy('sync');
    setError('');
    setNotice('');
    try {
      await qcApi.syncFromAI();
      setNotice(t('qc.syncDone'));
      toast.success(t('qc.syncDoneToast'));
      await load(true);
    } catch (e) {
      const msg = sanitizeError(e, t('qc.syncFailed'));
      setError(msg);
      toast.error(msg);
    } finally {
      setCfgBusy('');
    }
  };

  const doResetEndpoint = async () => {
    setCfgBusy('reset');
    setError('');
    setNotice('');
    try {
      await qcApi.resetEndpoint();
      setNotice(t('qc.resetDone'));
      toast.success(t('qc.resetDoneToast'));
      await load(true);
    } catch (e) {
      const msg = sanitizeError(e, t('qc.resetFailed'));
      setError(msg);
      toast.error(msg);
    } finally {
      setCfgBusy('');
    }
  };

  const doClearCfg = async () => {
    setCfgBusy('clear');
    setError('');
    setNotice('');
    try {
      await qcApi.clearConfig();
      setConfirmClearOpen(false);
      setNotice(t('qc.clearDone'));
      toast.success(t('qc.cleared'));
      await load(true);
    } catch (e) {
      const msg = sanitizeError(e, t('qc.clearFailed'));
      setError(msg);
      toast.error(msg);
    } finally {
      setCfgBusy('');
    }
  };

  const verdictBadge = (v: string) => {
    if (v === 'pass') return 'bg-success-subtle text-success-strong';
    if (v === 'fail') return 'bg-danger-subtle text-danger-strong';
    return 'bg-surface-2 text-ink-1';
  };

  if (loading) return (
    // 骨架对齐真实区块：标题行 → 质检引擎卡 → 4 张统计卡 → 逐镜明细卡
    <div className="space-y-6" role="status" aria-live="polite" aria-label={t('common.loading')}>
      <div className="flex justify-between items-center">
        <Skeleton className="h-5 w-24" />
        <Skeleton className="h-8 w-16" />
      </div>
      <Skeleton className="h-40 rounded-lg" />
      <div className="grid grid-cols-4 gap-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-20 rounded-lg" />
        ))}
      </div>
      <Skeleton className="h-48 rounded-lg" />
    </div>
  );

  // 硬失败：质检数据整体没取到（data 仍为空），下面的引擎状态与统计只会渲染成空壳
  if (error && !data) {
    return (
      <ErrorState
        title={t('project.loadingFailed')}
        description={error}
        onRetry={load}
      />
    );
  }

  const cfg = data?.config || {};
  const stats = data?.stats || { total: 0, passed: 0, failed: 0, retry_count: 0 };
  const records: any[] = Array.isArray(data?.history) ? data.history : [];
  // 后端 history[].kind 是**质检品类**（图片/视频/尾帧/资产/音频/剧本/提示词），
  // 此前一律渲染成「图像」，资产与提示词的记录显示得驴唇不对马嘴。
  const KIND_LABEL: Record<string, string> = {
    video: t('qc.kind.video'), image: t('qc.kind.image'), keyframe: t('qc.kind.keyframe'), asset: t('qc.kind.asset'),
    audio: t('qc.kind.audio'), script: t('qc.kind.script'), prompt: t('qc.kind.prompt'),
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold text-ink-1">{t('qc.heading')}</h3>
        <Button size="sm" variant="secondary" onClick={load}>{t('common.refresh')}</Button>
      </div>

      {notice && (
        <div className="p-3 bg-success-subtle border border-success/30 rounded-lg text-success-strong text-sm">
          {notice}
        </div>
      )}
      {error && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">{error}</div>
      )}

      {/* 质检引擎状态 */}
      <div className="bg-surface rounded-lg border border-line p-4">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-ink-1">{t('qc.engine')}</span>
          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
            cfg.enabled ? 'bg-success-subtle text-success-strong'
                        : 'bg-surface-2 text-ink-2'
          }`}>
            {cfg.enabled ? t('qc.enabledOn') : t('qc.enabledOff')}
          </span>
          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
            cfg.ready ? 'bg-brand-subtle text-brand-hover'
                      : 'bg-warning-subtle text-warning-strong'
          }`}>
            {cfg.ready ? t('qc.ready') : t('qc.notReady')}
          </span>
        </div>
        <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div>
            <div className="text-ink-2 text-xs">{t('qc.model')}</div>
            <div className="text-ink-1 truncate">{cfg.effective_model || cfg.model || '—'}</div>
          </div>
          <div>
            <div className="text-ink-2 text-xs">{t('qc.passLine')}</div>
            <div className="text-ink-1">{cfg.pass_score ?? '—'}</div>
          </div>
          <div>
            <div className="text-ink-2 text-xs">{t('qc.endpoint')}</div>
            <div className="text-ink-1 truncate">{cfg.effective_base_url || cfg.base_url || '—'}</div>
          </div>
          <div>
            <div className="text-ink-2 text-xs">API Key</div>
            <div className="text-ink-1">{cfg.has_api_key ? (cfg.api_key_masked || t('qc.configured')) : t('qc.notConfigured')}</div>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          {/* 这里显示的是**实际是否生效**（*_qc_active），不是「用户配了什么」——
              配置开关在下方「质检配置」里，两处刻意分工：
              总开关关 / 品类开关关 / 接口没配全，都会让某个品类「未生效」。 */}
          {[
            { label: t('qc.kind.script'), on: cfg.script_qc_active },
            { label: t('qc.kind.image'), on: cfg.image_qc_active },
            { label: t('qc.kind.video'), on: cfg.video_qc_active },
            { label: t('qc.kind.audio'), on: cfg.audio_qc_active },
          ].map((k) => (
            <span key={k.label} className={`px-2 py-0.5 rounded ${
              k.on ? 'bg-brand-subtle text-brand'
                   : 'bg-surface-2 text-ink-2'
            }`}>
              {k.label}{t('qc.suffix')} {k.on ? t('qc.active') : t('qc.inactive')}
            </span>
          ))}
        </div>
      </div>

      {/* 质检配置（D2 2026-09-23：此前前端零入口，enabled 默认 false → 新环境质检静默全关） */}
      {cfgDraft && (
        <div className="bg-surface rounded-lg border border-line p-4">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <span className="font-medium text-ink-1">{t('qc.config')}</span>
            <span className="text-xs text-ink-3">
              {t('qc.sourcePrefix')}：{cfg.endpoint_auto_synced ? t('qc.sourceAuto') : t('qc.sourceManual')}
            </span>
          </div>

          <label className="mt-3 flex flex-wrap items-center gap-2 text-sm text-ink-1">
            {/* 保留原生：共享组件未覆盖 checkbox */}
            <input
              type="checkbox"
              checked={!!cfgDraft.enabled}
              onChange={(e) => setCfgDraft({ ...cfgDraft, enabled: e.target.checked })}
              className={`rounded ${FOCUS_RING}`}
            />
            {t('qc.enableMaster')}
            <span className="text-xs text-ink-3">{t('qc.enableMasterHint')}</span>
          </label>

          <div className="mt-3 grid grid-cols-2 md:grid-cols-5 gap-2">
            {[
              { key: 'script_enabled', label: t('qc.kind.script') },
              { key: 'image_enabled', label: t('qc.kind.image') },
              { key: 'video_enabled', label: t('qc.kind.video') },
              { key: 'audio_enabled', label: t('qc.kind.audio') },
              { key: 'keyframe_qc_enabled', label: t('qc.kind.keyframe') },
            ].map((k) => (
              <label key={k.key} className="flex items-center gap-2 text-sm text-ink-2">
                <input
                  type="checkbox"
                  checked={!!cfgDraft[k.key]}
                  onChange={(e) => setCfgDraft({ ...cfgDraft, [k.key]: e.target.checked })}
                  className={`rounded ${FOCUS_RING}`}
                />
                {k.label}{t('qc.suffix')}
              </label>
            ))}
          </div>

          <label className="mt-2 flex flex-wrap items-center gap-2 text-sm text-ink-2">
            <input
              type="checkbox"
              checked={!!cfgDraft.image_ref_compare}
              onChange={(e) => setCfgDraft({ ...cfgDraft, image_ref_compare: e.target.checked })}
              className={`rounded ${FOCUS_RING}`}
            />
            {t('qc.refCompare')}
            <span className="text-xs text-ink-3">
              {t('qc.refCompareHint')}
            </span>
          </label>

          {/* 保留原生：这四个 number 输入带 step/min/max 约束与数值型默认值，
              Input 组件未开放 step/min/max，换成 Input 会静默丢掉步进与取值范围 */}
          <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-3">
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('qc.passScoreLabel')}</div>
              <input
                type="number" step="1" min="0" max="100"
                value={cfgDraft.pass_score ?? 70}
                onChange={(e) => setCfgDraft({ ...cfgDraft, pass_score: e.target.value })}
                className={`w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 ${FOCUS_RING}`}
              />
            </div>
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('qc.maxRetriesLabel')}</div>
              <input
                type="number" step="1" min="0" max="5"
                value={cfgDraft.max_retries ?? 2}
                onChange={(e) => setCfgDraft({ ...cfgDraft, max_retries: e.target.value })}
                className={`w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 ${FOCUS_RING}`}
              />
            </div>
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('qc.videoFramesLabel')}</div>
              <input
                type="number" step="1" min="1" max="6"
                value={cfgDraft.video_frame_count ?? 3}
                onChange={(e) => setCfgDraft({ ...cfgDraft, video_frame_count: e.target.value })}
                className={`w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 ${FOCUS_RING}`}
              />
            </div>
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('qc.timeoutLabel')}</div>
              <input
                type="number" step="10" min="30" max="600"
                value={cfgDraft.timeout ?? 180}
                onChange={(e) => setCfgDraft({ ...cfgDraft, timeout: e.target.value })}
                className={`w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 ${FOCUS_RING}`}
              />
            </div>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button onClick={saveCfg} loading={cfgSaving} disabled={cfgSaving} className="text-sm">
              {t('qc.saveCfg')}
            </Button>
            <Button
              variant="secondary"
              onClick={doSyncFromAi}
              disabled={cfgBusy !== ''}
              className="text-sm"
            >
              {cfgBusy === 'sync' ? t('qc.syncing') : t('qc.syncFromAi')}
            </Button>
            <Button
              variant="secondary"
              onClick={doResetEndpoint}
              disabled={cfgBusy !== ''}
              className="text-sm"
            >
              {cfgBusy === 'reset' ? t('qc.resetting') : t('qc.resetToAi')}
            </Button>
            <Button
              variant="danger"
              onClick={() => setConfirmClearOpen(true)}
              disabled={cfgBusy !== ''}
              className="text-sm"
            >
              {t('qc.clearCfg')}
            </Button>
          </div>
          <p className="mt-2 text-xs text-ink-3">
            {t('qc.helpText')}
          </p>
        </div>
      )}

      <Modal
        isOpen={confirmClearOpen}
        onClose={() => setConfirmClearOpen(false)}
        title={t('qc.clearCfg')}
        closeOnBackdrop={false}
        closeOnEsc={!cfgBusy}
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => setConfirmClearOpen(false)}
              disabled={!!cfgBusy}
            >
              {t('common.cancel')}
            </Button>
            <Button
              variant="danger"
              onClick={doClearCfg}
              loading={cfgBusy === 'clear'}
              disabled={!!cfgBusy}
            >
              {t('qc.confirmClear')}
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          {t('qc.clearBody1')}
          <strong className="text-ink-1">{t('qc.clearBodyStrong')}</strong>{t('qc.clearBody2')}
          {t('qc.clearBody3')}<strong className="text-ink-1">{t('qc.clearBodyStrong2')}</strong>
        </p>
        <p className="mt-2 text-sm text-ink-2">
          {t('qc.clearHint')}
        </p>
      </Modal>

      {/* 统计 */}
      <div className="grid grid-cols-4 gap-4">
        {[
          { label: t('qc.totalStats'), value: stats.total, color: 'text-ink-1' },
          { label: t('qc.passed'), value: stats.passed, color: 'text-success-strong' },
          { label: t('qc.notPassed'), value: stats.failed, color: 'text-danger-strong' },
          { label: t('qc.pending'), value: stats.retry_count, color: 'text-warning-strong' },
        ].map((s) => (
          <div key={s.label} className="bg-surface rounded-lg border border-line p-4 text-center">
            <div className={`text-2xl font-bold ${s.color}`}>{s.value ?? 0}</div>
            <div className="text-xs text-ink-2 mt-1">{s.label}</div>
          </div>
        ))}
      </div>

      {/* 逐镜明细（P2-15：改为**语义化表格**）—— 此前是 div 模拟的六列「表格」，
          读屏只能听到一串无结构的文本，既读不出行列关系，也没有表头关联。 */}
      {records.length === 0 ? (
        <EmptyState
          icon={<ClipboardCheck className="h-10 w-10" />}
          title={t('qc.noRecords')}
          description={t('qc.noRecordsHint')}
        />
      ) : (
        <div className="bg-surface rounded-lg border border-line overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">{t('qc.detailCaption')}</caption>
            <thead>
              <tr className="border-b border-line text-xs text-ink-2">
                <th scope="col" className="text-left font-medium p-3">{t('common.shot')}</th>
                <th scope="col" className="text-left font-medium p-3">{t('qc.kindCol')}</th>
                <th scope="col" className="text-left font-medium p-3">{t('qc.verdict')}</th>
                <th scope="col" className="text-left font-medium p-3">{t('common.score')}</th>
                <th scope="col" className="text-left font-medium p-3">{t('qc.time')}</th>
                <th scope="col" className="text-right font-medium p-3">{t('qc.actions')}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {records.map((r, i) => (
                <tr key={`${r.shot_id}-${r.kind}-${i}`}>
                  <th scope="row" className="text-left font-mono font-normal text-ink-1 p-3 align-middle">
                    {r.shot_id}
                  </th>
                  <td className="p-3 align-middle">
                    <span className="text-xs px-2 py-0.5 rounded bg-surface-2 text-ink-2">
                      {KIND_LABEL[r.kind] || r.kind || t('wb.qc')}
                    </span>
                  </td>
                  <td className="p-3 align-middle">
                    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${verdictBadge(r.verdict)}`}>
                      {r.verdict === 'pass' ? t('qc.passed') : r.verdict === 'fail' ? t('qc.notPassed')
                        : r.verdict === 'error' ? t('qc.errorVerdict') : r.verdict === 'unknown' ? t('qc.pending')
                        : String(r.verdict || t('common.unknown'))}
                    </span>
                  </td>
                  <td className="p-3 align-middle text-ink-2">
                    {/* ⚠️ 后端 score 是 **0~100**（实测区间 15~98），不是 0~1 的比例。
                        这里此前无条件 *100，会把 82 分显示成「8200」。 */}
                    {typeof r.score === 'number'
                      ? Math.round(r.score <= 1 ? r.score * 100 : r.score)
                      : '—'}
                  </td>
                  <td className="p-3 align-middle text-xs text-ink-3 whitespace-nowrap">
                    {r.timestamp ? String(r.timestamp).replace('T', ' ').slice(0, 19) : '—'}
                  </td>
                  <td className="p-3 align-middle text-right">
                    {(r.kind === 'image' || r.kind === 'video') && (
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={testing === r.shot_id}
                        onClick={() => runTest(r.shot_id)}
                      >
                        {testing === r.shot_id ? t('qc.retesting') : t('qc.retest')}
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
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

  const subs: { id: 'storyboard' | 'ninegrid' | 'keyframes'; icon: React.ReactNode; label: string; hint: string }[] = [
    { id: 'storyboard', icon: <Clapperboard className="h-4 w-4" />, label: t('wb.subStoryboard'), hint: t('sb.subStoryboardHint') },
    { id: 'ninegrid', icon: <Target className="h-4 w-4" />, label: t('wb.subNinegrid'), hint: t('sb.subNinegridHint') },
    { id: 'keyframes', icon: <ImageIcon className="h-4 w-4" />, label: t('wb.subKeyframes'), hint: t('sb.subKeyframesHint') },
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
            className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm transition-all ${FOCUS_RING} ${
              sub === s.id
                ? 'bg-brand text-white shadow'
                : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1'
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
      setError(err instanceof Error ? err.message : t('sb.fetchFailed'));
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
      toast.success(`${t('keyframes.generateStarted')}：${result.task_id}`);
      setTimeout(fetchPlan, 3000);
    } catch (err) {
      const msg = err instanceof Error ? err.message : t('sb.generateFailed');
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
        <h3 className="text-lg font-semibold">{t('keyframes.title')}</h3>
        <Button size="sm" onClick={fetchPlan} disabled={loading}>{t('common.refresh')}</Button>
      </div>

      {/* 加载态（此前首屏只剩标题栏，无任何反馈）：对齐真实区块的 4 张统计卡 */}
      {loading && !plan && (
        <div role="status" aria-live="polite" aria-label={t('common.loading')}>
          <div className="grid grid-cols-4 gap-4">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-24 rounded-lg" />
            ))}
          </div>
        </div>
      )}

      {/* 硬失败：整块拿不到 plan（无数据可展示）→ ErrorState；
          已有 plan 时仅是刷新/生成失败 → 降级为下面那条紧凑行内提示，不吃掉已展示内容 */}
      {error && !plan && (
        <ErrorState
          title={t('project.loadingFailed')}
          description={error}
          onRetry={fetchPlan}
        />
      )}
      {error && plan && (
        <div className="p-4 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong">{error}</div>
      )}

      {plan && (
        <div className="grid grid-cols-4 gap-4">
          <div className="bg-surface rounded-lg border border-line p-4 text-center">
            <div className="text-3xl font-bold text-brand">{plan.shot_count}</div>
            <div className="text-sm text-ink-2">{t('keyframes.totalShots')}</div>
          </div>
          <div className="bg-surface rounded-lg border border-line p-4 text-center">
            <div className="text-3xl font-bold text-success-strong">{plan.start_frames_ready}</div>
            <div className="text-sm text-ink-2">{t('keyframes.startReady')}</div>
          </div>
          <div className="bg-surface rounded-lg border border-line p-4 text-center">
            <div className="text-3xl font-bold text-info-strong">{plan.end_frames_ready}</div>
            <div className="text-sm text-ink-2">{t('keyframes.endReady')}</div>
          </div>
          <div className="bg-surface rounded-lg border border-line p-4 text-center">
            <div className="text-3xl font-bold text-warning-strong">{plan.to_generate}</div>
            <div className="text-sm text-ink-2">{t('keyframes.toGenerate')}</div>
          </div>
        </div>
      )}

      {plan && plan.to_generate > 0 && (
        <Button
          onClick={handleGenerate}
          disabled={generating}
          className="w-full bg-warning hover:bg-warning-strong"
        >
          {generating ? t('common.generating') : t('keyframes.generate')}
        </Button>
      )}

      {plan && (
        <div className="space-y-2">
          <h4 className="font-semibold text-ink-1 mb-3">{t('keyframes.shotList')}</h4>
          {plan.plan?.map((shot: any) => (
            <div
              key={shot.seq}
              className={`flex items-center gap-4 p-3 rounded-lg ${
                shot.need_gen ? 'bg-warning-subtle border border-warning/30' :
                shot.has_end ? 'bg-success-subtle border border-success/30' :
                'bg-surface-2'
              }`}
            >
              <span className="w-12 font-mono text-ink-2">#{shot.seq}</span>
              <span className="flex-1">{shot.status || shot.shot_id}</span>
              <div className="flex gap-2">
                {shot.has_start && <span className="px-2 py-1 bg-success/20 text-success-strong rounded text-xs">{t('keyframes.startFrame')}</span>}
                {shot.has_end && <span className="px-2 py-1 bg-info/20 text-info-strong rounded text-xs">{t('keyframes.endFrame')}</span>}
                {shot.need_gen && !shot.has_end && <span className="px-2 py-1 bg-warning/20 text-warning-strong rounded text-xs">{t('keyframes.toGenerate')}</span>}
              </div>
              {shot.url && (
                <a href={shot.url} target="_blank" rel="noopener noreferrer" className={`text-brand hover:text-brand rounded-sm ${FOCUS_RING}`}>{t('common.view')}</a>
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
      const msg = err instanceof Error ? err.message : t('sb.fetchFailed');
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
      setNotice(t('sb.imageRedone', { seq: card.seq }));
      await fetchCanvas();
    } catch (e) {
      setShotError(e instanceof Error ? e.message : t('sb.imageRedoFailed'));
    } finally {
      setBusy(null);
    }
  };

  const handleRetryVideo = async (card: any) => {
    const key = `${card.shot_id}:video`;
    const mode = videoMode[String(card.shot_id)] || 'reference';
    if (mode === 'keyframe' && !card.keyframe?.end_exists) {
      setShotError(t('sb.noKeyframeForInterp', { seq: card.seq }));
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
        t('sb.videoRedone', {
          seq: card.seq,
          mode: r.mode === 'keyframe' ? t('sb.modeKeyframe') : t('sb.modeReference'),
          refCount: r.ref_count,
          duration: r.duration,
        }) + (r.deliverable_marked_stale ? t('sb.videoRedoneStale') : '')
      );
      await fetchCanvas();
    } catch (e) {
      setShotError(e instanceof Error ? e.message : t('sb.videoRedoFailed'));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold">{t('wb.storyboardHub')}</h3>
        <Button size="sm" onClick={fetchCanvas} disabled={loading}>{t('common.refresh')}</Button>
      </div>

      {/* 加载态（此前首屏只剩标题栏，无任何反馈）：对齐真实区块的三列分镜卡 */}
      {loading && cards.length === 0 && (
        <div role="status" aria-live="polite" aria-label={t('common.loading')}>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-56 rounded-lg" />
            ))}
          </div>
        </div>
      )}

      {summary && (
        <div className="flex flex-wrap gap-4 text-sm text-ink-2">
          <span>{t('sb.shotCount', { n: summary.shot_count })}</span>
          <span>{t('sb.storyboardCount', { n: summary.storyboard_ready ?? 0 })}</span>
          <span>{t('sb.videoCount', { n: summary.video_ready ?? 0 })}</span>
          <span>{t('sb.keyframeCount', { n: summary.keyframe_end_ready ?? 0 })}</span>
          {(summary.qc_blocked ?? 0) > 0 && (
            <span className="text-warning-strong">{t('storyboard.qcBlocked')} {summary.qc_blocked}</span>
          )}
        </div>
      )}

      {notice && (
        <div className="p-3 bg-success-subtle border border-success/30 rounded-lg text-success-strong text-sm">
          {notice}
        </div>
      )}
      {/* 软失败：重做单镜失败时卡片仍在展示，只能用紧凑行内条，不能顶掉内容 */}
      {shotError && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
          {shotError}
        </div>
      )}

      {error === 'no-data' && (
        <EmptyState
          icon={<Clapperboard className="h-10 w-10" />}
          title={t('sb.noData')}
          description={t('sb.noDataHint')}
        />
      )}

      {/* 硬失败：分镜数据整体没取到且无卡片可展示 → ErrorState；
          已有卡片时的刷新失败 → 行内条（软失败） */}
      {error && error !== 'no-data' && cards.length === 0 && (
        <ErrorState
          title={t('project.loadingFailed')}
          description={error}
          onRetry={fetchCanvas}
        />
      )}
      {error && error !== 'no-data' && cards.length > 0 && (
        <div className="p-4 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong">{error}</div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {cards.map((card: any) => {
          const sid = String(card.shot_id);
          const imgBusy = busy === `${card.shot_id}:image`;
          const vidBusy = busy === `${card.shot_id}:video`;
          const mode = videoMode[sid] || 'reference';
          return (
            <div key={card.seq} className="bg-surface rounded-lg border border-line p-4 flex flex-col">
              <div className="flex items-center justify-between mb-2">
                <span className="font-mono text-sm text-ink-2">#{card.seq}</span>
                <span className="text-xs text-ink-2">{card.camera}</span>
              </div>

              <div className="flex gap-3 mb-2">
                <div className="w-24 h-24 shrink-0 rounded bg-surface-2 border border-line overflow-hidden flex items-center justify-center text-xs text-ink-3">
                  {card.storyboard?.exists && card.storyboard?.url ? (
                    <img src={card.storyboard.url} alt={t('sb.imageAlt', { seq: card.seq })} className="w-full h-full object-cover" />
                  ) : (
                    <span>{t('sb.noImage')}</span>
                  )}
                </div>
                <div className="min-w-0 flex-1 text-xs space-y-1">
                  <p className="text-ink-1 line-clamp-3">{card.description}</p>
                  {card.dialogue_text && (
                    <p className="text-ink-2 italic line-clamp-2">{card.dialogue_text}</p>
                  )}
                  <p className={card.video?.exists ? 'text-success-strong' : 'text-warning-strong'}>
                    {t('sb.video')}：{card.video?.exists ? t('sb.generated') : t('sb.notGenerated')}
                  </p>
                  {card.consistency?.score != null && (
                    <p className="text-ink-2">{t('sb.consistency')}：{card.consistency.score}</p>
                  )}
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2 mt-auto pt-2">
                {card.video?.exists && card.video?.url && (
                  <a
                    href={card.video.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={`text-xs text-brand hover:text-brand rounded-sm ${FOCUS_RING}`}
                  >
                    {t('sb.viewVideo')}
                  </a>
                )}
                <select
                  value={mode}
                  onChange={(e) =>
                    setVideoMode((prev) => ({ ...prev, [sid]: e.target.value as 'reference' | 'keyframe' }))
                  }
                  className={`text-xs rounded border border-line bg-surface text-ink-1 px-1 py-1 ${FOCUS_RING}`}
                  title={t('sb.modeHint')}
                >
                  <option value="reference">{t('sb.modeReference')}</option>
                  <option value="keyframe">{t('sb.modeKeyframe')}</option>
                </select>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => handleRetryImage(card)}
                  disabled={!!busy}
                >
                  {imgBusy ? t('sb.redoing') : t('sb.redoImage')}
                </Button>
                <Button
                  size="sm"
                  onClick={() => handleRetryVideo(card)}
                  disabled={!!busy}
                >
                  {vidBusy ? t('sb.redoing') : t('sb.redoVideo')}
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

  if (loading) return (
    // 骨架对齐真实区块：标题行 → 链路自检卡 → 计划卡 → 源选择卡
    <div className="space-y-4" role="status" aria-live="polite" aria-label={t('common.loading')}>
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 space-y-2">
          <Skeleton className="h-5 w-28" />
          <Skeleton className="h-4 w-56" />
        </div>
        <Skeleton className="h-8 w-20" />
      </div>
      <Skeleton className="h-32 rounded-lg" />
      <Skeleton className="h-32 rounded-lg" />
      <Skeleton className="h-44 rounded-lg" />
    </div>
  );

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
          <h3 className="text-lg font-semibold text-ink-1">{t('upscale.title')}</h3>
          <p className="text-sm text-ink-2 mt-0.5">{t('upscale.subtitle')}</p>
        </div>
        <Button size="sm" variant="secondary" onClick={load} disabled={loading || busy}>
          {t('common.refresh')}
        </Button>
      </div>

      {/* 链路自检 */}
      <div
        className={`p-3 rounded-lg border text-sm ${
          ready
            ? 'bg-success-subtle border-success/30 text-success-strong'
            : 'bg-warning-subtle border-warning/30 text-warning-strong'
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
            <span className="inline-flex items-center gap-1">{t('upscale.comfyOnline')}: {env.comfy_online ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
            <span className="inline-flex items-center gap-1">{t('upscale.modelReady')}: {env.model_ready ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
            <span className="inline-flex items-center gap-1">{t('upscale.teReady')}: {env.te_ready ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
            <span className="inline-flex items-center gap-1">{t('upscale.legacyReady')}: {env.legacy_ready ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
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
        <div className="bg-surface rounded-lg border border-line p-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="text-sm font-medium text-ink-1">
                {t('upscale.planToggle')}
              </p>
              <p className="text-xs text-ink-2 mt-1">
                {t('upscale.planToggleHint')}
              </p>
            </div>
            <div className="flex items-center gap-3 shrink-0">
              <span className={`text-xs font-medium ${planOn ? 'text-success-strong' : 'text-ink-2'}`}>
                {planOn ? t('upscale.on') : t('upscale.off')}
              </span>
              <button
                role="switch"
                aria-checked={planOn}
                disabled={savingPlan}
                onClick={() => savePlan({ enable_upscale: !planOn })}
                className={`relative w-11 h-6 rounded-full transition-colors disabled:opacity-50 ${FOCUS_RING} ${
                  planOn ? 'bg-brand' : 'bg-line-strong'
                }`}
              >
                <span
                  className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-surface shadow transition-transform ${
                    planOn ? 'translate-x-5' : ''
                  }`}
                />
              </button>
            </div>
          </div>

          {/* 自动生产的超分倍率（与手工超分独立配置） */}
          <div className="mt-3 pt-3 border-t border-line flex items-center gap-3">
            <span className="text-xs text-ink-2">{t('upscale.planScale')}</span>
            <div className="flex gap-2">
              {([2, 3, 4] as const).map((n) => (
                <button
                  key={n}
                  disabled={savingPlan || !planOn}
                  onClick={() => savePlan({ upscale_scale: n })}
                  className={`px-3 py-1 rounded-md text-xs font-medium transition-all disabled:opacity-50 ${FOCUS_RING} ${
                    planScale === n
                      ? 'bg-brand text-white'
                      : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1'
                  }`}
                >
                  {t('upscale.scaleTimes', { n })}
                </button>
              ))}
            </div>
            <span className="text-xs text-ink-3">{t('upscale.planScaleHint')}</span>
          </div>
        </div>
      )}

      {error && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-sm text-danger-strong">
          {error}
        </div>
      )}

      {/* 选择源 + 倍率 + 发起 */}
      <div className="bg-surface rounded-lg border border-line p-4 space-y-4">
        {sources.length === 0 ? (
          <EmptyState
            icon={<ZoomIn className="h-10 w-10" />}
            title={t('upscale.sourceEmpty')}
            description={t('upscale.sourceEmptyTip')}
          />
        ) : (
          <>
            <div className="grid md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-ink-2 mb-1.5">
                  {t('upscale.selectSource')}
                </label>
                <select
                  value={selected}
                  onChange={(e) => { setSelected(e.target.value); setPlaying(null); }}
                  disabled={busy}
                  className={`w-full px-3 py-2 text-sm border border-line rounded-lg bg-surface text-ink-1 ${FOCUS_RING}`}
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
                <label className="mt-2 flex items-center gap-2 text-xs text-ink-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={showComfy}
                    onChange={(e) => setShowComfy(e.target.checked)}
                    className={`rounded border-line accent-brand ${FOCUS_RING}`}
                  />
                  {t('upscale.showComfy', {
                    n: sources.length - sources.filter((s) => !s.kind.startsWith('ComfyUI')).length,
                  })}
                </label>
                {source && (
                  <p className="text-xs text-ink-2 mt-1.5">
                    {source.mtime} · {source.size_mb} MB
                  </p>
                )}
              </div>

              <div>
                <label className="block text-xs font-medium text-ink-2 mb-1.5">
                  {t('upscale.scale')}
                </label>
                <div className="flex gap-2">
                  {([2, 3, 4] as const).map((n) => (
                    <button
                      key={n}
                      onClick={() => setScale(n)}
                      disabled={busy}
                      className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${FOCUS_RING} ${
                        scale === n
                          ? 'bg-brand text-white shadow-lg'
                          : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1'
                      }`}
                    >
                      {t('upscale.scaleTimes', { n })}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-ink-2 mt-1.5">
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
                <span className="text-xs text-warning-strong self-center">
                  {t('upscale.notReadyTip')}
                </span>
              )}
            </div>
          </>
        )}
      </div>

      {/* 任务进度 / 结果 */}
      {task && (
        <div className="bg-surface rounded-lg border border-line p-4 space-y-3">
          <div className="flex items-center justify-between gap-3">
            <span className="font-semibold text-ink-1">{statusText()}</span>
            <span className="text-xs text-ink-2">
              {t('upscale.progress')} {task.progress || 0}%
            </span>
          </div>

          <div className="h-2 rounded-full bg-surface-2 overflow-hidden">
            <div
              className={`h-full transition-all ${
                task.status === 'error' ? 'bg-danger' : 'bg-brand'
              }`}
              style={{ width: `${Math.min(100, task.progress || 0)}%` }}
            />
          </div>

          {task.message && (
            <p className="text-xs text-ink-2">{task.message}</p>
          )}
          {task.status === 'error' && task.error && (
            <p className="text-xs text-danger-strong break-all">{task.error}</p>
          )}

          {task.status === 'done' && res && (
            <div className="pt-3 border-t border-line space-y-3">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                <div>
                  <div className="text-ink-2">{t('upscale.before')}</div>
                  <div className="font-medium text-ink-1">{fmtResolution(res.before)}</div>
                </div>
                <div>
                  <div className="text-ink-2">{t('upscale.after')}</div>
                  <div className="font-medium text-ink-1">{fmtResolution(res.after)}</div>
                </div>
                <div>
                  <div className="text-ink-2">{t('upscale.elapsed')}</div>
                  <div className="font-medium text-ink-1">
                    {res.elapsed_sec != null ? `${res.elapsed_sec}s` : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-ink-2">{t('upscale.resolution')}</div>
                  <div className="font-medium text-ink-1">
                    {res.after?.size_mb != null ? `${res.after.size_mb} MB` : '—'}
                  </div>
                </div>
              </div>

              {/* 音轨保留情况：无声超分是这里最容易踩的坑 */}
              <p className={`text-xs ${res.after?.has_audio ? 'text-success-strong' : 'text-warning-strong'}`}>
                {res.after?.has_audio ? t('upscale.audioKept') : t('upscale.audioLost')}
              </p>

              {res.output_url && (
                <>
                  <video src={res.output_url} controls className="w-full rounded-lg bg-black" />
                  <div className="flex gap-2">
                    <a
                      href={downloadUrl(res.output_url) || res.output_url}
                      className={`inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-line text-ink-1 hover:bg-surface-2 transition-colors ${FOCUS_RING}`}
                    >
                      {t('common.download')}
                    </a>
                    <span className="text-xs text-ink-2 self-center">
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
        <div className="bg-surface rounded-lg border border-line p-4">
          <h4 className="text-sm font-semibold text-ink-1 mb-3">
            {t('upscale.artifacts')}（{artifacts.length}）
          </h4>
          <div className="space-y-2">
            {artifacts.map((a) => (
              <div
                key={a.name}
                className="flex items-center justify-between gap-3 py-2 border-b border-line last:border-0"
              >
                <div className="min-w-0">
                  <p className="text-sm text-ink-1 truncate">{a.name}</p>
                  <p className="text-xs text-ink-2">{a.mtime} · {a.size_mb} MB</p>
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
                      className={`inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-line text-ink-1 hover:bg-surface-2 transition-colors ${FOCUS_RING}`}
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
      setError(err instanceof Error ? err.message : t('chat.killFailed'));
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
          setError(t('chat.noReply'));
        }
        return;
      }

      // 自主执行模式：下发任务 → 轮询 → 逐步展示「它自己做了什么」
      const started = await agentApi.send(text, projectKey);
      if (!started.success || !started.job_id) {
        setError(t('chat.startFailed'));
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
        if (Date.now() > deadline) { setError(t('chat.timeout')); break; }
      }
      setRun(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('chat.sendFailed'));
      setRun(null);
    } finally {
      setSending(false);
    }
  };

  return (
    <aside
      // h-[calc(100vh-7rem)] = 视口高 −（顶栏 ~63px + main 上下 padding 48px），
      // 让面板与左列内容等高、上下贯通；sticky 使其随页面滚动保持停靠。
      className="flex w-full flex-col overflow-hidden rounded-xl border border-line bg-surface lg:sticky lg:top-0 lg:h-[calc(100vh-7rem)] lg:w-[340px] lg:shrink-0 min-h-[420px]"
    >
      {/* 头部：与工作台其他面板一致的白底 + 灰边 + indigo 强调 */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-line shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="w-7 h-7 rounded-lg bg-brand-subtle flex items-center justify-center shrink-0">
            <MessageSquare className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <h3 className="font-semibold text-ink-1 leading-tight">{t('chat.panelTitle')}</h3>
            <div className="flex items-center gap-1.5 mt-0.5">
              <p className="text-[11px] text-ink-2 leading-tight">
                {autoMode ? t('chat.autoExec', { n: toolCount || '…' }) : t('chat.chatOnly')}
              </p>
              <button
                onClick={() => setAutoMode(v => !v)}
                title={autoMode ? t('chat.switchToChat') : t('chat.switchToAuto')}
                className={`text-[10px] leading-none px-1.5 py-0.5 rounded border transition-colors ${FOCUS_RING} ${
                  autoMode
                    ? 'border-brand/30 text-brand bg-brand-subtle'
                    : 'border-line text-ink-2'
                }`}
              >
                {autoMode ? t('chat.auto') : t('chat.chat')}
              </button>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-0.5 shrink-0">
          {autoMode && (
            <button
              onClick={toggleKill}
              title={killOn ? t('chat.releaseKill') : t('chat.kill')}
              className={`p-1.5 rounded-lg transition-colors ${FOCUS_RING} ${
                killOn
                  ? 'text-danger bg-danger-subtle'
                  : 'text-ink-3 hover:text-danger hover:bg-danger-subtle'
              }`}
            >
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
            </button>
          )}
          <Button variant="ghost" size="sm" onClick={loadHistory} title={t('chat.refreshHistory')}>
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </Button>
          <Button variant="ghost" size="sm" onClick={onClose} title={t('chat.collapse')}>
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 5l7 7-7 7M5 5l7 7-7 7" />
            </svg>
          </Button>
        </div>
      </div>

      {error && (
        <div className="mx-3 mt-3 p-2 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-xs shrink-0">
          {error}
        </div>
      )}

      {/* 消息区：浅色底以区别于面板头部/输入区，形成「对话」区域感。
          注意：空态与消息列表要二选一渲染 —— 若把滚动哨兵 <div> 和 h-full 的空态
          放在同一个 space-y-3 容器里，哨兵会额外吃到 12px margin 而撑出滚动条。 */}
      <div className="flex-1 min-h-0 overflow-y-auto bg-surface-2">
        {messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center px-4">
            <EmptyState
              icon={<MessageSquare className="h-10 w-10" />}
              title={autoMode ? t('chat.autoEmptyTitle') : t('chat.chatEmptyTitle')}
              description={autoMode ? t('chat.autoEmptyDesc') : t('chat.chatEmptyDesc')}
            />
            <div className="w-full space-y-1.5">
              {(autoMode
                ? [t('chat.suggestAuto1'), t('chat.suggestAuto2'), t('chat.suggestAuto3'), t('chat.suggestAuto4')]
                : [t('chat.suggestChat1'), t('chat.suggestChat2')]
              ).map((ex) => (
                <button
                  key={ex}
                  onClick={() => setInput(ex)}
                  className={`w-full text-left text-xs px-3 py-2 rounded-lg bg-surface border border-line text-ink-2 hover:border-brand hover:text-brand transition-colors ${FOCUS_RING}`}
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
                    : 'bg-surface border border-line text-ink-1 mr-6 rounded-bl-sm'
                }`}
              >
                <p className="whitespace-pre-wrap break-words">{msg.content}</p>
              </div>
            ))}
            {/* 自主执行过程：把总控「自己调了哪些功能、成功没有」透明地摊开 */}
            {run && (
              <div className="mr-6 px-3 py-2 rounded-lg border border-line bg-surface space-y-1.5">
                <div className="flex items-center gap-2 text-xs text-ink-2">
                  {run.status === 'running' ? (
                    <span className="w-3 h-3 border-2 border-brand/40 border-t-brand rounded-full animate-spin inline-block shrink-0" />
                  ) : (
                    <span className="w-1.5 h-1.5 rounded-full bg-ink-3 inline-block shrink-0" />
                  )}
                  {t('chat.runningSteps', { n: run.steps.length })}
                </div>
                {run.steps.map((s: AgentStep, i: number) => (
                  <div key={i} className="flex items-start gap-1.5 text-[11px] leading-snug">
                    <span className={`shrink-0 flex items-center ${s.blocked ? 'text-warning-strong' : s.ok ? 'text-success-strong' : 'text-danger-strong'}`}>
                      {s.blocked
                        ? <AlertTriangle className="h-3.5 w-3.5" />
                        : s.ok
                          ? <Check className="h-3.5 w-3.5" />
                          : <X className="h-3.5 w-3.5" />}
                    </span>
                    <span className="font-mono text-ink-2 shrink-0">{s.tool}</span>
                    <span className="text-ink-2 break-all">
                      {s.summary}
                      {s.cached ? t('chat.cached') : ''}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {sending && !run && (
              <div className="bg-surface border border-line mr-6 px-3 py-2 rounded-lg text-sm text-ink-2 flex items-center gap-2">
                <span className="w-3 h-3 border-2 border-brand/40 border-t-brand rounded-full animate-spin inline-block" />
                {t('chat.thinking')}
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* 输入区 */}
      <div className="p-3 border-t border-line shrink-0 bg-surface">
        <div className="flex gap-2">
          <Input
            value={input}
            onChange={setInput}
            onEnter={sendMessage}
            placeholder={t('chat.panelPlaceholder')}
            className="flex-1 min-w-0"
          />
          <Button
            variant="brand"
            onClick={sendMessage}
            disabled={sending || !input.trim()}
            className="shrink-0"
          >
            {t('chat.send')}
          </Button>
        </div>
      </div>
    </aside>
  );
}
