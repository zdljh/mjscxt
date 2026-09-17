import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { projectsApi, keyframesApi, storyboardApi, ttsApi, mixApi, qcApi, exportApi, autopilotApi, upscaleApi, chatApi, agentApi } from '@/api/client';
import { Button, Loading, EmptyState } from '@/components/ui';
import { GridPage } from '@/pages/GridPage';
import type { Project, Deliverable, UpscaleEnv, UpscaleSource, UpscaleTask, UpscaleArtifact, AgentStep } from '@/types';

// ========== Workbench Tab Types ==========
// 注意：'chat' 已移除 —— AI 总控改成了右侧常驻面板，不再是标签页（见 ChatPanel）
type WorkbenchTab = 'overview' | 'autopilot' | 'keyframes' | 'ninegrid' | 'storyboard' | 'tts' | 'mix' | 'qc' | 'export' | 'deliver' | 'upscale';

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

  const tabs: { id: WorkbenchTab; icon: string; label: string }[] = [
    { id: 'overview', icon: '📊', label: t('wb.overview') },
    { id: 'autopilot', icon: '🤖', label: t('wb.autopilot') },
    { id: 'keyframes', icon: '🖼️', label: t('wb.keyframes') },
    { id: 'ninegrid', icon: '🎯', label: t('wb.ninegrid') },
    { id: 'storyboard', icon: '🎬', label: t('wb.storyboard') },
    { id: 'tts', icon: '🎙️', label: t('wb.tts') },
    { id: 'mix', icon: '🔊', label: t('wb.mix') },
    { id: 'qc', icon: '✅', label: t('wb.qc') },
    { id: 'export', icon: '💾', label: t('wb.export') },
    // 成品验收：后端 /api/autopilot/deliverables 的成片清单 + 验收/打回，
    // 此前只有已删除的全局 DeliverPage（且它 fetch 了数据却从不渲染）
    { id: 'deliver', icon: '📦', label: t('wb.deliver') },
    // 超分：后端 upscale_client 与其 8 个端点早已可用，但前端此前零引用 ——
    // 与已删除的孤儿页面同属「建好没入口」的能力，这里补上手工入口。
    { id: 'upscale', icon: '🔍', label: t('wb.upscale') },
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
              <h2 className="text-2xl font-bold text-gray-900 dark:text-white">{project.name}</h2>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
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

          {/* Stats Bar */}
          <div className="grid grid-cols-4 gap-4">
            {[
              { label: '角色', count: assets?.counts?.characters || 0, color: 'text-indigo-400' },
              { label: '物品', count: assets?.counts?.items || 0, color: 'text-green-400' },
              { label: '场景', count: assets?.counts?.scenes || 0, color: 'text-yellow-400' },
              { label: '分镜', count: assets?.counts?.storyboards || 0, color: 'text-blue-400' },
            ].map((stat) => (
              <div key={stat.label} className="bg-white dark:bg-gray-800 rounded-lg p-4 border border-gray-200 dark:border-gray-700">
                <div className={`text-2xl font-bold ${stat.color}`}>{stat.count}</div>
                <div className="text-sm text-gray-500">{stat.label}</div>
              </div>
            ))}
          </div>

          {/* Tab Navigation */}
          <div className="flex flex-wrap gap-2 border-b border-gray-200 dark:border-gray-700 pb-4">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                  activeTab === tab.id
                    ? 'bg-indigo-600 text-white shadow-lg'
                    : 'bg-gray-100 text-gray-600 hover:bg-gray-200 hover:text-gray-900 dark:bg-white/5 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-white'
                }`}
              >
                <span className="text-lg">{tab.icon}</span>
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
            onGoAutopilot={() => setActiveTab('autopilot')}
          />
        )}
        {activeTab === 'autopilot' && (
          <AutopilotTab projectKey={projectKey} novelId={project.novel_id} />
        )}
        {activeTab === 'keyframes' && (
          <KeyframesTab projectKey={projectKey} />
        )}
        {activeTab === 'ninegrid' && (
          <GridPage projectKey={projectKey} />
        )}
        {activeTab === 'storyboard' && (
          <StoryboardTab projectKey={projectKey} />
        )}
        {activeTab === 'tts' && (
          <TtsTab projectKey={projectKey} />
        )}
        {activeTab === 'mix' && (
          <MixTab projectKey={projectKey} />
        )}
        {activeTab === 'qc' && (
          <QcTab projectKey={projectKey} />
        )}
        {activeTab === 'export' && (
          <ExportTab projectKey={projectKey} assets={assets} />
        )}
        {activeTab === 'deliver' && (
          <DeliverTab projectKey={projectKey} onGoAutopilot={() => setActiveTab('autopilot')} />
        )}
        {activeTab === 'upscale' && (
          <UpscaleTab projectKey={projectKey} />
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
            className="sticky top-0 shrink-0 w-11 h-[calc(100vh-7rem)] min-h-[420px] flex flex-col items-center gap-3 py-4 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-500 dark:text-gray-400 hover:text-indigo-600 hover:border-indigo-400 transition-colors"
          >
            <span className="w-7 h-7 rounded-lg bg-indigo-50 dark:bg-indigo-500/15 flex items-center justify-center text-sm">💬</span>
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

// ========== 启动全自动生产（自动生产标签 / 空项目入口共用） ==========
interface StartResult {
  message?: string;
  pending_episodes?: number;
  done_episodes?: number;
  already_done?: boolean;
}

async function startProduction(projectKey: string, novelId?: string): Promise<StartResult> {
  const resp = await fetch('/api/autonomous/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_name: projectKey, novel_id: novelId || '' }),
  });
  const data: any = await resp.json().catch(() => ({}));
  if (!resp.ok || data?.success === false) {
    throw new Error(data?.error || data?.message || '启动失败');
  }
  return data as StartResult;
}

// ========== Overview Tab ==========
function OverviewTab({
  assets,
  projectKey,
  novelId,
  onRefreshAssets,
  onGoAutopilot,
}: {
  assets: ProjectAssets | null;
  projectKey: string;
  novelId?: string;
  onRefreshAssets: () => Promise<void> | void;
  onGoAutopilot: () => void;
}) {
  const [preview, setPreview] = useState<{ item: AssetItem; type: 'character' | 'item' | 'scene' } | null>(null);
  const [starting, setStarting] = useState(false);
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');

  // 缺陷 D3：空项目此前只有一句「会自动生成」提示，却没有可点的入口
  const handleGenerateAssets = async () => {
    setStarting(true);
    setMsg('');
    setErr('');
    try {
      const res = await startProduction(projectKey, novelId);
      setMsg(res.message || '生产已启动，资产将随流水线陆续生成');
      // 生产是异步的，稍后自动回拉一次资产
      setTimeout(() => { onRefreshAssets(); }, 5000);
    } catch (e) {
      setErr(sanitizeError(e, '启动生产失败'));
    } finally {
      setStarting(false);
    }
  };

  const groups: { key: 'characters' | 'items' | 'scenes'; label: string; icon: string; type: 'character' | 'item' | 'scene' }[] = [
    { key: 'characters', label: '角色', icon: '👤', type: 'character' },
    { key: 'items', label: '物品', icon: '📦', type: 'item' },
    { key: 'scenes', label: '场景', icon: '🏞️', type: 'scene' },
  ];

  const total = groups.reduce((n, g) => n + (assets?.gallery?.[g.key]?.length || 0), 0);

  if (total === 0) {
    return (
      <div className="py-12 space-y-6">
        <div className="text-center text-gray-500 dark:text-gray-400">
          <div className="text-4xl mb-3">📁</div>
          <p>暂无资产</p>
          <p className="text-sm mt-1">角色 / 物品 / 场景 会在生产流程中自动生成</p>
        </div>

        <div className="flex flex-col items-center gap-2">
          <Button onClick={handleGenerateAssets} disabled={starting}>
            {starting ? '启动中…' : '开始生产（自动生成角色 / 物品 / 场景）'}
          </Button>
          <button
            onClick={onGoAutopilot}
            className="text-xs text-indigo-500 hover:underline"
          >
            前往「自动生产」查看进度
          </button>
          {msg && <p className="text-sm text-green-600 dark:text-green-400">{msg}</p>}
          {err && <p className="text-sm text-red-500">{err}</p>}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {groups.map((g) => {
        const list = assets?.gallery?.[g.key] || [];
        if (list.length === 0) return null;
        return (
          <div key={g.key}>
            <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 mb-3">
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
      className="text-left bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden hover:shadow-lg transition-shadow"
    >
      <div className="aspect-video bg-gradient-to-br from-gray-100 to-gray-200 dark:from-gray-800 dark:to-gray-900 flex items-center justify-center overflow-hidden">
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
        <h4 className="font-medium text-gray-900 dark:text-white text-sm truncate">{item.name}</h4>
        <p className="text-xs text-gray-500 mt-1">
          {item.view_count ? `${item.view_count} 个视角` : fallbackIcon === '👤' ? '角色' : fallbackIcon === '📦' ? '物品' : '场景'}
        </p>
      </div>
    </button>
  );
}

// ========== Asset Preview Modal ==========
function AssetPreviewModal({
  preview,
  onClose,
}: {
  preview: { item: AssetItem; type: 'character' | 'item' | 'scene' } | null;
  onClose: () => void;
}) {
  const [active, setActive] = useState(0);

  useEffect(() => { setActive(0); }, [preview]);

  if (!preview) return null;
  const { item } = preview;

  // 汇总所有可看的图：主图 + 各视角
  const gallery = [
    item.thumb,
    ...(item.views || []),
  ].filter(Boolean) as { url: string; view?: string; size?: number }[];

  const current = gallery[active];
  const src = assetSrc(current?.url);

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-white dark:bg-gray-800 rounded-xl w-full max-w-3xl max-h-[90vh] overflow-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <h3 className="font-semibold text-lg text-gray-900 dark:text-white">{item.name}</h3>
          <button onClick={onClose} className="text-gray-500 dark:text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 text-2xl leading-none">×</button>
        </div>

        <div className="p-4 space-y-4">
          {src ? (
            <img src={src} alt={item.name} className="w-full rounded-lg bg-gray-100 dark:bg-gray-900" />
          ) : (
            <div className="py-16 text-center text-gray-500 dark:text-gray-400">图片不可用</div>
          )}

          {gallery.length > 1 && (
            <div className="flex gap-2 flex-wrap">
              {gallery.map((g, i) => {
                const t = assetSrc(g.url);
                return (
                  <button
                    key={i}
                    onClick={() => setActive(i)}
                    className={`w-20 h-14 rounded overflow-hidden border-2 ${i === active ? 'border-indigo-500' : 'border-transparent'}`}
                  >
                    {t && <img src={t} alt={g.view || `view-${i}`} className="w-full h-full object-cover" />}
                  </button>
                );
              })}
            </div>
          )}

          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="secondary"
              onClick={() => {
                if (!src) return;
                const a = document.createElement('a');
                a.href = src;
                a.download = `${item.name}${current?.view ? '_' + current.view : ''}.png`;
                a.click();
              }}
            >
              下载当前图
            </Button>
            {current?.size && (
              <span className="text-xs text-gray-500">{(current.size / 1024).toFixed(0)} KB</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ========== Autopilot Tab ==========
function AutopilotTab({ projectKey, novelId }: { projectKey: string; novelId?: string }) {
  const { t } = useApp();
  const [status, setStatus] = useState<any>(null);
  const [progress, setProgress] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(false);
  const [notice, setNotice] = useState('');

  const fetchStatus = async () => {
    setLoading(true);
    setError('');
    try {
      const [s, p] = await Promise.all([
        autopilotApi.status(),
        autopilotApi.progressByProject(projectKey).catch(() => null),
      ]);
      setStatus(s);
      setProgress(p);
    } catch (err) {
      setError(sanitizeError(err, '获取状态失败'));
    } finally {
      setLoading(false);
    }
  };

  const act = async (fn: () => Promise<any>) => {
    setError('');
    setNotice('');
    try {
      await fn();
      setNotice('操作已生效');
      await fetchStatus();
    } catch (err) {
      setError(sanitizeError(err, '操作失败'));
    }
  };

  // 缺陷 D2 / D5：启动生产，并把后端返回的 message 如实展示（不再默默无反馈）
  const handleStart = async () => {
    setStarting(true);
    setError('');
    setNotice('');
    try {
      const res = await startProduction(projectKey, novelId);
      setNotice(res.message || '已启动全自动生产');
      await fetchStatus();
    } catch (e) {
      setError(sanitizeError(e, '启动生产失败'));
    } finally {
      setStarting(false);
    }
  };

  useEffect(() => { fetchStatus(); }, [projectKey]);

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold">自动生产</h3>
        <div className="flex gap-2">
          <Button
            size="sm"
            onClick={handleStart}
            disabled={starting}
            style={{ background: 'linear-gradient(135deg, #6366f1 0%, #4f46e5 100%)' }}
          >
            {starting ? '启动中…' : '启动 / 继续生产'}
          </Button>
          <Button size="sm" variant="secondary" onClick={fetchStatus} disabled={loading}>刷新</Button>
        </div>
      </div>

      {notice && (
        <div className="p-4 bg-green-500/10 border border-green-500/30 rounded-lg text-green-600 dark:text-green-400">
          {notice}
        </div>
      )}

      {error && <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">{error}</div>}

      {status && (
        <div className="grid grid-cols-3 gap-4">
          <div className={`p-4 rounded-lg text-center ${
            status.running ? 'bg-green-500/10 border border-green-500/30' :
            status.paused ? 'bg-yellow-500/10 border border-yellow-500/30' :
            'bg-gray-500/10 border border-gray-500/30'
          }`}>
            <div className="text-3xl mb-2">{status.running ? '🚀' : status.paused ? '⏸️' : '⏹️'}</div>
            <div className="font-bold">
              {status.running ? '运行中' : status.paused ? '已暂停' : '已停止'}
            </div>
          </div>
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-center">
            <div className="text-3xl font-bold text-green-400">{status.totals?.episodes_done || 0}</div>
            <div className="text-sm text-gray-500 dark:text-gray-400">已完成剧集</div>
          </div>
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-center">
            <div className="text-3xl font-bold text-red-400">{status.totals?.episodes_failed || 0}</div>
            <div className="text-sm text-gray-500 dark:text-gray-400">失败剧集</div>
          </div>
        </div>
      )}

      {progress && (
        <div className="mb-6">
          <div className="flex justify-between text-sm text-gray-500 dark:text-gray-400 mb-2">
            <span>进度</span>
            <span>{progress.done}/{progress.total_episodes}</span>
          </div>
          <div className="h-4 bg-gray-700 rounded-full overflow-hidden">
            <div
              className="h-full bg-gradient-to-r from-indigo-500 to-purple-500 transition-all"
              style={{ width: `${progress.total_episodes > 0 ? (progress.done / progress.total_episodes * 100) : 0}%` }}
            />
          </div>
        </div>
      )}

      {status && (
        <div className="flex gap-4">
          {!status?.running && !status?.paused && (
            <Button
              onClick={() => act(() => autopilotApi.enable())}
              style={{ background: 'linear-gradient(135deg, #10b981 0%, #059669 100%)' }}
            >启用</Button>
          )}
          {status?.running && (
            <Button
              variant="secondary"
              onClick={() => act(() => autopilotApi.pause())}
              style={{ background: 'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)' }}
            >暂停</Button>
          )}
          {status?.paused && (
            <Button
              variant="secondary"
              onClick={() => act(() => autopilotApi.resume())}
              style={{ background: 'linear-gradient(135deg, #3b82f6 0%, #2563eb 100%)' }}
            >恢复</Button>
          )}
          {(status?.running || status?.paused) && (
            <Button
              variant="danger"
              onClick={() => act(() => autopilotApi.disable())}
              style={{ background: 'linear-gradient(135deg, #ef4444 0%, #dc2626 100%)' }}
            >停止</Button>
          )}
        </div>
      )}
    </div>
  );
}

// ========== Keyframes Tab ==========
function KeyframesTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
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
      alert(`尾帧生成任务已启动: ${result.task_id}`);
      setTimeout(fetchPlan, 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成失败');
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
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-center">
            <div className="text-3xl font-bold text-indigo-400">{plan.shot_count}</div>
            <div className="text-sm text-gray-500 dark:text-gray-400">总镜头数</div>
          </div>
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-center">
            <div className="text-3xl font-bold text-green-400">{plan.start_frames_ready}</div>
            <div className="text-sm text-gray-500 dark:text-gray-400">首帧就绪</div>
          </div>
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-center">
            <div className="text-3xl font-bold text-blue-400">{plan.end_frames_ready}</div>
            <div className="text-sm text-gray-500 dark:text-gray-400">尾帧就绪</div>
          </div>
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-center">
            <div className="text-3xl font-bold text-yellow-400">{plan.to_generate}</div>
            <div className="text-sm text-gray-500 dark:text-gray-400">待生成</div>
          </div>
        </div>
      )}

      {plan && plan.to_generate > 0 && (
        <Button
          onClick={handleGenerate}
          disabled={generating}
          style={{ background: 'linear-gradient(135deg, #f59e0b 0%, #ef4444 100%)' }}
          className="w-full py-3"
        >
          {generating ? '生成中...' : '生成尾帧'}
        </Button>
      )}

      {plan && (
        <div className="space-y-2">
          <h4 className="font-semibold text-gray-700 dark:text-gray-300 mb-3">镜头列表</h4>
          {plan.plan?.map((shot: any) => (
            <div
              key={shot.seq}
              className={`flex items-center gap-4 p-3 rounded-lg ${
                shot.need_gen ? 'bg-yellow-500/10 border border-yellow-500/30' :
                shot.has_end ? 'bg-green-500/10 border border-green-500/30' :
                'bg-gray-50 dark:bg-white/5'
              }`}
            >
              <span className="w-12 font-mono text-gray-500 dark:text-gray-400">#{shot.seq}</span>
              <span className="flex-1">{shot.status || shot.shot_id}</span>
              <div className="flex gap-2">
                {shot.has_start && <span className="px-2 py-1 bg-green-500/20 text-green-400 rounded text-xs">首帧</span>}
                {shot.has_end && <span className="px-2 py-1 bg-blue-500/20 text-blue-400 rounded text-xs">尾帧</span>}
                {shot.need_gen && !shot.has_end && <span className="px-2 py-1 bg-yellow-500/20 text-yellow-400 rounded text-xs">待生成</span>}
              </div>
              {shot.url && (
                <a href={shot.url} target="_blank" rel="noopener noreferrer" className="text-indigo-400 hover:text-indigo-300">查看</a>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ========== Storyboard Tab ==========
function StoryboardTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const [cards, setCards] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const fetchCanvas = async () => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      const data = await storyboardApi.canvas(projectKey);
      setCards(data.cards || []);
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

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold">分镜管理</h3>
        <Button size="sm" onClick={fetchCanvas} disabled={loading}>刷新</Button>
      </div>

      {error === 'no-data' && (
        <div className="py-12 text-center text-gray-500 dark:text-gray-400">
          <div className="text-4xl mb-3">🎬</div>
          <p>暂无分镜数据，请先进行剧本生成</p>
        </div>
      )}

      {error && error !== 'no-data' && (
        <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">{error}</div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {cards.map((card: any) => (
          <div key={card.seq} className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="font-mono text-sm text-gray-500 dark:text-gray-400">#{card.seq}</span>
              <span className="text-xs text-gray-500">{card.camera}</span>
            </div>
            <p className="text-sm text-gray-700 dark:text-gray-300 mb-2">{card.description}</p>
            {card.dialogue_text && (
              <p className="text-xs text-gray-500 italic">"{card.dialogue_text}"</p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ========== TTS Tab ==========
function TtsTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const [env, setEnv] = useState<any>(null);
  const [plan, setPlan] = useState<any>(null);
  const [tasks, setTasks] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');

  const fetchEnv = async () => {
    if (!projectKey) return;
    setLoading(true);
    try {
      const data = await ttsApi.env(projectKey);
      setEnv(data);
    } catch (err) {
      console.error('Failed to fetch TTS env:', err);
    } finally {
      setLoading(false);
    }
  };

  const fetchPlan = async () => {
    if (!projectKey) return;
    setLoading(true);
    try {
      const data = await ttsApi.plan({ project_name: projectKey });
      setPlan(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取计划失败');
    } finally {
      setLoading(false);
    }
  };

  const handleGenerate = async () => {
    if (!projectKey) return;
    setGenerating(true);
    setError('');
    try {
      const result = await ttsApi.generate({ project_name: projectKey });
      alert(`配音生成任务已启动: ${result.task_id}`);
      fetchPlan();
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成失败');
    } finally {
      setGenerating(false);
    }
  };

  useEffect(() => {
    fetchEnv();
    fetchPlan();
  }, [projectKey]);

  return (
    <div className="space-y-6">
      <h3 className="text-lg font-semibold">TTS 配音</h3>

      {env && (
        <div className={`p-4 rounded-lg mb-4 ${env.available ? 'bg-green-500/10 border border-green-500/30' : 'bg-red-500/10 border border-red-500/30'}`}>
          <div className="flex items-center gap-2">
            <span className="text-2xl">{env.available ? '✅' : '❌'}</span>
            <span className="font-semibold">{env.available ? 'TTS 环境可用' : 'TTS 环境不可用'}</span>
          </div>
        </div>
      )}

      {plan && (
        <div className="mb-6">
          <h4 className="font-semibold text-gray-700 dark:text-gray-300 mb-3">配音计划预览</h4>
          <div className="grid grid-cols-3 gap-4 mb-4">
            <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-3 text-center">
              <div className="text-2xl font-bold text-indigo-400">{plan.line_count ?? (plan.lines?.length ?? 0)}</div>
              <div className="text-sm text-gray-500 dark:text-gray-400">总台词数</div>
            </div>
            <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-3 text-center">
              <div className="text-2xl font-bold text-green-400">{plan.characters?.length || 0}</div>
              <div className="text-sm text-gray-500 dark:text-gray-400">涉及角色</div>
            </div>
            <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-3 text-center">
              <div className="text-2xl font-bold text-blue-400">{plan.episode}</div>
              <div className="text-sm text-gray-500 dark:text-gray-400">集数</div>
            </div>
          </div>

          <Button
            onClick={handleGenerate}
            disabled={generating || !env?.available}
            style={{ background: 'linear-gradient(135deg, #10b981 0%, #059669 100%)' }}
            className="w-full py-3"
          >
            {generating ? '生成中...' : '生成配音'}
          </Button>
        </div>
      )}

      {error && <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">{error}</div>}
    </div>
  );
}

// ========== Mix Tab ==========
function MixTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const [plan, setPlan] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');

  const fetchPlan = async () => {
    if (!projectKey) return;
    setLoading(true);
    try {
      const data = await mixApi.plan({ project_name: projectKey });
      setPlan(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取计划失败');
    } finally {
      setLoading(false);
    }
  };

  const handleGenerate = async () => {
    if (!projectKey) return;
    setGenerating(true);
    setError('');
    try {
      const result = await mixApi.generate({ project_name: projectKey });
      alert(`混音任务已启动: ${result.task_id}`);
      fetchPlan();
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成失败');
    } finally {
      setGenerating(false);
    }
  };

  useEffect(() => { fetchPlan(); }, [projectKey]);

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold">音画混音</h3>
        <Button size="sm" onClick={fetchPlan} disabled={loading}>刷新</Button>
      </div>

      {error && <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">{error}</div>}

      <Button
        onClick={handleGenerate}
        disabled={generating}
        style={{ background: 'linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%)' }}
        className="w-full py-3"
      >
        {generating ? '生成中...' : '开始混音'}
      </Button>
    </div>
  );
}

// ========== QC Tab ==========
function QcTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const [config, setConfig] = useState<any>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const fetchData = async () => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      const data = await qcApi.history(projectKey);
      if (data.success) {
        setConfig(data.config || null);
        setHistory((data.history as any[]) || []);
        setStats(data.stats || null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchData(); }, [projectKey]);

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold">质量检查</h3>
        <Button size="sm" onClick={fetchData} disabled={loading}>刷新</Button>
      </div>

      {error && <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">{error}</div>}

      {stats && (
        <div className="grid grid-cols-4 gap-4">
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-center">
            <div className="text-3xl font-bold text-gray-500 dark:text-gray-400">{stats.total}</div>
            <div className="text-sm text-gray-500">总检查数</div>
          </div>
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-center">
            <div className="text-3xl font-bold text-green-400">{stats.passed}</div>
            <div className="text-sm text-gray-500">通过</div>
          </div>
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-center">
            <div className="text-3xl font-bold text-red-400">{stats.failed}</div>
            <div className="text-sm text-gray-500">失败</div>
          </div>
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-center">
            <div className="text-3xl font-bold text-yellow-400">{stats.retry_count}</div>
            <div className="text-sm text-gray-500">重试次数</div>
          </div>
        </div>
      )}

      {history.length > 0 && (
        <div className="space-y-2">
          <h4 className="font-semibold text-gray-700 dark:text-gray-300 mb-3">检查历史</h4>
          {history.map((item: any) => (
            <div
              key={item.seq}
              className={`flex items-center gap-4 p-3 rounded-lg ${
                item.verdict === 'pass' ? 'bg-green-500/10 border border-green-500/30' :
                item.verdict === 'fail' ? 'bg-red-500/10 border border-red-500/30' :
                'bg-yellow-500/10 border border-yellow-500/30'
              }`}
            >
              <span className="w-12 font-mono text-gray-500 dark:text-gray-400">#{item.seq}</span>
              <span className="flex-1">{item.shot_id}</span>
              <span className="text-lg font-bold text-gray-900 dark:text-white">{item.score}</span>
              <span className={`px-2 py-1 rounded text-xs ${
                item.verdict === 'pass' ? 'bg-green-500/20 text-green-400' :
                item.verdict === 'fail' ? 'bg-red-500/20 text-red-400' :
                'bg-yellow-500/20 text-yellow-400'
              }`}>
                {item.verdict === 'pass' ? '通过' : item.verdict === 'fail' ? '失败' : '重试'}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ========== Export Tab ==========
function ExportTab({ projectKey, assets }: { projectKey: string; assets: ProjectAssets | null }) {
  const { t } = useApp();
  const [files, setFiles] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const loadFiles = async () => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      const data = await exportApi.listFiles(projectKey);
      setFiles(data.files || []);
    } catch (err) {
      setError(sanitizeError(err, '加载导出文件失败'));
    } finally {
      setLoading(false);
    }
  };

  const handleGenerate = async () => {
    setGenerating(true);
    setError('');
    setNotice('');
    try {
      const data = await exportApi.generate(projectKey, ['fcpml', 'edl', 'json']);
      setFiles(data.files || []);
      // 明确告知镜头数：0 镜导出出来就是空时间轴，避免用户下载后才发现是空文件。
      if (typeof data.shot_count === 'number' && data.shot_count === 0) {
        setNotice('导出文件已生成，但本项目还没有分镜／视频，导出内容为空');
      } else if (typeof data.shot_count === 'number') {
        setNotice(`导出文件已生成，共 ${data.shot_count} 个镜头（约 ${data.total_sec ?? 0} 秒）`);
      } else {
        setNotice('导出文件已生成');
      }
    } catch (e) {
      setError(sanitizeError(e, '生成导出文件失败'));
    } finally {
      setGenerating(false);
    }
  };

  useEffect(() => { loadFiles(); }, [projectKey]);

  // 统一的下行下载：用 <a download> 触发，避免 window.open 被浏览器弹窗拦截后
  // 表现为「点了没反应」（这正是缺陷 D4 的表象）。
  const triggerDownload = (url: string, filename?: string) => {
    const a = document.createElement('a');
    a.href = url;
    if (filename) a.download = filename;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  // 成片下载（缺陷 D4）：此前下载按钮是纯占位、没有 onClick，这里接上真实下载
  const finals = assets?.final || [];
  const finalUrl = (item: AssetItem) =>
    `/api/final/${encodeURIComponent(`${item.project_dir || projectKey}/${item.name}`)}`;

  const handleDownloadFinal = (item: AssetItem) => {
    triggerDownload(`${finalUrl(item)}?download=1`, item.name);
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold">导出</h3>
        <div className="flex gap-2">
          <Button size="sm" onClick={handleGenerate} disabled={generating}>
            {generating ? '生成中…' : '生成导出文件'}
          </Button>
          <Button size="sm" variant="secondary" onClick={loadFiles} disabled={loading}>刷新</Button>
        </div>
      </div>

      {notice && (
        <div className="p-3 bg-green-500/10 border border-green-500/30 rounded-lg text-green-600 dark:text-green-400 text-sm">
          {notice}
        </div>
      )}
      {error && <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">{error}</div>}

      {/* 成片下载 */}
      <div>
        <h4 className="text-sm font-semibold text-gray-500 dark:text-gray-400 mb-2">🎥 成片下载 · {finals.length}</h4>
        {finals.length > 0 ? (
          <div className="space-y-2">
            {finals.map((item, idx) => (
              <div key={idx} className="flex items-center justify-between p-3 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
                <div className="min-w-0">
                  <p className="font-medium text-gray-900 dark:text-white truncate">{item.name}</p>
                  {item.size ? (
                    <p className="text-xs text-gray-500">{(item.size / 1024 / 1024).toFixed(1)} MB</p>
                  ) : null}
                </div>
                <div className="flex gap-2 shrink-0">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => window.open(finalUrl(item), '_blank')}
                  >
                    预览
                  </Button>
                  <Button size="sm" onClick={() => handleDownloadFinal(item)}>下载</Button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-gray-500 dark:text-gray-400 py-4">暂无成片，请先完成视频生成与合成</p>
        )}
      </div>

      {/* 剪辑工程文件 */}
      <div>
        <h4 className="text-sm font-semibold text-gray-500 dark:text-gray-400 mb-2">
          🎞️ 剪辑工程文件 · {files.filter(f => f.exists).length}
        </h4>
        {files.filter(f => f.exists).length > 0 ? (
          <div className="space-y-2">
            {files.filter(f => f.exists).map((file: any, idx: number) => (
              <div key={idx} className="flex items-center justify-between p-3 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
                <div className="min-w-0">
                  <p className="font-medium text-gray-900 dark:text-white truncate">{file.filename}</p>
                  <p className="text-xs text-gray-500">{String(file.format || '').toUpperCase()}</p>
                </div>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => triggerDownload(`/api/export/${encodeURIComponent(projectKey)}/${file.format}`, file.filename)}
                >
                  下载
                </Button>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-gray-500 dark:text-gray-400 py-4">暂无导出文件，点击右上角「生成导出文件」</p>
        )}
      </div>
    </div>
  );
}

// ========== 成品验收 Tab ==========
// 后端能力（app.py + pipeline.py）：
//   GET  /api/autopilot/deliverables?project=X  按集返回成片索引
//   POST /api/autopilot/deliverables/review     置验收/打回（打回 → 下轮托管自动重跑该集）
//   GET  /api/autopilot/deliverable/file/...    播放；加 ?download=1 下载
//   条目自带 exists（文件是否真在磁盘）与 url（后端已防目录穿越）
//
// ⚠️ 不要沿用已删除的全局 DeliverPage 的写法：它 fetch 了 deliverables 却从不渲染，
//    下载靠「猜文件名」（final.mp4 / output.mp4 / 项目名.mp4），也不按项目过滤。
function DeliverTab({ projectKey, onGoAutopilot }: { projectKey: string; onGoAutopilot: () => void }) {
  const { t } = useApp();
  const [items, setItems] = useState<Deliverable[]>([]);
  const [pending, setPending] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  /** 正在提交验收/打回的集号（防重复点击） */
  const [busy, setBusy] = useState<number | null>(null);
  /** 正在填写打回原因的集号 */
  const [rejecting, setRejecting] = useState<number | null>(null);
  const [reason, setReason] = useState('');
  /** 正在内联播放的文件名 */
  const [playing, setPlaying] = useState<string | null>(null);
  const [toast, setToast] = useState('');

  const load = React.useCallback(async () => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      const d = await autopilotApi.deliverables(projectKey);
      setItems(d.items || []);
      setPending(d.pending || 0);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('deliver.actionFailed'));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [projectKey, t]);

  useEffect(() => { void load(); }, [load]);

  // 验收 / 打回。字段名必须与后端一致（见 client.ts 注释）——
  // 这里曾因写成 {deliverable, verdict} 导致接口一直 400，功能从未生效。
  const review = async (episodeNo: number, verdict: 'accepted' | 'rejected', note = '') => {
    setBusy(episodeNo);
    setError('');
    try {
      await autopilotApi.reviewDeliverable({
        project: projectKey,
        episode_no: episodeNo,
        review: verdict,
        note,
      });
      setToast(verdict === 'accepted' ? t('deliver.acceptedOk') : t('deliver.rejectedOk'));
      setRejecting(null);
      setReason('');
      await load();
      window.setTimeout(() => setToast(''), 3200);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('deliver.actionFailed'));
    } finally {
      setBusy(null);
    }
  };

  const sizeText = (n?: number) => (n ? `${(n / 1048576).toFixed(1)} MB` : '—');

  const statusBadge = (d: Deliverable) => {
    if (d.review === 'accepted') {
      return { text: t('deliver.accepted'), cls: 'bg-green-100 text-green-700 dark:bg-green-500/20 dark:text-green-300' };
    }
    if (d.review === 'rejected') {
      return { text: t('deliver.rejected'), cls: 'bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-300' };
    }
    return { text: t('deliver.pending'), cls: 'bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300' };
  };

  if (loading) return <Loading />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white">{t('deliver.title')}</h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
            {items.length > 0
              ? t('deliver.counts', { total: items.length, pending })
              : t('deliver.subtitle')}
          </p>
        </div>
        <Button size="sm" variant="secondary" onClick={load} disabled={loading || busy !== null}>
          {t('common.refresh')}
        </Button>
      </div>

      {error && (
        <div className="p-3 bg-red-50 dark:bg-red-500/10 border border-red-200 dark:border-red-500/30 rounded-lg text-sm text-red-700 dark:text-red-300">
          {error}
        </div>
      )}
      {toast && (
        <div className="p-3 bg-green-50 dark:bg-green-500/10 border border-green-200 dark:border-green-500/30 rounded-lg text-sm text-green-700 dark:text-green-300">
          {toast}
        </div>
      )}

      {items.length === 0 ? (
        <div className="text-center py-12">
          <div className="text-5xl mb-4">📦</div>
          <h4 className="text-lg font-medium text-gray-900 dark:text-white mb-1">{t('deliver.noItems')}</h4>
          <p className="text-sm text-gray-500 dark:text-gray-400 mb-5">{t('deliver.noItemsTip')}</p>
          <Button onClick={onGoAutopilot}>{t('deliver.goAutopilot')}</Button>
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((d) => {
            const b = statusBadge(d);
            const canReview = busy === null;
            const playable = !!d.url && d.exists !== false;
            return (
              <div
                key={`${d.project}-${d.episode_no}`}
                className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-gray-900 dark:text-white">
                        {t('deliver.episodeNo', { n: d.episode_no })}
                      </span>
                      {d.meta?.title && (
                        <span className="text-sm text-gray-500 dark:text-gray-400">{d.meta.title}</span>
                      )}
                      <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${b.cls}`}>{b.text}</span>
                      {d.exists === false && (
                        <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-300">
                          {t('deliver.missing')}
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-1.5">
                      {d.filename} · {sizeText(d.size)}
                    </p>
                    {d.exists === false && (
                      <p className="text-xs text-red-600 dark:text-red-400 mt-1">{t('deliver.missingHint')}</p>
                    )}
                    {/* 注意字段名是 review_note（后端 pipeline.set_deliverable_review 写的） */}
                    {d.review === 'rejected' && d.review_note && (
                      <p className="text-xs text-gray-600 dark:text-gray-300 mt-1">
                        {t('deliver.note')}: {d.review_note}
                      </p>
                    )}
                  </div>

                  <div className="flex gap-2 shrink-0 flex-wrap justify-end">
                    {playable && (
                      <>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => setPlaying(playing === d.filename ? null : d.filename)}
                        >
                          {playing === d.filename ? t('common.close') : t('deliver.play')}
                        </Button>
                        <a
                          href={`${d.url}?download=1`}
                          className="inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
                        >
                          {t('common.download')}
                        </a>
                      </>
                    )}
                    <Button
                      size="sm"
                      onClick={() => review(d.episode_no, 'accepted')}
                      disabled={!canReview || d.review === 'accepted'}
                    >
                      {t('deliver.accept')}
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => {
                        setRejecting(rejecting === d.episode_no ? null : d.episode_no);
                        setReason('');
                      }}
                      disabled={!canReview || d.review === 'rejected'}
                    >
                      {t('deliver.reject')}
                    </Button>
                  </div>
                </div>

                {playing === d.filename && d.url && (
                  <video src={d.url} controls className="w-full mt-3 rounded-lg bg-black" />
                )}

                {rejecting === d.episode_no && (
                  <div className="mt-3 pt-3 border-t border-gray-200 dark:border-gray-700 space-y-2">
                    <label className="block text-xs text-gray-600 dark:text-gray-300">
                      {t('deliver.rejectReason')}
                    </label>
                    <textarea
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                      rows={2}
                      placeholder={t('deliver.rejectReasonPlaceholder')}
                      className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-white"
                    />
                    <div className="flex gap-2">
                      <Button size="sm" onClick={() => review(d.episode_no, 'rejected', reason)} disabled={!canReview}>
                        {t('deliver.reject')}
                      </Button>
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => {
                          setRejecting(null);
                          setReason('');
                        }}
                      >
                        {t('common.cancel')}
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ========== 超分（FlashVSR） ==========
// 接口：
//   GET  /api/upscale/env             链路自检（ComfyUI 在线 / 模型 / 节点）
//   GET  /api/upscale/sources         候选输入视频（成片 / 片段 / 已有超分 / ComfyUI）
//   POST /api/upscale/video           {project_name, video_path, scale, attach_audio} -> task_id
//   GET  /api/upscale/status/<id>     轮询进度
//   GET  /api/upscale/list            该项目已生成的超分产物
//
// ⚠️ attach_audio 必须传 true：后端 TE-Speed 链路默认 attach_audio=False，
//    对「成片」超分会把已合成的 TTS 配音整轨丢掉（backend 侧该参数此前也不在白名单，
//    已一并补上）。
//
// ⚠️ 可下载性取决于 URL 前缀：只有 /api/upscale/<project>/<name> 支持 ?download=1；
//    ComfyUI 侧来源走 /api/upscale/comfyview 是 302 重定向，不能直接当附件下载。
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
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white">{t('upscale.title')}</h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">{t('upscale.subtitle')}</p>
        </div>
        <Button size="sm" variant="secondary" onClick={load} disabled={loading || busy}>
          {t('common.refresh')}
        </Button>
      </div>

      {/* 链路自检 */}
      <div
        className={`p-3 rounded-lg border text-sm ${
          ready
            ? 'bg-green-50 dark:bg-green-500/10 border-green-200 dark:border-green-500/30 text-green-700 dark:text-green-300'
            : 'bg-amber-50 dark:bg-amber-500/10 border-amber-200 dark:border-amber-500/30 text-amber-700 dark:text-amber-300'
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
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="text-sm font-medium text-gray-900 dark:text-white">
                {t('upscale.planToggle')}
              </p>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                {t('upscale.planToggleHint')}
              </p>
            </div>
            <div className="flex items-center gap-3 shrink-0">
              <span className={`text-xs font-medium ${planOn ? 'text-green-600 dark:text-green-400' : 'text-gray-500 dark:text-gray-400'}`}>
                {planOn ? t('upscale.on') : t('upscale.off')}
              </span>
              <button
                role="switch"
                aria-checked={planOn}
                disabled={savingPlan}
                onClick={() => savePlan({ enable_upscale: !planOn })}
                className={`relative w-11 h-6 rounded-full transition-colors disabled:opacity-50 ${
                  planOn ? 'bg-indigo-600' : 'bg-gray-300 dark:bg-gray-600'
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
          <div className="mt-3 pt-3 border-t border-gray-100 dark:border-gray-700 flex items-center gap-3">
            <span className="text-xs text-gray-600 dark:text-gray-300">{t('upscale.planScale')}</span>
            <div className="flex gap-2">
              {([2, 3, 4] as const).map((n) => (
                <button
                  key={n}
                  disabled={savingPlan || !planOn}
                  onClick={() => savePlan({ upscale_scale: n })}
                  className={`px-3 py-1 rounded-md text-xs font-medium transition-all disabled:opacity-50 ${
                    planScale === n
                      ? 'bg-indigo-600 text-white'
                      : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-white/5 dark:text-gray-400 dark:hover:bg-white/10'
                  }`}
                >
                  {t('upscale.scaleTimes', { n })}
                </button>
              ))}
            </div>
            <span className="text-xs text-gray-400 dark:text-gray-500">{t('upscale.planScaleHint')}</span>
          </div>
        </div>
      )}

      {error && (
        <div className="p-3 bg-red-50 dark:bg-red-500/10 border border-red-200 dark:border-red-500/30 rounded-lg text-sm text-red-700 dark:text-red-300">
          {error}
        </div>
      )}

      {/* 选择源 + 倍率 + 发起 */}
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 space-y-4">
        {sources.length === 0 ? (
          <div className="text-center py-8">
            <div className="text-4xl mb-3">🔍</div>
            <h4 className="text-base font-medium text-gray-900 dark:text-white mb-1">{t('upscale.sourceEmpty')}</h4>
            <p className="text-sm text-gray-500 dark:text-gray-400">{t('upscale.sourceEmptyTip')}</p>
          </div>
        ) : (
          <>
            <div className="grid md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1.5">
                  {t('upscale.selectSource')}
                </label>
                <select
                  value={selected}
                  onChange={(e) => { setSelected(e.target.value); setPlaying(null); }}
                  disabled={busy}
                  className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-white"
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
                <label className="mt-2 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={showComfy}
                    onChange={(e) => setShowComfy(e.target.checked)}
                    className="rounded border-gray-300 dark:border-gray-600"
                  />
                  {t('upscale.showComfy', {
                    n: sources.length - sources.filter((s) => !s.kind.startsWith('ComfyUI')).length,
                  })}
                </label>
                {source && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1.5">
                    {source.mtime} · {source.size_mb} MB
                  </p>
                )}
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1.5">
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
                          ? 'bg-indigo-600 text-white shadow-lg'
                          : 'bg-gray-100 text-gray-600 hover:bg-gray-200 hover:text-gray-900 dark:bg-white/5 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-white'
                      }`}
                    >
                      {t('upscale.scaleTimes', { n })}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1.5">
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
                <span className="text-xs text-amber-600 dark:text-amber-400 self-center">
                  {t('upscale.notReadyTip')}
                </span>
              )}
            </div>
          </>
        )}
      </div>

      {/* 任务进度 / 结果 */}
      {task && (
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 space-y-3">
          <div className="flex items-center justify-between gap-3">
            <span className="font-semibold text-gray-900 dark:text-white">{statusText()}</span>
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {t('upscale.progress')} {task.progress || 0}%
            </span>
          </div>

          <div className="h-2 rounded-full bg-gray-100 dark:bg-gray-700 overflow-hidden">
            <div
              className={`h-full transition-all ${
                task.status === 'error' ? 'bg-red-500' : 'bg-indigo-600'
              }`}
              style={{ width: `${Math.min(100, task.progress || 0)}%` }}
            />
          </div>

          {task.message && (
            <p className="text-xs text-gray-500 dark:text-gray-400">{task.message}</p>
          )}
          {task.status === 'error' && task.error && (
            <p className="text-xs text-red-600 dark:text-red-400 break-all">{task.error}</p>
          )}

          {task.status === 'done' && res && (
            <div className="pt-3 border-t border-gray-200 dark:border-gray-700 space-y-3">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                <div>
                  <div className="text-gray-500 dark:text-gray-400">{t('upscale.before')}</div>
                  <div className="font-medium text-gray-900 dark:text-white">{fmtResolution(res.before)}</div>
                </div>
                <div>
                  <div className="text-gray-500 dark:text-gray-400">{t('upscale.after')}</div>
                  <div className="font-medium text-gray-900 dark:text-white">{fmtResolution(res.after)}</div>
                </div>
                <div>
                  <div className="text-gray-500 dark:text-gray-400">{t('upscale.elapsed')}</div>
                  <div className="font-medium text-gray-900 dark:text-white">
                    {res.elapsed_sec != null ? `${res.elapsed_sec}s` : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-gray-500 dark:text-gray-400">{t('upscale.resolution')}</div>
                  <div className="font-medium text-gray-900 dark:text-white">
                    {res.after?.size_mb != null ? `${res.after.size_mb} MB` : '—'}
                  </div>
                </div>
              </div>

              {/* 音轨保留情况：无声超分是这里最容易踩的坑 */}
              <p className={`text-xs ${res.after?.has_audio ? 'text-green-600 dark:text-green-400' : 'text-amber-600 dark:text-amber-400'}`}>
                {res.after?.has_audio ? t('upscale.audioKept') : t('upscale.audioLost')}
              </p>

              {res.output_url && (
                <>
                  <video src={res.output_url} controls className="w-full rounded-lg bg-black" />
                  <div className="flex gap-2">
                    <a
                      href={downloadUrl(res.output_url) || res.output_url}
                      className="inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
                    >
                      {t('common.download')}
                    </a>
                    <span className="text-xs text-gray-500 dark:text-gray-400 self-center">
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
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <h4 className="text-sm font-semibold text-gray-900 dark:text-white mb-3">
            {t('upscale.artifacts')}（{artifacts.length}）
          </h4>
          <div className="space-y-2">
            {artifacts.map((a) => (
              <div
                key={a.name}
                className="flex items-center justify-between gap-3 py-2 border-b border-gray-100 dark:border-gray-700 last:border-0"
              >
                <div className="min-w-0">
                  <p className="text-sm text-gray-900 dark:text-white truncate">{a.name}</p>
                  <p className="text-xs text-gray-500 dark:text-gray-400">{a.mtime} · {a.size_mb} MB</p>
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
                      className="inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
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
      const deadline = Date.now() + 35 * 60 * 1000;   // 兜底，避免异常时永久轮询
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
      className="w-[340px] shrink-0 flex flex-col rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 overflow-hidden sticky top-0 h-[calc(100vh-7rem)] min-h-[420px]"
    >
      {/* 头部：与工作台其他面板一致的白底 + 灰边 + indigo 强调 */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700 shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="w-7 h-7 rounded-lg bg-indigo-50 dark:bg-indigo-500/15 flex items-center justify-center text-sm shrink-0">
            💬
          </span>
          <div className="min-w-0">
            <h3 className="font-semibold text-gray-900 dark:text-white leading-tight">AI总控</h3>
            <div className="flex items-center gap-1.5 mt-0.5">
              <p className="text-[11px] text-gray-500 dark:text-gray-400 leading-tight">
                {autoMode ? `自主执行 · ${toolCount || '…'} 个功能` : '仅对话'}
              </p>
              <button
                onClick={() => setAutoMode(v => !v)}
                title={autoMode ? '切回纯聊天（不执行动作）' : '切到自主执行（总控自己干活）'}
                className={`text-[10px] leading-none px-1.5 py-0.5 rounded border transition-colors ${
                  autoMode
                    ? 'border-indigo-300 text-indigo-600 dark:text-indigo-400 bg-indigo-50 dark:bg-indigo-500/15'
                    : 'border-gray-300 dark:border-gray-600 text-gray-500 dark:text-gray-400'
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
                  ? 'text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/15'
                  : 'text-gray-500 dark:text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-500/15'
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
            className="p-1.5 rounded-lg text-gray-500 dark:text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 dark:hover:bg-indigo-500/15 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
          <button
            onClick={onClose}
            title="收起面板"
            className="p-1.5 rounded-lg text-gray-500 dark:text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 dark:hover:bg-indigo-500/15 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 5l7 7-7 7M5 5l7 7-7 7" />
            </svg>
          </button>
        </div>
      </div>

      {error && (
        <div className="mx-3 mt-3 p-2 bg-red-500/10 border border-red-500/30 rounded-lg text-red-500 dark:text-red-400 text-xs shrink-0">
          {error}
        </div>
      )}

      {/* 消息区：浅色底以区别于面板头部/输入区，形成「对话」区域感。
          注意：空态与消息列表要二选一渲染 —— 若把滚动哨兵 <div> 和 h-full 的空态
          放在同一个 space-y-3 容器里，哨兵会额外吃到 12px margin 而撑出滚动条。 */}
      <div className="flex-1 min-h-0 overflow-y-auto bg-gray-50 dark:bg-gray-900/40">
        {messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center px-4">
            <div className="w-11 h-11 rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 flex items-center justify-center text-lg mb-3">
              💬
            </div>
            <p className="text-sm text-gray-600 dark:text-gray-300">
              {autoMode ? '说一句话，总控自己决定并执行' : '和总控聊聊创作想法'}
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 mb-4">
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
                  className="w-full text-left text-xs px-3 py-2 rounded-lg bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:border-indigo-400 hover:text-indigo-600 dark:hover:text-indigo-400 transition-colors"
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
                    ? 'bg-indigo-600 text-white ml-6 rounded-br-sm'
                    : 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-200 mr-6 rounded-bl-sm'
                }`}
              >
                <p className="whitespace-pre-wrap break-words">{msg.content}</p>
              </div>
            ))}
            {/* 自主执行过程：把总控「自己调了哪些功能、成功没有」透明地摊开 */}
            {run && (
              <div className="mr-6 px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 space-y-1.5">
                <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                  {run.status === 'running' ? (
                    <span className="w-3 h-3 border-2 border-indigo-400/40 border-t-indigo-500 rounded-full animate-spin inline-block shrink-0" />
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
                    <span className="font-mono text-gray-500 dark:text-gray-400 shrink-0">{s.tool}</span>
                    <span className="text-gray-600 dark:text-gray-300 break-all">
                      {s.summary}
                      {s.cached ? '（复用缓存）' : ''}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {sending && !run && (
              <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 mr-6 px-3 py-2 rounded-lg text-sm text-gray-500 dark:text-gray-400 flex items-center gap-2">
                <span className="w-3 h-3 border-2 border-indigo-400/40 border-t-indigo-500 rounded-full animate-spin inline-block" />
                思考中...
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* 输入区 */}
      <div className="p-3 border-t border-gray-200 dark:border-gray-700 shrink-0 bg-white dark:bg-gray-800">
        <div className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') sendMessage(); }}
            placeholder="输入消息..."
            className="flex-1 min-w-0 px-3 py-2 text-sm rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:ring-2 focus:ring-indigo-500/30 focus:border-indigo-500 outline-none transition-colors"
          />
          <button
            onClick={sendMessage}
            disabled={sending || !input.trim()}
            className="px-3 py-2 rounded-lg text-sm font-medium bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
          >
            发送
          </button>
        </div>
      </div>
    </aside>
  );
}
