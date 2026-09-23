import React, { useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { storyboardApi, projectsApi } from '@/api/client';
import { Button } from '@/components/ui';
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
        <h3 className="text-lg font-semibold text-gray-900">{t('nineGrid.title')}</h3>
        <p className="text-sm text-gray-500 mt-1">{t('nineGrid.desc')}</p>
      </div>

      {/* Controls */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 space-y-4">
        {!projectKey && (
          <div className="flex items-center gap-4 flex-wrap">
            <label className="text-sm font-medium text-gray-700 whitespace-nowrap">
              {t('export.selectProject')}
            </label>
            <select
              value={project}
              onChange={(e) => setProject(e.target.value)}
              className="flex-1 min-w-[200px] px-3 py-2 border border-gray-300 rounded-lg"
            >
              {projectList.map((p) => (
                <option key={p.dir_key} value={p.dir_key}>
                  {p.name} ({p.dir_key})
                </option>
              ))}
            </select>
          </div>
        )}

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            {t('nineGrid.sceneDescription')}
          </label>
          <textarea
            value={sceneDesc}
            onChange={(e) => setSceneDesc(e.target.value)}
            placeholder={t('nineGrid.scenePlaceholder')}
            rows={3}
            className="w-full px-3 py-2 border border-gray-300 rounded-lg resize-none"
          />
        </div>

        <div className="flex items-center gap-3 flex-wrap">
          <Button onClick={handleGenerate} disabled={generating || !project || !sceneDesc.trim()}>
            {generating ? t('common.generating') : t('nineGrid.generate')}
          </Button>
          {notice && <span className="text-sm text-green-600">{notice}</span>}
          {error && <span className="text-sm text-red-500">{error}</span>}
        </div>
      </div>

      {/* Nine Grid */}
      {nineGrid ? (
        <div className="bg-white rounded-xl border border-gray-200 p-6">
          <div className="flex items-center justify-between mb-4">
            <h4 className="text-sm font-medium text-gray-700">
              {t('nineGrid.resultTitle')} · {shots.length}
            </h4>
            {nineGrid.grid_id && (
              <span className="text-xs text-gray-400 font-mono">{nineGrid.grid_id}</span>
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
                      ? 'border-blue-500 ring-2 ring-blue-300 bg-blue-50/50'
                      : 'border-gray-200 hover:border-blue-300'
                  }`}
                >
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-semibold text-gray-400">#{i + 1}</span>
                    {(isSelected || isPersisted) && (
                      <span className="text-xs text-blue-600 font-medium">
                        {isPersisted ? '✓ 已选定' : '✓'}
                      </span>
                    )}
                  </div>
                  {shot ? (
                    <div className="space-y-1">
                      <p className="text-sm font-medium text-gray-900">
                        {shot.composition || `格 ${i + 1}`}
                      </p>
                      <p className="text-xs text-gray-500">
                        {[shot.camera_angle, shot.zoom_level ? `变焦 ${shot.zoom_level}` : '']
                          .filter(Boolean)
                          .join(' · ')}
                      </p>
                      {shot.description && (
                        <p className="text-xs text-gray-400 line-clamp-3">{shot.description}</p>
                      )}
                    </div>
                  ) : (
                    <p className="text-xs text-gray-400">—</p>
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
          <div className="bg-white rounded-xl border border-dashed border-gray-300 p-8 text-center">
            <div className="text-4xl mb-3">🎨</div>
            <p className="text-gray-500 text-sm">{t('nineGrid.info')}</p>
          </div>
        )
      )}
    </div>
  );
}
