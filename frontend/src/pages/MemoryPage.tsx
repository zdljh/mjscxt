import React, { useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { memoryApi } from '@/api/client';
import { Card, Button, Loading, EmptyState, Badge, ConfirmDialog } from '@/components/ui';
import { useToast } from '@/components/ui/toast';
import type { Memory, MemoryType, MemoryStats, PromptLesson } from '@/types';

/** 教训类型 → 人话（避免界面出现原始 kind 值） */
const LESSON_KIND_LABEL: Record<string, string> = {
  asset: '资产参考图',
  storyboard: '分镜图',
  video: '视频',
  keyframe: '关键帧',
};

export function MemoryPage() {
  const { t } = useApp();
  const [memories, setMemories] = useState<Memory[]>([]);
  const [stats, setStats] = useState<MemoryStats>({
    total: 0, lessons: 0, successes: 0, insights: 0, promptLessons: 0,
  });
  const [loading, setLoading] = useState(true);
  const [insights, setInsights] = useState<string[]>([]);
  const [insightsLoading, setInsightsLoading] = useState(false);
  // 质检教训库：生成链路**自动沉淀**的学习成果（与手动登记的 memories 是两套数据）
  const [lessons, setLessons] = useState<PromptLesson[]>([]);
  const [lessonKind, setLessonKind] = useState('');
  const [clearOpen, setClearOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const toast = useToast();

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
  }, []);

  // 教训库单独拉取（切 kind 时重拉），不与手动记忆耦合
  useEffect(() => {
    memoryApi.lessons({ kind: lessonKind || undefined, limit: 60 })
      .then(setLessons)
      .catch(() => setLessons([]));
  }, [lessonKind]);

  const handleClearOld = async () => {
    setClearing(true);
    try {
      await memoryApi.clearOld(30);
      // memoryApi.list() 已在这一层拆封成数组，可直接 setState。
      // 之前这里把整个响应对象 setMemories(d)，随后 memories.map 直接抛 TypeError。
      const [list, s] = await Promise.all([memoryApi.list(), memoryApi.stats()]);
      setMemories(list);
      setStats(s);
    } catch (e) {
      console.error('Failed to clear', e);
      toast.error(`清理失败：${e instanceof Error ? e.message : '未知错误'}`);
    } finally {
      setClearing(false);
      setClearOpen(false);
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
        <h2 className="text-2xl font-bold text-gray-900 dark:text-white">{t('memory.title')}</h2>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => window.location.reload()}>
            {t('common.refresh')}
          </Button>
          <Button variant="danger" onClick={() => setClearOpen(true)}>
            {t('memory.clearOld')}
          </Button>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label={t('memory.total')} value={stats.total} color="blue" />
        <StatCard label={t('memory.lessons')} value={stats.lessons} color="yellow" />
        <StatCard label={t('memory.successes')} value={stats.successes} color="green" />
        <StatCard label="质检教训库" value={stats.promptLessons ?? 0} color="purple" />
      </div>

      {/* 质检教训库：生成链路自动沉淀，驱动「不达标 → 改提示词重生成」 */}
      <Card title="质检教训库（自动学习）">
        <div className="flex flex-wrap items-center gap-2 mb-3">
          {['', 'asset', 'storyboard', 'video'].map((k) => (
            <button
              key={k || 'all'}
              onClick={() => setLessonKind(k)}
              className={`px-3 py-1 rounded-full text-xs border transition-colors ${
                lessonKind === k
                  ? 'bg-indigo-600 text-white border-indigo-600'
                  : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 border-gray-200 dark:border-gray-700'
              }`}
            >
              {k ? LESSON_KIND_LABEL[k] || k : '全部'}
            </button>
          ))}
        </div>
        {lessons.length === 0 ? (
          <EmptyState
            icon="📚"
            title="暂无质检教训"
            description="质检不达标时系统会自动沉淀教训，并在重试前召回改写提示词。可在 AI 设置中开启图片质检以让资产/分镜也产生教训。"
          />
        ) : (
          <div className="space-y-3">
            {lessons.slice(0, 20).map((l, i) => (
              <div key={`${l.phash || ''}-${i}`} className="p-4 bg-gray-50 dark:bg-gray-800/50 rounded-lg">
                <div className="flex items-start justify-between mb-2 gap-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <Badge variant="warning">{LESSON_KIND_LABEL[l.kind || ''] || l.kind || '未知'}</Badge>
                    {l.project ? (
                      <span className="text-xs text-gray-500 dark:text-gray-400">{l.project}</span>
                    ) : null}
                    {typeof l.score === 'number' ? (
                      <span className="text-xs text-gray-400">得分 {l.score}</span>
                    ) : null}
                  </div>
                  <span className="text-xs text-gray-400 whitespace-nowrap">
                    {l.ts ? new Date(l.ts).toLocaleString() : ''}
                  </span>
                </div>
                <ul className="list-disc list-inside space-y-1">
                  {(l.issues || []).slice(0, 5).map((iss, j) => (
                    <li key={j} className="text-sm text-gray-700 dark:text-gray-300">{iss}</li>
                  ))}
                  {(l.issues || []).length === 0 && l.reason ? (
                    <li className="text-sm text-gray-700 dark:text-gray-300">{l.reason}</li>
                  ) : null}
                </ul>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Insights */}
      {!insightsLoading && insights.length > 0 && (
        <Card title={t('memory.insightTitle')}>
          <div className="space-y-2">
            {insights.slice(0, 5).map((ins, i) => (
              <p key={i} className="text-sm text-gray-600 dark:text-gray-300">
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
              <div key={mem.mem_id || mem.id} className="p-4 bg-gray-50 dark:bg-gray-800/50 rounded-lg">
                <div className="flex items-start justify-between mb-2">
                  <Badge variant={memTypeOf(mem) === 'lesson' ? 'warning' : memTypeOf(mem) === 'success' ? 'success' : 'info'}>
                    {memTypeLabel(mem)}
                  </Badge>
                  <span className="text-xs text-gray-400">{new Date(mem.created_at).toLocaleDateString()}</span>
                </div>
                <p className="text-gray-700 dark:text-gray-300 text-sm">{mem.content}</p>
                {(mem.tags || []).length > 0 && (
                  <div className="flex gap-2 mt-2">
                    {(mem.tags || []).map((tag) => (
                      <span key={tag} className="text-xs px-2 py-0.5 bg-gray-200 dark:bg-gray-700 rounded text-gray-600 dark:text-gray-400">
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

      <ConfirmDialog
        isOpen={clearOpen}
        onClose={() => (clearing ? undefined : setClearOpen(false))}
        onConfirm={handleClearOld}
        title={t('memory.clearOld')}
        danger
        loading={clearing}
        message={`将删除 30 天前的记忆条目。此操作不可撤销。`}
      />
    </div>
  );
}

function StatCard({ label, value, color }: { label: string; value: number; color: string }) {
  const colorMap: Record<string, string> = {
    blue: 'bg-blue-100 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400',
    yellow: 'bg-yellow-100 dark:bg-yellow-900/20 text-yellow-600 dark:text-yellow-400',
    green: 'bg-green-100 dark:bg-green-900/20 text-green-600 dark:text-green-400',
    purple: 'bg-purple-100 dark:bg-purple-900/20 text-purple-600 dark:text-purple-400',
  };
  return (
    <Card>
      <div className="flex items-center gap-4">
        <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${colorMap[color] || colorMap.blue}`}>
          <span className="text-xl font-bold">{value}</span>
        </div>
        <span className="text-gray-600 dark:text-gray-400">{label}</span>
      </div>
    </Card>
  );
}
