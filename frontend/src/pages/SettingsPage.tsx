import React, { useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { settingsApi, providersApi } from '@/api/client';
import { Card, Button, Loading } from '@/components/ui';
import type { Provider } from '@/types';

export function SettingsPage() {
  const { t } = useApp();
  const [settings, setSettings] = useState({
    llm_provider: '',
    llm_api_key: '',
    comfyui_url: '',
    tts_provider: '',
    tts_api_key: '',
    watermark_enabled: false,
    watermark_text: '',
  });
  const [providers, setProviders] = useState<Provider[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      settingsApi.get().then(d => {
        const s = (d as any).settings || {};
        setSettings({
          llm_provider: String(s.llm_provider || ''),
          llm_api_key: String(s.llm_api_key || ''),
          comfyui_url: String(s.comfyui_url || ''),
          tts_provider: String(s.tts_provider || ''),
          tts_api_key: String(s.tts_api_key || ''),
          watermark_enabled: Boolean(s.watermark_enabled),
          watermark_text: String(s.watermark_text || ''),
        });
      }).catch(() => {}),
      providersApi.list().then(d => setProviders(d.providers || [])).catch(() => {}),
    ]).finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      await settingsApi.update(settings);
      alert(t('settings.saved') || '设置已保存');
    } catch (e) {
      alert(t('settings.saveFailed') || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return (
    <div className="flex justify-center py-12">
      <Loading />
    </div>
  );

  if (error) return (
    <div className="p-4 bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 rounded-lg">
      {t('settings.loadFailed') || '设置加载失败，请刷新重试'}
    </div>
  );

  return (
    <div className="space-y-6 fade-in">
      <h2 className="text-2xl font-bold text-gray-900 dark:text-white">{t('settings.title')}</h2>

      {/* LLM Settings */}
      <Card title={t('settings.llm.title')}>
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('settings.llm.provider')}
            </label>
            {providers.length > 0 ? (
              <select
                value={settings.llm_provider}
                onChange={(e) => setSettings(s => ({ ...s, llm_provider: e.target.value }))}
                className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              >
                <option value="">请选择引擎...</option>
                {providers.map(p => (
                  <option key={p.id} value={p.id}>{p.name} ({p.model})</option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={settings.llm_provider}
                onChange={(e) => setSettings(s => ({ ...s, llm_provider: e.target.value }))}
                placeholder="例如: deepseek, qwen"
                className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              />
            )}
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('settings.llm.apiKey')}
            </label>
            <input
              type="password"
              value={settings.llm_api_key}
              onChange={(e) => setSettings(s => ({ ...s, llm_api_key: e.target.value }))}
              placeholder="sk-..."
              className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
          </div>
        </div>
      </Card>

      {/* ComfyUI Settings */}
      <Card title={t('settings.comfyui.title')}>
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t('settings.comfyui.url')}
            </label>
            <input
              type="text"
              value={settings.comfyui_url}
              onChange={(e) => setSettings(s => ({ ...s, comfyui_url: e.target.value }))}
              placeholder="http://127.0.0.1:8188"
              className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
          </div>
        </div>
      </Card>

      {/* Watermark Settings */}
      <Card title={t('settings.watermark.title')}>
        <div className="space-y-4">
          <div className="flex items-center gap-3">
            <input
              type="checkbox"
              checked={settings.watermark_enabled}
              onChange={(e) => setSettings(s => ({ ...s, watermark_enabled: e.target.checked }))}
              className="w-5 h-5 rounded border-gray-300"
            />
            <span className="text-gray-700 dark:text-gray-300">{t('settings.watermark.enabled')}</span>
          </div>
          {settings.watermark_enabled && (
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t('settings.watermark.text')}
              </label>
              <input
                type="text"
                value={settings.watermark_text}
                onChange={(e) => setSettings(s => ({ ...s, watermark_text: e.target.value }))}
                placeholder={t('settings.watermarkPlaceholder') || '输入水印文字'}
                className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              />
            </div>
          )}
        </div>
      </Card>

      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={saving} size="lg">
          {saving ? t('common.loading') : t('common.save')}
        </Button>
      </div>
    </div>
  );
}
