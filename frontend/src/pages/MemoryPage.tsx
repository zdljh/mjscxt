import React, { useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { memoryApi, projectsApi } from '@/api/client';
import { Card, Button, Loading, EmptyState, Badge, ConfirmDialog, Input, Select } from '@/components/ui';
import { useToast } from '@/components/ui/toast';
import type { Memory, MemoryType, MemoryStats, PromptLesson, LessonPage, Project } from '@/types';

/**
 * 教训类型 → 人话（避免界面出现原始 kind 值）。
 * ⚠️ 与 ProjectWorkbenchPage 里的 KIND_LABEL 是两份独立的映射（那份管「质检品类」，
 * 这份管「教训环节」），7 类中文标签按设计文档 §7 第 7 条单一事实源对齐：
 *   asset=资产 / storyboard=分镜 / video=视频 / keyframe=尾帧 /
 *   audio=配音 / script=剧本 / prompt=提示词预检
 */
const LESSON_KIND_LABEL: Record<string, string> = {
  asset: '资产',
  storyboard: '分镜',
  video: '视频',
  keyframe: '尾帧',
  audio: '配音',
  script: '剧本',
  prompt: '提示词预检',
};

/** 7 类环节（与后端 kind 枚举单一事实源一致，固定顺序渲染 chips） */
const LESSON_KINDS: string[] = ['asset', 'storyboard', 'video', 'keyframe', 'audio', 'script', 'prompt'];

/** 每页条数（配合后端 offset/limit 分页） */
const PAGE_SIZE = 20;

/** 从 lesson 的 context/category/priority 里拼出「命中来源」的可读文案 */
function lessonSourceLabel(l: PromptLesson): string {
  const ctx = l.context && typeof l.context === 'object' ? l.context : {};
  const keys = Object.keys(ctx);
  if (keys.length > 0) {
    return keys.map((k) => `${k}=${String((ctx as Record<string, unknown>)[k])}`).join('，');
  }
  const parts: string[] = [];
  if (l.category) parts.push(`类别:${l.category}`);
  if (l.priority) parts.push(`优先级:${l.priority}`);
  return parts.length > 0 ? parts.join('，') : '—';
}

export function MemoryPage() {
  const { t } = useApp();
  const toast = useToast();
  const [memories, setMemories] = useState<Memory[]>([]);
  const [stats, setStats] = useState<MemoryStats>({
    total: 0, lessons: 0, successes: 0, insights: 0, promptLessons: 0,
  });
  const [loading, setLoading] = useState(true);
  const [insights, setInsights] = useState<string[]>([]);
  const [insightsLoading, setInsightsLoading] = useState(false);

  // ---- 质检教训库（生成链路自动沉淀；与手动登记的 memories 是两套数据）----
  const [projects, setProjects] = useState<Project[]>([]);
  /** 已选环节（多选）；空数组 = 全部 */
  const [lessonKinds, setLessonKinds] = useState<string[]>([]);
  const [lessonProject, setLessonProject] = useState('');
  const [lessonSince, setLessonSince] = useState('');
  const [lessonUntil, setLessonUntil] = useState('');
  /** 检索框输入（回车后才生效） */
  const [lessonSearch, setLessonSearch] = useState('');
  /** 已生效的关键词（触发重查） */
  const [lessonQ, setLessonQ] = useState('');
  const [lessons, setLessons] = useState<PromptLesson[]>([]);
  const [lessonPage, setLessonPage] = useState<LessonPage>({
    total: 0, filtered: 0, by_kind: {}, dead_lessons: 0, lessons: [],
  });
  const [lessonOffset, setLessonOffset] = useState(0);
  const [lessonLoading, setLessonLoading] = useState(false);
  const [lessonStats, setLessonStats] = useState({
    total: 0, by_kind: {} as Record<string, number>, dead_lessons: 0, used_total: 0,
  });
  /** 手动触发一次「重新拉取当前页」（删除/清空后） */
  const [reloadTick, setReloadTick] = useState(0);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  // 删除单条 / 清空本环节 / 清理 30 天前
  const [deleteTarget, setDeleteTarget] = useState<PromptLesson | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [clearKindOpen, setClearKindOpen] = useState(false);
  const [clearingKind, setClearingKind] = useState(false);
  const [clearOldOpen, setClearOldOpen] = useState(false);
  const [clearingOld, setClearingOld] = useState(false);

  const loadLessonStats = () => {
    memoryApi.lessonStats().then(setLessonStats).catch(() => {});
  };

  useEffect(() => {
    Promise.all([
      // memoryApi.list() 已统一拆封为 Memory[]，这里不要再读 .memories，
      // 否则数组被当成信封对象解析，列表恒为空。
      memoryApi.list().then(list => setMemories(list)),
      memoryApi.stats().then(d => setStats(d)),
    ]).finally(() => setLoading(false));
    setInsightsLoading(true);
    memoryApi.insights()
      .then(d => {
        const list = Array.isArray(d?.insights) ? d.insights : [];
        setInsights(list.map((x: unknown) => typeof x === 'string' ? x : JSON.stringify(x)));
      })
      .catch(() => {})
      .finally(() => setInsightsLoading(false));

    // 项目下拉（失败静默降级为空列表，不影响教训列表）
    projectsApi.list()
      .then(d => setProjects(d?.projects || []))
      .catch(() => setProjects([]));

    loadLessonStats();
  }, []);

  // 教训库单独拉取：任一筛选条件变化 → 回到第一页全量刷新
  useEffect(() => {
    let cancelled = false;
    setLessonLoading(true);
    memoryApi.lessons({
      kind: lessonKinds.join(',') || undefined,
      project: lessonProject || undefined,
      since: lessonSince || undefined,
      until: lessonUntil || undefined,
      q: lessonQ || undefined,
      limit: PAGE_SIZE,
      offset: 0,
    })
      .then(page => {
        if (cancelled) return;
        setLessonPage(page);
        setLessons(page.lessons);
        setLessonOffset(page.lessons.length);
      })
      .catch(() => {
        if (cancelled) return;
        setLessonPage({ total: 0, filtered: 0, by_kind: {}, dead_lessons: 0, lessons: [] });
        setLessons([]);
        setLessonOffset(0);
      })
      .finally(() => {
        if (!cancelled) setLessonLoading(false);
      });
    return () => { cancelled = true; };
  }, [lessonKinds, lessonProject, lessonSince, lessonUntil, lessonQ, reloadTick]);

  const toggleKind = (k: string) => {
    setLessonKinds(prev => (prev.includes(k) ? prev.filter(x => x !== k) : [...prev, k]));
  };

  const toggleExpand = (key: string) => {
    setExpanded(prev => ({ ...prev, [key]: !prev[key] }));
  };

  const handleLoadMore = async () => {
    setLessonLoading(true);
    try {
      const page = await memoryApi.lessons({
        kind: lessonKinds.join(',') || undefined,
        project: lessonProject || undefined,
        since: lessonSince || undefined,
        until: lessonUntil || undefined,
        q: lessonQ || undefined,
        limit: PAGE_SIZE,
        offset: lessonOffset,
      });
      setLessons(prev => [...prev, ...page.lessons]);
      setLessonOffset(prev => prev + page.lessons.length);
    } catch (e) {
      toast.error(`加载更多失败：${e instanceof Error ? e.message : '未知错误'}`);
    } finally {
      setLessonLoading(false);
    }
  };

  const handleDeleteLesson = async () => {
    if (!deleteTarget?.lesson_id) return;
    setDeleting(true);
    try {
      await memoryApi.deleteLesson(deleteTarget.lesson_id);
      toast.success('已删除该教训');
      setDeleteTarget(null);
      setReloadTick(x => x + 1);
      loadLessonStats();
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : '未知错误'}`);
    } finally {
      setDeleting(false);
    }
  };

  const handleClearLessons = async () => {
    if (lessonKinds.length !== 1) return;
    const kind = lessonKinds[0];
    setClearingKind(true);
    try {
      await memoryApi.clearLessons(kind);
      toast.success(`已清空「${LESSON_KIND_LABEL[kind] || kind}」环节教训`);
      setClearKindOpen(false);
      setReloadTick(x => x + 1);
      loadLessonStats();
    } catch (e) {
      toast.error(`清空失败：${e instanceof Error ? e.message : '未知错误'}`);
    } finally {
      setClearingKind(false);
    }
  };

  const handleClearOld = async () => {
    setClearingOld(true);
    try {
      await memoryApi.clearOld(30);
      // memoryApi.list() 已在这一层拆封成数组，可直接 setState。
      // 之前这里把整个响应对象 setMemories(d)，随后 memories.map 直接抛 TypeError。
      const [list, s] = await Promise.all([memoryApi.list(), memoryApi.stats()]);
      setMemories(list);
      setStats(s);
      toast.success('已清理 30 天前的记忆条目');
    } catch (e) {
      console.error('Failed to clear', e);
      toast.error(`清理失败：${e instanceof Error ? e.message : '未知错误'}`);
    } finally {
      setClearingOld(false);
      setClearOldOpen(false);
    }
  };

  /** 记忆类型 → 展示文案。后端类型集合比 i18n 现有键更宽，
   *  未知类型回退为原文，避免界面出现 `memory.type.xxx` 这种原始键名。 */
  const memTypeOf = (mem: Memory): MemoryType => (mem.mem_type || mem.type || 'insight');
  const memTypeLabel = (mem: Memory): string => {
    const key = `memory.type.${memTypeOf(mem)}`;
    const label = t(key);
    return label === key ? memTypeOf(mem) : label;
  };

  if (loading) return <Loading />;

  return (
    <div className="space-y-6 fade-in">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold text-ink-1">{t('memory.title')}</h2>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => window.location.reload()}>
            {t('common.refresh')}
          </Button>
          <Button variant="danger" onClick={() => setClearOldOpen(true)}>
            {t('memory.clearOld')}
          </Button>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4">
        <StatCard label={t('memory.total')} value={stats.total} color="blue" />
        <StatCard label={t('memory.lessons')} value={stats.lessons} color="yellow" />
        <StatCard label={t('memory.successes')} value={stats.successes} color="green" />
        <StatCard label="质检教训库" value={stats.promptLessons ?? 0} color="purple" />
        <StatCard label="死教训数" value={lessonStats.dead_lessons} color="gray" />
      </div>

      {/* 质检教训库：生成链路自动沉淀，驱动「不达标 → 改提示词重生成」 */}
      <Card title="质检教训库（自动学习）">
        {/* 环节 chips 多选（7 类） */}
        {/* 保留原生：胶囊筛选 chip（rounded-full），Button 的 rounded-md 会改掉形状 */}
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <button
            onClick={() => setLessonKinds([])}
            className={`px-3 py-1 rounded-full text-xs border transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ${
              lessonKinds.length === 0
                ? 'bg-brand text-white border-brand'
                : 'bg-surface-2 text-ink-2 border-line hover:bg-line'
            }`}
          >
            全部
          </button>
          {LESSON_KINDS.map((k) => {
            const active = lessonKinds.includes(k);
            return (
              <button
                key={k}
                onClick={() => toggleKind(k)}
                className={`px-3 py-1 rounded-full text-xs border transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ${
                  active
                    ? 'bg-brand text-white border-brand'
                    : 'bg-surface-2 text-ink-2 border-line hover:bg-line'
                }`}
              >
                {LESSON_KIND_LABEL[k]}
              </button>
            );
          })}
        </div>

        {/* 项目 / 时间区间 / 关键词检索 */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mb-3">
          <Select
            value={lessonProject}
            onChange={setLessonProject}
            options={[
              { value: '', label: '全部项目' },
              // ⚠️ 按 name 去重：Select 内部用 value 当 React key，同名项目会产生重复 key
              ...Array.from(new Map(projects.map((p) => [p.name, p])).values())
                .map((p) => ({ value: p.name, label: p.name })),
            ]}
          />
          <Input
            type="date"
            value={lessonSince}
            onChange={setLessonSince}
            placeholder="起始日期"
          />
          <Input
            type="date"
            value={lessonUntil}
            onChange={setLessonUntil}
            placeholder="结束日期"
          />
          <Input
            value={lessonSearch}
            onChange={setLessonSearch}
            onEnter={() => setLessonQ(lessonSearch.trim())}
            placeholder="关键词检索（回车触发）"
          />
        </div>

        {/* 汇总 + 清空本环节 */}
        <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
          <div className="text-xs text-ink-2">
            当前命中 {lessonPage.filtered} 条
            {lessonPage.filtered !== lessonPage.total ? `（全库 ${lessonPage.total} 条）` : ''}
            {' · '}死教训 {lessonPage.dead_lessons} 条
            {' · '}累计被召回 {lessonStats.used_total} 次
          </div>
          <Button
            variant="danger"
            size="sm"
            disabled={lessonKinds.length !== 1}
            onClick={() => setClearKindOpen(true)}
            title={lessonKinds.length === 1
              ? `清空「${LESSON_KIND_LABEL[lessonKinds[0]]}」环节`
              : '请先只选择一个环节'}
          >
            清空本环节
          </Button>
        </div>

        {lessons.length === 0 && !lessonLoading ? (
          <EmptyState
            icon="📚"
            title="暂无质检教训"
            description="质检不达标时系统会自动沉淀教训，并在重试前召回改写提示词。可在 AI 设置中开启图片质检以让资产/分镜也产生教训。"
          />
        ) : (
          <div className="space-y-3">
            {lessons.map((l, i) => {
              const key = l.lesson_id || `${l.phash || ''}-${i}`;
              const isExpanded = !!expanded[key];
              const useCount = l.use_count ?? 0;
              const issues = l.issues || [];
              const terms = l.terms || [];
              return (
                <div key={key} className="p-4 bg-surface-2 border border-line rounded-lg">
                  <div className="flex items-start justify-between gap-2 mb-2">
                    <div className="flex items-center gap-2 flex-wrap">
                      <Badge variant="warning">{LESSON_KIND_LABEL[l.kind || ''] || '未知'}</Badge>
                      {l.project ? (
                        <span className="text-xs text-ink-2">{l.project}</span>
                      ) : null}
                      {/* 召回次数徽标：0 次灰色弱化（死教训） */}
                      <span className={`text-xs px-2 py-0.5 rounded-full ${
                        useCount === 0
                          ? 'bg-line text-ink-2'
                          : 'bg-brand-subtle text-brand-hover'
                      }`}>
                        被召回 {useCount} 次
                      </span>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs text-ink-3 whitespace-nowrap">
                        {l.ts ? new Date(l.ts).toLocaleString() : ''}
                      </span>
                      <Button
                        variant="link"
                        className="text-xs whitespace-nowrap"
                        onClick={() => toggleExpand(key)}
                      >
                        {isExpanded ? '收起' : '详情'}
                      </Button>
                      {/* text-danger 覆盖 link 变体的 text-brand：
                          Tailwind 按 theme.colors 键序产出，danger 在 brand 之后，同属性后者胜出 */}
                      <Button
                        variant="link"
                        className="text-xs whitespace-nowrap text-danger hover:text-danger-strong"
                        onClick={() => setDeleteTarget(l)}
                      >
                        删除
                      </Button>
                    </div>
                  </div>

                  <ul className="list-disc list-inside space-y-1">
                    {issues.slice(0, 5).map((iss, j) => (
                      <li key={j} className="text-sm text-ink-1">{iss}</li>
                    ))}
                    {issues.length === 0 && l.reason ? (
                      <li className="text-sm text-ink-1">{l.reason}</li>
                    ) : null}
                  </ul>

                  {isExpanded && (
                    <div className="mt-3 pt-3 border-t border-line space-y-3">
                      <div>
                        <div className="text-xs font-medium text-ink-2 mb-1">提示词原文</div>
                        <pre className="text-xs text-ink-1 bg-surface border border-line rounded p-2 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
                          {l.prompt || '—'}
                        </pre>
                      </div>
                      {issues.length > 0 && (
                        <div>
                          <div className="text-xs font-medium text-ink-2 mb-1">问题清单</div>
                          <ul className="list-disc list-inside space-y-1">
                            {issues.map((iss, j) => (
                              <li key={j} className="text-sm text-ink-1">{iss}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {terms.length > 0 && (
                        <div>
                          <div className="text-xs font-medium text-ink-2 mb-1">关键词</div>
                          <div className="flex flex-wrap gap-2">
                            {terms.map((term) => (
                              <span key={term} className="text-xs px-2 py-0.5 bg-line rounded text-ink-2">
                                {term}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
                        <div>
                          <span className="text-ink-2">得分：</span>
                          <span className="text-ink-1">{typeof l.score === 'number' ? l.score : '—'}</span>
                        </div>
                        <div>
                          <span className="text-ink-2">命中来源：</span>
                          <span className="text-ink-1">{lessonSourceLabel(l)}</span>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}

            {lessonLoading && <Loading size="sm" label="加载中…" />}
            {lessons.length < lessonPage.filtered && (
              <div className="flex justify-center pt-2">
                <Button variant="secondary" size="sm" onClick={handleLoadMore} loading={lessonLoading}>
                  加载更多（已显示 {lessons.length}/{lessonPage.filtered}）
                </Button>
              </div>
            )}
          </div>
        )}
      </Card>

      {/* Insights */}
      {!insightsLoading && insights.length > 0 && (
        <Card title={t('memory.insightTitle')}>
          <div className="space-y-2">
            {insights.slice(0, 5).map((ins, i) => (
              <p key={i} className="text-sm text-ink-2">
                {ins}
              </p>
            ))}
          </div>
        </Card>
      )}

      {/* Memories list */}
      <Card title={t('memory.listTitle')}>
        {memories.length === 0 ? (
          <EmptyState icon="🧠" title={t('memory.noMemories')} description={t('memory.noMemoriesDesc')} />
        ) : (
          <div className="space-y-3">
            {memories.slice(0, 20).map((mem) => (
              <div key={mem.mem_id || mem.id} className="p-4 bg-surface-2 rounded-lg">
                <div className="flex items-start justify-between mb-2">
                  <Badge variant={memTypeOf(mem) === 'lesson' ? 'warning' : memTypeOf(mem) === 'success' ? 'success' : 'info'}>
                    {memTypeLabel(mem)}
                  </Badge>
                  <span className="text-xs text-ink-3">{new Date(mem.created_at).toLocaleDateString()}</span>
                </div>
                <p className="text-ink-1 text-sm">{mem.content}</p>
                {(mem.tags || []).length > 0 && (
                  <div className="flex gap-2 mt-2">
                    {(mem.tags || []).map((tag) => (
                      <span key={tag} className="text-xs px-2 py-0.5 bg-line rounded text-ink-2">
                        #{tag}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* 清理 30 天前记忆 */}
      <ConfirmDialog
        isOpen={clearOldOpen}
        onClose={() => (clearingOld ? undefined : setClearOldOpen(false))}
        onConfirm={handleClearOld}
        title={t('memory.clearOld')}
        danger
        loading={clearingOld}
        message={`将删除 30 天前的记忆条目。此操作不可撤销。`}
      />

      {/* 删除单条教训 */}
      <ConfirmDialog
        isOpen={!!deleteTarget}
        onClose={() => (deleting ? undefined : setDeleteTarget(null))}
        onConfirm={handleDeleteLesson}
        title="删除教训"
        danger
        loading={deleting}
        message={deleteTarget
          ? `确定删除这条「${LESSON_KIND_LABEL[deleteTarget.kind || ''] || '未知'}」教训吗？此操作不可撤销。`
          : ''}
      />

      {/* 清空本环节 */}
      <ConfirmDialog
        isOpen={clearKindOpen}
        onClose={() => (clearingKind ? undefined : setClearKindOpen(false))}
        onConfirm={handleClearLessons}
        title="清空本环节"
        danger
        loading={clearingKind}
        message={lessonKinds.length === 1
          ? `将删除「${LESSON_KIND_LABEL[lessonKinds[0]]}」环节的全部 ${lessonPage.by_kind[lessonKinds[0]] ?? 0} 条教训。此操作不可撤销。`
          : ''}
      />
    </div>
  );
}

function StatCard({ label, value, color }: { label: string; value: number; color: string }) {
  const colorMap: Record<string, string> = {
    blue: 'bg-info-subtle text-brand',
    yellow: 'bg-warning-subtle text-warning-strong',
    green: 'bg-success-subtle text-success-strong',
    purple: 'bg-brand-subtle text-brand',
    gray: 'bg-surface-2 text-ink-2',
  };
  return (
    <Card>
      <div className="flex items-center gap-4">
        <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${colorMap[color] || colorMap.blue}`}>
          <span className="text-xl font-bold">{value}</span>
        </div>
        <span className="text-ink-2">{label}</span>
      </div>
    </Card>
  );
}
