import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { projectsApi, novelsApi } from '@/api/client';
import { Button, Loading, Modal, Badge, ConfirmDialog } from '@/components/ui';
import type { Project, Novel } from '@/types';

type NovelSource = 'upload' | 'existing';

const DEFAULT_CONFIG = {
  style: '3D动漫渲染',
  episodes: 10,
  shots_per_episode: 12,
  resolution: '768p_vertical',
  fps: 24,
  duration_per_shot: 5,
  qc_enabled: true,
  episode_duration_sec: 60,
  target_shots: 12,
  voice_map: {},
};

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
        config: DEFAULT_CONFIG,
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
      setEditError(e instanceof Error ? e.message : '保存失败');
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
      setDeleteError(e instanceof Error ? e.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  };

  if (loading) return <Loading />;

  return (
    <div className="space-y-6 fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-gray-900 dark:text-white">{t('project.title')}</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{t('project.chooseOrUpload')}</p>
        </div>
        <Button onClick={openModal}>
          <span className="mr-2">+</span>
          {t('project.createNew')}
        </Button>
      </div>

      {loadError && (
        <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-500 text-sm">
          {t('project.loadingFailed')}: {loadError}
        </div>
      )}

      {projects.length === 0 ? (
        <div className="text-center py-12">
          <div className="text-4xl mb-4">📁</div>
          <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">{t('project.noProjects')}</h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">{t('project.noProjectsHint')}</p>
          <Button onClick={openModal} className="mt-2">
            {t('project.createNew')}
          </Button>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {projects.map((proj) => (
            <div
              key={proj.id}
              className="group bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-4 hover:shadow-lg transition-shadow"
            >
              <div
                className="cursor-pointer"
                onClick={() => { window.location.hash = `/?p=${encodeURIComponent(proj.dir_key || proj.id)}`; }}
              >
                <div className="aspect-video bg-gradient-to-br from-blue-100 to-purple-100 dark:from-blue-900/20 dark:to-purple-900/20 rounded-lg mb-4 flex items-center justify-center group-hover:scale-105 transition-transform">
                  <span className="text-4xl">🎬</span>
                </div>
                <h3 className="font-semibold text-gray-900 dark:text-white mb-1">{proj.name}</h3>
                <p className="text-sm text-gray-500 dark:text-gray-400 mb-3">
                  风格: {proj.config?.style || '—'}
                </p>
                <div className="flex items-center justify-between text-sm">
                  <Badge variant="info">{proj.episode_count} {t('ep.suffix')}</Badge>
                  <span className="text-gray-400">{new Date(proj.created_at).toLocaleDateString()}</span>
                </div>
              </div>

              {/* 操作按钮 */}
              <div className="flex gap-2 mt-3 pt-3 border-t border-gray-100 dark:border-gray-700">
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setEditingProject(proj);
                    setEditName(proj.name);
                    setEditError('');
                  }}
                  className="flex-1 px-3 py-1.5 text-xs font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
                >
                  ✏️ 编辑
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    openDeleteModal(proj);
                  }}
                  className="px-3 py-1.5 text-xs font-medium rounded-lg border border-red-300 dark:border-red-700 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
                >
                  🗑️ 删除
                </button>
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
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('project.name')}
            </label>
            <input
              type="text"
              value={projectName}
              onChange={(e) => setProjectName(e.target.value)}
              placeholder={t('project.namePlaceholder')}
              className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
          </div>

          {/* 小说来源切换 */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('project.novelSource')}
            </label>
            <div className="inline-flex rounded-lg border border-gray-300 dark:border-gray-600 overflow-hidden">
              {([
                { id: 'upload' as NovelSource, label: t('project.sourceUpload') },
                { id: 'existing' as NovelSource, label: t('project.sourceExisting') },
              ]).map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => { setSource(opt.id); setFormError(''); }}
                  className={`px-4 py-2 text-sm font-medium transition-colors ${
                    source === opt.id
                      ? 'bg-blue-600 text-white'
                      : 'bg-transparent text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
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
                    ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/10'
                    : 'border-gray-300 dark:border-gray-600 hover:border-blue-400'
                }`}
              >
                <div className="text-3xl mb-2">📄</div>
                <p className="text-sm font-medium text-gray-900 dark:text-white">
                  {pendingFile ? pendingFile.name : t('upload.uploadText')}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
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
                <p className="text-sm text-gray-500 dark:text-gray-400 py-3">
                  {t('project.noNovelsYet')}
                </p>
              ) : (
                <select
                  value={selectedNovel}
                  onChange={(e) => { setSelectedNovel(e.target.value); setFormError(''); }}
                  className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                >
                  <option value="">{t('project.selectNovelPlaceholder')}</option>
                  {novels.map((n) => (
                    <option key={n.novel_id} value={n.novel_id}>
                      {n.name}（{n.chapter_count} 章）
                    </option>
                  ))}
                </select>
              )}
            </div>
          )}

          {formError && (
            <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-500 text-sm break-words">
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
        <Modal isOpen={!!editingProject} onClose={closeEditModal} title="编辑项目"
          closeOnBackdrop={false} closeOnEsc={false} preventClose={savingEdit}>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                项目名称
              </label>
              <input
                type="text"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              />
            </div>
            {editError && (
              <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-500 text-sm">
                {editError}
              </div>
            )}
            <div className="flex gap-3 pt-2">
              <Button variant="secondary" onClick={closeEditModal} disabled={savingEdit}>
                取消
              </Button>
              <Button onClick={handleSaveEdit} disabled={savingEdit}>
                {savingEdit ? '保存中...' : '保存'}
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
        title="删除项目"
        danger
        loading={deleting}
        confirmText="确认删除"
        message={
          <>
            <p className="text-gray-700 dark:text-gray-300">
              确定要删除项目《<span className="font-semibold">{deletingProject?.name}</span>》吗？
            </p>
            <p className="mt-2 text-sm text-red-500">
              ⚠️ 此操作会将项目及其所有产物移入回收站，可从磁盘还原。
            </p>
            {deleteError && (
              <p className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-500">
                {deleteError}
              </p>
            )}
          </>
        }
      />
    </div>
  );
}
