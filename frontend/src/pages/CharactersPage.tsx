import React, { useCallback, useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { charactersApi } from '@/api/client';
import { Card, Button, Loading, EmptyState, Badge } from '@/components/ui';
import type { Character } from '@/types';

/**
 * 从 URL hash（#/?p=<dir_key>）读取当前项目。
 * 读不到就返回空串 —— 不再像以前那样兜底成 'default'：
 * 后端并没有名为 default 的项目，用它去拉角色只会拿到空字典，
 * 把「没进项目」这件事伪装成「项目里没角色」（测试报告 #1 的诱因之一）。
 */
function projectFromHash(): string {
  const m = window.location.hash.match(/[?&]p=([^&]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

/** 角色卡片上的「图片数」：后端无 images 字段，实际是 views（五视图）+ references（参考图） */
function countImages(c: Character): number {
  const viewN = Object.values(c.views ?? {}).filter(Boolean).length;
  const refN = c.references?.length ?? 0;
  return viewN + refN;
}

export function CharactersPage() {
  const { t } = useApp();
  const [characters, setCharacters] = useState<Character[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState('');
  const [newRole, setNewRole] = useState<string>('主角');
  const [submitting, setSubmitting] = useState(false);
  const [projectId, setProjectId] = useState<string>(() => projectFromHash());

  // 跟随 hash 变化（在同一 SPA 内切换项目不刷新页面）
  useEffect(() => {
    const onHash = () => setProjectId(projectFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const load = useCallback(async () => {
    if (!projectId) {
      setCharacters([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      // charactersApi.list 已在 API 层把后端返回的字典解包成数组，异常形态退化为 []，
      // 这里不要（也不需要）再自己 Object.values —— 见测试报告 #1。
      setCharacters(await charactersApi.list(projectId));
    } catch (e) {
      console.error('加载角色列表失败:', e);
      setCharacters([]);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleAdd = async () => {
    const name = newName.trim();
    if (!name || !projectId || submitting) return;
    setSubmitting(true);
    try {
      // 后端 POST /api/characters 必带 project（缺了直接 400），
      // 且返回的是 {success, character_id, name} 而非角色对象 ——
      // 所以添加成功后重新拉一次列表，不要把半成品对象塞进 state。
      await charactersApi.add({ project: projectId, name, role: newRole, description: '' });
      setNewName('');
      setShowAdd(false);
      await load();
    } catch (e) {
      console.error('添加角色失败:', e);
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) return <Loading />;

  if (!projectId) {
    return (
      <div className="space-y-6 fade-in">
        <h2 className="text-2xl font-bold text-gray-900 dark:text-white">{t('character.title')}</h2>
        <EmptyState icon="👤" title={t('character.noChars')} description={t('character.subtitle')} />
      </div>
    );
  }

  return (
    <div className="space-y-6 fade-in">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold text-gray-900 dark:text-white">{t('character.title')}</h2>
        <Button onClick={() => setShowAdd(!showAdd)}>{t('character.add')}</Button>
      </div>

      {showAdd && (
        <Card>
          <div className="space-y-3">
            <input
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder={t('character.namePlaceholder')}
              className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
            />
            <select
              value={newRole}
              onChange={(e) => setNewRole(e.target.value)}
              className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
            >
              <option value="主角">{t('character.roleProtagonist')}</option>
              <option value="配角">{t('character.roleSupporting')}</option>
              <option value="反派">{t('character.roleVillain')}</option>
              <option value="NPC">{t('character.roleNPC')}</option>
            </select>
            <div className="flex gap-2">
              <Button variant="secondary" onClick={() => setShowAdd(false)}>
                {t('common.cancel')}
              </Button>
              <Button onClick={handleAdd} disabled={submitting}>
                {submitting ? t('common.loading') : t('character.add')}
              </Button>
            </div>
          </div>
        </Card>
      )}

      {characters.length === 0 ? (
        <EmptyState icon="👤" title={t('character.noChars')} description="" />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {characters.map((char) => (
            <Card key={char.id}>
              <div className="aspect-square bg-gradient-to-br from-purple-100 to-blue-100 dark:from-purple-900/20 dark:to-blue-900/20 rounded-lg mb-3 flex items-center justify-center">
                <span className="text-4xl">👤</span>
              </div>
              <h3 className="font-medium text-gray-900 dark:text-white">{char.name}</h3>
              <div className="flex items-center justify-between mt-2">
                <Badge variant="info">{char.role || t('character.roleSupporting')}</Badge>
                {/* 原来这里读 char.images.length —— 后端没有 images 字段，
                    必抛 TypeError 白屏（测试报告 #1）。改为按真实字段统计。 */}
                <span className="text-xs text-gray-400">
                  {countImages(char)} {t('common.images')}
                </span>
              </div>
              {char.description && (
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-2 line-clamp-2">{char.description}</p>
              )}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
