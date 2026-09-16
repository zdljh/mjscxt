import { useState, useEffect, useCallback } from 'react';
import { projectsApi } from '@/api/client';
import type { Project } from '@/types';

/**
 * 统一项目列表 hook，替代各页面中散落的 useApp().projects || []。
 * 用法：const [projects, loadProjects] = useProjects();
 *        useEffect(() => { loadProjects(); }, []);
 */
export function useProjects() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await projectsApi.list();
      setProjects((d as any).projects || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载项目失败');
      console.error('useProjects load error:', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { projects, loading, error, reload: load };
}
