import React, { useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { storyboardApi, projectsApi } from '@/api/client';
import { Button, Select, Textarea } from '@/components/ui';
import { Check, Palette } from '@/components/ui/icons';
import type { Project } from '@/types';

// 后端 NineGridStoryboard 返回的是「构图草案」：9 组机位/构图/景别文本，
// 字段为 index / composition / camera_angle / zoom_level / description / prompt_hint。
// 它并不产出图片（image_url 是早期前端一厢情愿的假设），所以这里按文本卡片渲染。
interface NineGridShot {
  index: number;
  composition?: string;
  camera_angle?: string;
  zoom_level?: string | number;
  description?: string;
  prompt_hint?: string;
  emotion?: string;
  selected?: boolean;
}

interface NineGridData {
  grid_id: string;
  project?: string;
  scene_description?: string;
  created_at?: string;
  shots: NineGridShot[];
  selected_index?: number;
}

export function GridPage({ projectKey }: { projectKey?: string } = {}) {
  const { t } = useApp();
  const [project, setProject] = useState(projectKey || '');
  const [projectList, setProjectList] = useState<Project[]>([]);
  const [sceneDesc, setSceneDesc] = useState('');
  const [generating, setGenerating] = useState(false);
  const [nineGrid, setNineGrid] = useState<NineGridData | null>(null);
  const [selectedCell, setSelectedCell] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);

  // 作为工作台标签使用时由外部锁定项目，不再重复请求项目列表
  useEffect(() => {
    if (projectKey) {
      setProject(projectKey);
      return;
    }
    projectsApi.list().then((d) => {
      const list = d.projects || [];
      setProjectList(list);
      if (list.length > 0) {
        setProject((prev) => prev || list[0].dir_key || list[0].name);
      }
    }).catch(() => {});
  }, [projectKey]);

  const handleGenerate = async () => {
    if (!project || !sceneDesc.trim()) {
      setError(t('nineGrid.pleaseSelectProject'));
      return;
    }
    setGenerating(true);
    setError('');
    setNotice('');
    try {
      const data = await storyboardApi.generateNineGrid(project, sceneDesc.trim());
      // 缺陷：后端原先不返回 grid_id，导致「确认选择」必定 404。现已返回，这里如实读取。
      const payload = data as unknown as NineGridData;
      setNineGrid({
        grid_id: payload.grid_id,
        project,
        scene_description: sceneDesc.trim(),
        created_at: payload.created_at,
        shots: payload.shots || [],
      });
      setSelectedCell(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成失败');
    } finally {
      setGenerating(false);
    }
  };

  const handleConfirmSelection = async () => {
    if (selectedCell === null || !nineGrid?.grid_id) return;
    setSaving(true);
    setError('');
    try {
      await storyboardApi.selectNineGridShot(nineGrid.grid_id, project, selectedCell);
      setNineGrid((prev) => (prev ? { ...prev, selected_index: selectedCell } : prev));
      setNotice(
        `${t('nineGrid.confirmSuccess')}（第 ${selectedCell + 1} 格 · ${
          nineGrid.shots[selectedCell]?.composition || ''
        }）`
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : '选择失败');
    } finally {
      setSaving(false);
    }
  };

  const shots = nineGrid?.shots || [];
  const cells: (NineGridShot | null)[] = Array.from({ length: 9 }, (_, i) => shots[i] || null);

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-lg font-semibold text-ink-1">{t('nineGrid.title')}</h3>
        <p className="text-sm text-ink-2 mt-1">{t('nineGrid.desc')}</p>
      </div>

      {/* Controls */}
      <div className="bg-surface rounded-xl border border-line p-4 space-y-4">
        {!projectKey && (
          <div className="flex items-center gap-4 flex-wrap">
            <label className="text-sm font-medium text-ink-1 whitespace-nowrap">
              {t('export.selectProject')}
            </label>
            <Select
              value={project}
              onChange={setProject}
              options={projectList.map((p) => ({ value: p.dir_key, label: `${p.name} (${p.dir_key})` }))}
              className="flex-1 min-w-[200px]"
            />
          </div>
        )}

        <Textarea
          value={sceneDesc}
          onChange={setSceneDesc}
          label={t('nineGrid.sceneDescription')}
          placeholder={t('nineGrid.scenePlaceholder')}
          rows={3}
          resize={false}
        />

        <div className="flex items-center gap-3 flex-wrap">
          <Button onClick={handleGenerate} disabled={generating || !project || !sceneDesc.trim()}>
            {generating ? t('common.generating') : t('nineGrid.generate')}
          </Button>
          {notice && <span className="text-sm text-success-strong">{notice}</span>}
          {error && <span className="text-sm text-danger-strong">{error}</span>}
        </div>
      </div>

      {/* Nine Grid */}
      {nineGrid ? (
        <div className="bg-surface rounded-xl border border-line p-6">
          <div className="flex items-center justify-between mb-4">
            <h4 className="text-sm font-medium text-ink-1">
              {t('nineGrid.resultTitle')} · {shots.length}
            </h4>
            {nineGrid.grid_id && (
              <span className="text-xs text-ink-3 font-mono">{nineGrid.grid_id}</span>
            )}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {cells.map((shot, i) => {
              const isSelected = selectedCell === i;
              const isPersisted = nineGrid.selected_index === i;
              return (
                <div
                  key={i}
                  onClick={() => shot && setSelectedCell(i)}
                  className={`relative rounded-lg border-2 p-3 transition-all ${
                    shot ? 'cursor-pointer' : 'opacity-50 cursor-not-allowed'
                  } ${
                    isSelected
                      ? 'border-brand ring-2 ring-brand/40 bg-info-subtle/50'
                      : 'border-line hover:border-brand'
                  }`}
                >
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-semibold text-ink-3">#{i + 1}</span>
                    {(isSelected || isPersisted) && (
                      <span className="inline-flex items-center gap-1 text-xs text-brand font-medium">
                        <Check className="h-3.5 w-3.5" />
                        {isPersisted ? '已选定' : ''}
                      </span>
                    )}
                  </div>
                  {shot ? (
                    <div className="space-y-1">
                      <p className="text-sm font-medium text-ink-1">
                        {shot.composition || `格 ${i + 1}`}
                      </p>
                      <p className="text-xs text-ink-2">
                        {[shot.camera_angle, shot.zoom_level ? `变焦 ${shot.zoom_level}` : '']
                          .filter(Boolean)
                          .join(' · ')}
                      </p>
                      {shot.description && (
                        <p className="text-xs text-ink-3 line-clamp-3">{shot.description}</p>
                      )}
                    </div>
                  ) : (
                    <p className="text-xs text-ink-3">—</p>
                  )}
                </div>
              );
            })}
          </div>

          {selectedCell !== null && selectedCell !== nineGrid.selected_index && (
            <div className="mt-4 flex justify-end">
              <Button size="sm" onClick={handleConfirmSelection} disabled={saving}>
                {saving ? t('common.loading') : t('nineGrid.confirmSelect')}
              </Button>
            </div>
          )}
        </div>
      ) : (
        !generating && (
          <div className="bg-surface rounded-xl border border-dashed border-line-strong p-8 text-center">
            <div className="mb-3 flex justify-center text-ink-3">
              <Palette className="h-9 w-9" />
            </div>
            <p className="text-ink-2 text-sm">{t('nineGrid.info')}</p>
          </div>
        )
      )}
    </div>
  );
}
