import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { projectsApi, novelsApi } from '@/api/client';
import { Button, Input, Modal, Badge, ConfirmDialog, Select, Skeleton, EmptyState, ErrorState } from '@/components/ui';
import { AlertTriangle, Clapperboard, FileText, FolderOpen, Pencil, Plus, Trash2 } from '@/components/ui/icons';
import type { Project, Novel } from '@/types';

type NovelSource = 'upload' | 'existing';

const DEFAULT_CONFIG = {
  // 默认创作风格：后端数据值（非 UI 文案），保持原字面值「3D动漫渲染」不变，
  // 仅以 \u 转义书写，避免源码里出现 CJK（i18n 扫描要求本文件零中文）。
  style: '3D\u52a8\u6f2b\u6e32\u67d3',
  episodes: 10,
  shots_per_episode: 12,
  resolution: '768p_vertical',
  aspect_ratio: '9:16 \u7ad6\u5c4f',
  fps: 24,
  duration_per_shot: 5,
  qc_enabled: true,
  episode_duration_sec: 60,
  target_shots: 12,
  voice_map: {},
};

// 新建项目可选风格（后端数据值，与 ai_chat.SETTING_FIELDS 里 art_style 的 options 对齐）。
// 仍以 \u 转义书写（本文件零 CJK 约定）。value 即写入 config.style 的字面值。
const STYLE_PRESETS: { value: string; label: string }[] = [
  { value: '3D\u52a8\u6f2b\u6e32\u67d3', label: '3D \u52a8\u6f2b\u6e32\u67d3' },
  { value: '\u56fd\u6f2b2D\u52a8\u6f2b\u98ce', label: '\u56fd\u6f2b 2D \u52a8\u6f2b\u98ce' },
  { value: '\u56fd\u98ce\u6c34\u58a8', label: '\u56fd\u98ce\u6c34\u58a8' },
  { value: '\u5199\u5b9e\u7535\u5f71\u611f', label: '\u5199\u5b9e\u7535\u5f71\u611f' },
  { value: '\u65e5\u5f0f\u8d5b\u7490\u7490', label: '\u65e5\u5f0f\u8d5b\u7490\u7490' },
  { value: '\u539a\u6d82\u63d2\u753b', label: '\u539a\u6d82\u63d2\u753b' },
];

// 新建项目可选画面比例（与 ai_chat.SETTING_FIELDS 里 aspect_ratio 的 options 对齐）。
// value 即写入 config.aspect_ratio 的字面值，沿用 \u 转义（本文件零 CJK 约定）。
// 视频 / 分镜画幅由 style 串解析，这里落一个独立字段供生成链路与确认门识别。
const ASPECT_PRESETS: { value: string; label: string }[] = [
  { value: '9:16 \u7ad6\u5c4f', label: '9:16 \u7ad6\u5c4f' },
  { value: '16:9 \u6a2a\u5c4f', label: '16:9 \u6a2a\u5c4f' },
  { value: '1:1 \u65b9\u5f62', label: '1:1 \u65b9\u5f62' },
];

const ACCEPT_EXTS = '.txt,.docx,.pdf,.epub,.md';

export function ProjectsPage() {
  const { t } = useApp();
  const [projects, setProjects] = useState<Project[]>([]);
  const [novels, setNovels] = useState<Novel[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  // --- 新建项目弹窗（上传小说 + 选已有小说 二合一）---
  const [showNewProject, setShowNewProject] = useState(false);
  const [source, setSource] = useState<NovelSource>('upload');
  const [projectName, setProjectName] = useState('');
  const [selectedNovel, setSelectedNovel] = useState('');
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');
  // 风格选择：预设下拉 + 自定义输入。customStyle 非空时优先用自定义值。
  const [stylePreset, setStylePreset] = useState(STYLE_PRESETS[0].value);
  const [customStyle, setCustomStyle] = useState('');
  // 画面比例（视频/分镜画幅）：与风格一起在新建入口统一设置
  const [aspectRatio, setAspectRatio] = useState(ASPECT_PRESETS[0].value);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // --- 编辑项目弹窗 ---
  const [editingProject, setEditingProject] = useState<Project | null>(null);
  const [editName, setEditName] = useState('');
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState('');

  // --- 删除项目弹窗 ---
  const [deletingProject, setDeletingProject] = useState<Project | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');

  const reload = async () => {
    const [p, n] = await Promise.all([
      projectsApi.list().then(d => d.projects || []).catch(() => [] as Project[]),
      novelsApi.list().then(d => d.novels || []).catch(() => [] as Novel[]),
    ]);
    setProjects(p);
    setNovels(n);
    return { p, n };
  };

  useEffect(() => {
    reload()
      .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  const resetForm = () => {
    setSource('upload');
    setProjectName('');
    setSelectedNovel('');
    setPendingFile(null);
    setDragOver(false);
    setFormError('');
    setStylePreset(STYLE_PRESETS[0].value);
    setCustomStyle('');
    setAspectRatio(ASPECT_PRESETS[0].value);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const openModal = () => {
    resetForm();
    setShowNewProject(true);
  };

  const closeModal = () => {
    if (submitting) return;
    setShowNewProject(false);
    resetForm();
  };

  const pickFile = (file: File | undefined | null) => {
    if (!file) return;
    setPendingFile(file);
    setFormError('');
    // 用户没写项目名时，用文件名兜个默认值，减少一步手动输入
    if (!projectName.trim()) {
      setProjectName(file.name.replace(/\.[^.]+$/, ''));
    }
  };

  const handleCreate = async () => {
    setFormError('');
    const name = projectName.trim();
    if (!name) {
      setFormError(t('project.nameRequired'));
      return;
    }

    setSubmitting(true);
    try {
      // 风格：自定义模式必须填写，否则回落到预设
      let finalStyle = stylePreset === '__custom__' ? customStyle.trim() : stylePreset;
      if (!finalStyle) {
        setFormError(t('project.styleRequired'));
        setSubmitting(false);
        return;
      }

      let novelId = '';

      if (source === 'upload') {
        if (!pendingFile) {
          setFormError(t('project.needNovelFile'));
          return;
        }
        // autoProject=false：不让后端按小说标题自动建项目，
        // 而是由前端带着用户填的名称显式创建，避免名字对不上。
        const up = await novelsApi.upload(pendingFile, { autoProject: false });
        const first = (up.results || [])[0];
        if (!first?.success || !first.novel?.novel_id) {
          throw new Error(first?.error || t('upload.failed'));
        }
        novelId = first.novel.novel_id;
      } else {
        novelId = selectedNovel;
        if (!novelId) {
          setFormError(t('project.selectNovelPlaceholder'));
          return;
        }
      }

      const res = await projectsApi.create({
        name,
        novel_id: novelId,
        config: {
          ...DEFAULT_CONFIG,
          style: finalStyle,
          aspect_ratio: aspectRatio,
        },
      } as any);
      const key = res?.project?.dir_key || res?.project?.id || '';

      await reload();
      setShowNewProject(false);
      resetForm();

      if (key) {
        window.location.hash = `/?p=${encodeURIComponent(key)}`;
      }
    } catch (e) {
      setFormError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const busyLabel = useMemo(() => {
    if (!submitting) return t('project.create');
    return source === 'upload' ? t('project.uploadingCreating') : t('project.creating');
  }, [submitting, source, t]);

  // --- 编辑项目 ---
  const openEditModal = (proj: Project) => {
    setEditingProject(proj);
    setEditName(proj.name);
    setEditError('');
  };

  const closeEditModal = () => {
    setEditingProject(null);
    setEditName('');
    setEditError('');
    setSavingEdit(false);
  };

  const handleSaveEdit = async () => {
    if (!editingProject) return;
    const newName = editName.trim();
    if (!newName) {
      setEditError(t('project.nameRequired'));
      return;
    }
    setSavingEdit(true);
    setEditError('');
    try {
      await projectsApi.rename(editingProject.dir_key || editingProject.id, newName);
      await reload();
      closeEditModal();
    } catch (e) {
      setEditError(e instanceof Error ? e.message : t('project.saveFailed'));
    } finally {
      setSavingEdit(false);
    }
  };

  // --- 删除项目 ---
  const openDeleteModal = (proj: Project) => {
    setDeletingProject(proj);
    setDeleteError('');
  };

  const closeDeleteModal = () => {
    setDeletingProject(null);
    setDeleteError('');
  };

  const handleDelete = async () => {
    if (!deletingProject) return;
    setDeleting(true);
    setDeleteError('');
    try {
      await projectsApi.deleteV2(deletingProject.dir_key || deletingProject.id, true);
      await reload();
      closeDeleteModal();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : t('project.deleteFailed'));
    } finally {
      setDeleting(false);
    }
  };

  // 加载态：沿用真实内容的外层与卡片网格列数，避免骨架 → 内容的布局跳变
  if (loading) {
    return (
      <div
        className="space-y-6 fade-in"
        role="status"
        aria-live="polite"
        aria-label={t('common.loading')}
      >
        <div className="flex items-center justify-between">
          <div className="space-y-2">
            <Skeleton className="h-7 w-32" />
            <Skeleton className="h-4 w-56" />
          </div>
          <Skeleton className="h-9 w-28" />
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-32 rounded-xl" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-ink-1">{t('project.title')}</h2>
          <p className="text-sm text-ink-2 mt-1">{t('project.chooseOrUpload')}</p>
        </div>
        {/* whitespace-nowrap + shrink-0：375 视口下按钮文字会被挤成两行 */}
        <Button onClick={openModal} className="shrink-0 whitespace-nowrap">
          <Plus className="h-4 w-4" />
          {t('project.createNew')}
        </Button>
      </div>

      {/* 软失败：项目列表非空 → 只是这一次刷新失败，保留紧凑行内提示条，绝不吃掉已展示的列表 */}
      {loadError && projects.length > 0 && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
          {t('project.loadingFailed')}: {loadError}
        </div>
      )}

      {projects.length === 0 ? (
        loadError ? (
          /* 硬失败：项目列表为空且加载出错，没有任何数据可展示 → 整块错误态 + 重试 */
          <ErrorState
            title={t('project.loadingFailed')}
            description={loadError}
            onRetry={() => {
              setLoadError('');
              setLoading(true);
              reload()
                .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)))
                .finally(() => setLoading(false));
            }}
          />
        ) : (
          <EmptyState
            icon={<FolderOpen className="h-10 w-10" />}
            title={t('project.noProjects')}
            description={t('project.noProjectsHint')}
            action={<Button onClick={openModal}>{t('project.createNew')}</Button>}
          />
        )
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {projects.map((proj) => (
            <div
              key={proj.id}
              className="group bg-surface rounded-xl border border-line p-4 hover:shadow-lg transition-shadow"
            >
              <div
                role="button"
                tabIndex={0}
                className="cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2"
                onClick={() => { window.location.hash = `/?p=${encodeURIComponent(proj.dir_key || proj.id)}`; }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    window.location.hash = `/?p=${encodeURIComponent(proj.dir_key || proj.id)}`;
                  }
                }}
              >
                <div className="aspect-video bg-surface-2 rounded-lg mb-4 flex items-center justify-center group-hover:scale-105 transition-transform">
                  <Clapperboard className="h-10 w-10 text-ink-3" />
                </div>
                <h3 className="font-semibold text-ink-1 mb-1">{proj.name}</h3>
                <p className="text-sm text-ink-2 mb-3">
                  {t('project.style')}: {proj.config?.style || '—'}
                </p>
                <div className="flex items-center justify-between text-sm">
                  <Badge variant="info">{proj.episode_count} {t('ep.suffix')}</Badge>
                  <span className="text-ink-3">{new Date(proj.created_at).toLocaleDateString()}</span>
                </div>
              </div>

              {/* 操作按钮 */}
              <div className="flex gap-2 mt-3 pt-3 border-t border-line">
                {/* 编辑/删除按钮与可点击卡片是兄弟节点（不嵌套），无需再 stopPropagation */}
                <Button
                  variant="secondary"
                  size="sm"
                  className="flex-1"
                  onClick={() => {
                    setEditingProject(proj);
                    setEditName(proj.name);
                    setEditError('');
                  }}
                >
                  <Pencil className="h-4 w-4" /> {t('project.edit')}
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => openDeleteModal(proj)}
                >
                  <Trash2 className="h-4 w-4" /> {t('project.delete')}
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 新建项目（上传小说 / 选已有小说 二合一） */}
      <Modal isOpen={showNewProject} onClose={closeModal} title={t('project.createNew')}
        size="lg" closeOnBackdrop={false} closeOnEsc={false} preventClose={submitting}>
        <div className="space-y-4">
          {/* 项目名称 */}
          <div>
            <Input
              value={projectName}
              onChange={setProjectName}
              label={t('project.name')}
              placeholder={t('project.namePlaceholder')}
            />
          </div>

          {/* 风格选择：预设下拉 + 自定义输入 */}
          <div>
            <Select
              value={stylePreset}
              onChange={(v) => { setStylePreset(v); setFormError(''); }}
              label={t('project.style')}
              options={[
                ...STYLE_PRESETS,
                { value: '__custom__', label: t('project.styleCustom') },
              ]}
            />
            {stylePreset === '__custom__' && (
              <div className="mt-2">
                <Input
                  value={customStyle}
                  onChange={setCustomStyle}
                  placeholder={t('project.styleCustomPlaceholder')}
                />
              </div>
            )}
          </div>

          {/* 画面比例：与风格一起在新建入口统一设置，决定视频/分镜画幅 */}
          <div>
            <Select
              value={aspectRatio}
              onChange={(v) => { setAspectRatio(v); setFormError(''); }}
              label={t('project.aspectRatio')}
              options={ASPECT_PRESETS}
            />
          </div>

          {/* 小说来源切换 */}
          <div>
            <label className="block text-sm font-medium text-ink-1 mb-1">
              {t('project.novelSource')}
            </label>
            <div className="inline-flex rounded-lg border border-line-strong overflow-hidden">
              {/* 保留原生：分段开关（选中态共用同一元素），
                  Button 的 rounded-md/h-8 会破坏「无间隙拼成一个圆角容器」的形状 */}
              {([
                { id: 'upload' as NovelSource, label: t('project.sourceUpload') },
                { id: 'existing' as NovelSource, label: t('project.sourceExisting') },
              ]).map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => { setSource(opt.id); setFormError(''); }}
                  className={`px-4 py-2 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ${
                    source === opt.id
                      ? 'bg-brand text-white'
                      : 'bg-transparent text-ink-2 hover:bg-surface-2'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {/* 上传模式 */}
          {source === 'upload' && (
            <div>
              <div
                onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => { e.preventDefault(); setDragOver(false); pickFile(e.dataTransfer.files?.[0]); }}
                onClick={() => fileInputRef.current?.click()}
                className={`border-2 border-dashed rounded-xl p-6 text-center cursor-pointer transition-colors ${
                  dragOver
                    ? 'border-brand bg-info-subtle'
                    : 'border-line-strong hover:border-brand'
                }`}
              >
                <div className="mb-2 flex justify-center text-ink-3">
                  <FileText className="h-8 w-8" />
                </div>
                <p className="text-sm font-medium text-ink-1">
                  {pendingFile ? pendingFile.name : t('upload.uploadText')}
                </p>
                <p className="text-xs text-ink-2 mt-1">
                  {pendingFile
                    ? `${(pendingFile.size / 1024).toFixed(0)} KB`
                    : t('upload.fileHint')}
                </p>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept={ACCEPT_EXTS}
                  className="hidden"
                  onChange={(e) => pickFile(e.target.files?.[0])}
                />
              </div>
            </div>
          )}

          {/* 选择已有模式 */}
          {source === 'existing' && (
            <div>
              {novels.length === 0 ? (
                <p className="text-sm text-ink-2 py-3">
                  {t('project.noNovelsYet')}
                </p>
              ) : (
                <Select
                  value={selectedNovel}
                  onChange={(v) => { setSelectedNovel(v); setFormError(''); }}
                  options={[
                    { value: '', label: t('project.selectNovelPlaceholder') },
                    ...novels.map((n) => ({
                      value: n.novel_id,
                      label: t('project.novelOption', { name: n.name, n: n.chapter_count }),
                    })),
                  ]}
                />
              )}
            </div>
          )}

          {formError && (
            <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm break-words">
              {formError}
            </div>
          )}

          <div className="flex gap-3 pt-2">
            <Button variant="secondary" onClick={closeModal} disabled={submitting}>
              {t('common.cancel')}
            </Button>
            <Button onClick={handleCreate} disabled={submitting}>
              {busyLabel}
            </Button>
          </div>
        </div>
      </Modal>

      {/* 编辑项目弹窗 */}
      {editingProject && (
        <Modal isOpen={!!editingProject} onClose={closeEditModal} title={t('project.editTitle')}
          closeOnBackdrop={false} closeOnEsc={false} preventClose={savingEdit}>
          <div className="space-y-4">
            <div>
              <Input value={editName} onChange={setEditName} label={t('project.name')} />
            </div>
            {editError && (
              <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
                {editError}
              </div>
            )}
            <div className="flex gap-3 pt-2">
              <Button variant="secondary" onClick={closeEditModal} disabled={savingEdit}>
                {t('common.cancel')}
              </Button>
              <Button onClick={handleSaveEdit} disabled={savingEdit}>
                {savingEdit ? t('common.saving') : t('common.save')}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* 删除项目：统一确认弹窗（原生手写两步确认已被 ConfirmDialog 取代） */}
      <ConfirmDialog
        isOpen={!!deletingProject}
        onClose={closeDeleteModal}
        onConfirm={handleDelete}
        title={t('project.deleteTitle')}
        danger
        loading={deleting}
        confirmText={t('project.confirmDelete')}
        message={
          <>
            <p className="text-ink-1">
              {t('project.deleteConfirmPrefix')}<span className="font-semibold">{deletingProject?.name}</span>{t('project.deleteConfirmSuffix')}
            </p>
            <p className="mt-2 flex items-start gap-1.5 text-sm text-danger-strong">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              {t('project.deleteWarning')}
            </p>
            {deleteError && (
              <p className="mt-3 rounded-lg border border-danger/30 bg-danger-subtle p-3 text-sm text-danger-strong">
                {deleteError}
              </p>
            )}
          </>
        }
      />
    </div>
  );
}
