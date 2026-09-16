import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { projectsApi, charactersApi, episodesApi, qcApi } from '@/api/client';
import { Card, Loading, Badge, Button } from '@/components/ui';
import type { Project } from '@/types';

interface ProjectDetailProps {
  projectKey?: string | null;
}

interface Character {
  id: string;
  name: string;
  role: string;
  description?: string;
  outfit?: string;
  created_at?: string;
}

export function ProjectDetailPage({ projectKey }: ProjectDetailProps) {
  const { t } = useApp();
  const [project, setProject] = useState<Project | null>(null);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>('');
  const [showAddChar, setShowAddChar] = useState(false);
  const [newCharName, setNewCharName] = useState('');
  const [newCharRole, setNewCharRole] = useState('主角');
  const [newCharDesc, setNewCharDesc] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const key = projectKey || '';

  useEffect(() => {
    if (!key) {
      setError('No project selected');
      setLoading(false);
      return;
    }
    setLoading(true);
    setError('');
    Promise.all([
      projectsApi.get(key).then(d => setProject(d as Project)).catch(() => null),
      charactersApi.list(key).then(d => {
        // charactersApi.list 已归一化为数组（见 api/client.ts 的 toAssetArray）
        setCharacters(d.map((c: any) => ({
          id: c.id,
          name: c.name,
          role: c.role,
          description: c.description,
          outfit: c.outfit,
          created_at: c.created_at,
        })));
      }).catch(() => setCharacters([])),
    ]).finally(() => setLoading(false));
  }, [key]);

  const handleAddCharacter = async () => {
    if (!newCharName.trim()) return;
    setSubmitting(true);
    try {
      await charactersApi.add({
        project,
        name: newCharName,
        role: newCharRole,
        description: newCharDesc,
      } as any);
      setShowAddChar(false);
      setNewCharName('');
      setNewCharRole('主角');
      setNewCharDesc('');
      const d = await charactersApi.list(key);
      setCharacters(d.map((c: any) => ({
        id: c.id,
        name: c.name,
        role: c.role,
        description: c.description,
        outfit: c.outfit,
        created_at: c.created_at,
      })));
    } catch (e) {
      setError((e as Error)?.message || '添加角色失败');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) return <Loading />;
  if (error) return <div className="text-red-500">{error}</div>;
  if (!project) return <div className="text-gray-500">项目未找到：{key}</div>;

  return (
    <div className="space-y-6 fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-gray-900 dark:text-white">{project.name}</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            {t('canvas.style')}: {project.config.style} • {project.episode_count} {t('ep.suffix')}
          </p>
        </div>
        <Button variant="secondary" onClick={() => { window.location.hash = '#/projects'; }}>
          ← {t('common.back') || '返回'}
        </Button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-4">
        <Card>
          <div className="text-center">
            <div className="text-3xl font-bold text-indigo-400">{project.episode_count}</div>
            <div className="text-sm text-gray-500">{t('ep.suffix')}</div>
          </div>
        </Card>
        <Card>
          <div className="text-center">
            <div className="text-3xl font-bold text-green-400">{characters.length}</div>
            <div className="text-sm text-gray-500">{t('character.title')}</div>
          </div>
        </Card>
        <Card>
          <div className="text-center">
            <div className="text-3xl font-bold text-yellow-400">{project.config.shots_per_episode}</div>
            <div className="text-sm text-gray-500">{t('keyframes.totalShots', { defaultValue: '每集镜头数' })}</div>
          </div>
        </Card>
        <Card>
          <div className="text-center">
            <div className="text-3xl font-bold text-blue-400">{project.config.resolution}</div>
            <div className="text-sm text-gray-500">{t('canvas.resolution')}</div>
          </div>
        </Card>
      </div>

      {/* Characters Section */}
      <Card title={`${t('character.title')} (${characters.length})`} action={
        <Button size="sm" onClick={() => setShowAddChar(!showAddChar)}>
          + {t('character.add')}
        </Button>
      }>
        {showAddChar && (
          <div className="mb-4 p-4 bg-gray-50 dark:bg-gray-800 rounded-lg space-y-3">
            <input
              type="text"
              placeholder={t('character.namePlaceholder') || '角色名称'}
              value={newCharName}
              onChange={e => setNewCharName(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded dark:bg-gray-700 dark:text-white"
            />
            <select
              value={newCharRole}
              onChange={e => setNewCharRole(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded dark:bg-gray-700 dark:text-white"
            >
              <option value="主角">{t('character.roleProtagonist')}</option>
              <option value="配角">{t('character.roleSupporting')}</option>
              <option value="反派">{t('character.roleVillain')}</option>
              <option value="NPC">{t('character.roleNPC')}</option>
            </select>
            <textarea
              placeholder={t('character.description') || '角色描述'}
              value={newCharDesc}
              onChange={e => setNewCharDesc(e.target.value)}
              rows={2}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded dark:bg-gray-700 dark:text-white resize-none"
            />
            <div className="flex gap-2">
              <Button size="sm" onClick={handleAddCharacter} disabled={submitting}>
                {submitting ? t('common.loading') : t('character.add')}
              </Button>
              <Button size="sm" variant="secondary" onClick={() => setShowAddChar(false)}>
                {t('common.cancel')}
              </Button>
            </div>
          </div>
        )}
        {characters.length === 0 ? (
          <div className="text-center py-8 text-gray-400">
            {t('character.noChars')}
          </div>
        ) : (
          <div className="space-y-2">
            {characters.map((char) => (
              <div key={char.id} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-800/50 rounded-lg">
                <div>
                  <span className="font-medium text-gray-900 dark:text-white">{char.name}</span>
                  <Badge variant="info">{char.role}</Badge>
                </div>
                {char.description && (
                  <span className="text-sm text-gray-500 max-w-xs truncate">{char.description}</span>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Quick Actions */}
      <div className="grid grid-cols-3 gap-4">
        <Card action={<Button size="sm" onClick={() => { window.location.hash = '#/keyframes'; }}>去查看</Button>}>
          <div className="text-2xl mb-2">🖼️</div>
          <div className="font-medium">{t('keyframes.title')}</div>
          <div className="text-sm text-gray-500">{t('keyframes.desc', { defaultValue: '管理首尾帧图片' })}</div>
        </Card>
        <Card action={<Button size="sm" onClick={() => { window.location.hash = '#/storyboard'; }}>去查看</Button>}>
          <div className="text-2xl mb-2">🎬</div>
          <div className="font-medium">{t('storyboard.canvas')}</div>
          <div className="text-sm text-gray-500">{t('storyboard.desc', { defaultValue: '分镜画布与镜头管理' })}</div>
        </Card>
        <Card action={<Button size="sm" onClick={() => { window.location.hash = '#/qc'; }}>去查看</Button>}>
          <div className="text-2xl mb-2">✅</div>
          <div className="font-medium">{t('qc.title')}</div>
          <div className="text-sm text-gray-500">{t('qc.desc', { defaultValue: '质量检查与重试' })}</div>
        </Card>
      </div>
    </div>
  );
}
