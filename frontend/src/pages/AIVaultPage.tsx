import React, { useState, useEffect } from 'react';
import { aiConfigApi, watermarkApi } from '@/api/client';
import { Badge, Button, Card, ConfirmDialog, Input, Loading, Select } from '@/components/ui';
import { AlertTriangle, Brain, CheckCircle2, Network, Search, X } from '@/components/ui/icons';
import type { IconProps } from '@/components/ui/icons';
import type { AIConfigModule, AIConfigResponse, AITestResult } from '@/types';

type ModuleKey = 'text' | 'qc' | 'chat';

interface ModuleState {
  base_url: string;
  model: string;
  api_key: string;
  has_api_key: boolean;
  /** 思考档位：'' = 不注入（服务端默认），其余为 low / high / max */
  reasoning_effort: string;
}

const EMPTY_MODULE: ModuleState = {
  base_url: '', model: '', api_key: '', has_api_key: false, reasoning_effort: '',
};

/** 思考档位下拉的兜底选项（后端会下发 reasoning_effort_options，拿不到时用这份） */
const FALLBACK_REASONING_OPTIONS = ['', 'low', 'high', 'max'];

/** 档位的中文说明（键为后端下发的原始值） */
const REASONING_EFFORT_LABELS: Record<string, string> = {
  '': '默认（不注入该参数，由服务端决定）',
  low: 'low · 思考最少，省 token 最快',
  high: 'high · 思考较充分',
  max: 'max · 思考最充分，最贵最慢',
};

interface SystemSettings {
  comfyui_url: string;
  watermark_enabled: boolean;
  watermark_text: string;
}

/**
* 模块卡片头部的图标底色 / 字色。
* 原先是 `from-*-500 to-*-500` 渐变 —— 那是另一套设计语言，且与「卡片白底 +
* 细描边」的观感冲突；这里统一走语义 token。
*/
const MODULE_CONFIG: Record<ModuleKey, {
  icon: React.ComponentType<IconProps>;
  title: string;
  desc: string;
  placeholderUrl: string;
  placeholderModel: string;
  probe?: string;
  tone: string;
}> = {
  text: {
    icon: Brain,
    title: '文本分析模型（= LLM 引擎）',
    desc: '就是 LLM 引擎：小说转剧本、章节转剧本、提示词分析等纯文本任务都用它',
    placeholderUrl: 'https://api.deepseek.com/v1',
    placeholderModel: 'deepseek-chat',
    tone: 'bg-brand-subtle text-brand',
  },
  qc: {
    icon: Search,
    title: '质检模型',
    desc: '负责分镜图片和视频的视觉质量检查（需支持图像输入）',
    placeholderUrl: 'https://api.openai.com/v1',
    placeholderModel: 'gpt-4o-mini',
    probe: 'vision',
    tone: 'bg-accent-subtle text-accent',
  },
  chat: {
    icon: Network,
    title: '对话总控模型',
    desc: 'AI 创作总控：通过多轮对话确定漫剧风格、题材、画风等设定',
    placeholderUrl: 'https://api.deepseek.com/v1',
    placeholderModel: 'deepseek-chat',
    tone: 'bg-success-subtle text-success-strong',
  },
};

export function AIVaultPage() {
  // AI 模块配置状态
  const [config, setConfig] = useState<Record<ModuleKey, ModuleState>>({
    text: { ...EMPTY_MODULE },
    qc: { ...EMPTY_MODULE },
    chat: { ...EMPTY_MODULE },
  });
  const [reOptions, setReOptions] = useState<string[]>(FALLBACK_REASONING_OPTIONS);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<ModuleKey, AITestResult | null>>({
    text: null, qc: null, chat: null,
  });
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [clearTarget, setClearTarget] = useState<ModuleKey | null>(null);
  const [clearing, setClearing] = useState(false);

  // 系统设置状态（ComfyUI 只读展示 + 水印）
  const [sysSettings, setSysSettings] = useState<SystemSettings>({
    comfyui_url: '',
    watermark_enabled: false,
    watermark_text: '',
  });
  const [sysSaving, setSysSaving] = useState(false);

  useEffect(() => {
    Promise.all([
      // 加载 AI 模块配置
      aiConfigApi.get()
        .then(d => {
          // 后端实际结构：config = { modules: {text,qc,chat}, modules_meta, ... }
          // 早期前端误以为模块直接平铺在 config 上，导致读不到任何值、所有卡片都显示「未配置」。
          const raw = (d.config || {}) as any;
          const c = (raw.modules || raw) as Record<ModuleKey, AIConfigModule>;
          // ComfyUI 地址来自这份响应的 config.comfyui（后端如实标注了来源是环境变量），
          // 不再从 /ai/settings 取——那个接口返回的是「创作设定」，压根没有这个字段。
          if (raw.comfyui?.url) {
            setSysSettings(prev => ({ ...prev, comfyui_url: String(raw.comfyui.url) }));
          }
          if (Array.isArray(raw.reasoning_effort_options) && raw.reasoning_effort_options.length) {
            setReOptions(raw.reasoning_effort_options.map((x: unknown) => String(x)));
          }
          setConfig({
            text: {
              base_url: c.text?.base_url || '',
              model: c.text?.model || '',
              api_key: c.text?.api_key || '',
              has_api_key: c.text?.has_api_key || false,
              reasoning_effort: c.text?.reasoning_effort || '',
            },
            qc: {
              base_url: c.qc?.base_url || '',
              model: c.qc?.model || '',
              api_key: c.qc?.api_key || '',
              has_api_key: c.qc?.has_api_key || false,
              reasoning_effort: c.qc?.reasoning_effort || '',
            },
            chat: {
              base_url: c.chat?.base_url || '',
              model: c.chat?.model || '',
              api_key: c.chat?.api_key || '',
              has_api_key: c.chat?.has_api_key || false,
              reasoning_effort: c.chat?.reasoning_effort || '',
            },
          });
        })
        .catch(err => console.error('Failed to load AI config:', err)),
      // 加载水印设置：走专用的 /watermark/config。
      // 原先读 /ai/settings 的 watermark_enabled，但那个接口返回的是「创作设定」，
      // 永远不含该字段 → 开关恒为关、保存也写不进真实配置。
      watermarkApi.get()
        .then(d => {
          const w = d.config || {};
          setSysSettings(prev => ({
            ...prev,
            watermark_enabled: Boolean(w.enabled),
            watermark_text: String(w.text || ''),
          }));
        })
        .catch(() => {}),
    ]).finally(() => setLoading(false));
  }, []);

  const showMessage = (type: 'success' | 'error', text: string) => {
    setMessage({ type, text });
    setTimeout(() => setMessage(null), 4000);
  };

  // ========== AI 模块操作 ==========
  const handleSave = async (module: ModuleKey) => {
    const state = config[module];
    if (!state.base_url.trim() || !state.model.trim()) {
      showMessage('error', `请填写 ${MODULE_CONFIG[module].title} 的 base_url 和 model`);
      return;
    }
    if (!state.has_api_key && !state.api_key.trim()) {
      showMessage('error', `请填写 ${MODULE_CONFIG[module].title} 的 api_key`);
      return;
    }

    setSaving(module);
    try {
      const result = await aiConfigApi.save(
        module,
        state.base_url.trim(),
        state.model.trim(),
        state.api_key.trim() || undefined,
        state.reasoning_effort
      );
      if (result.success) {
        showMessage('success', result.message || `${MODULE_CONFIG[module].title} 配置已保存`);
        setConfig(prev => ({
          ...prev,
          [module]: {
            ...prev[module],
            has_api_key: result.module_config?.has_api_key ?? prev[module].has_api_key,
            // 回显后端归一化后的档位：非法值会被后端清成 ''，前端要跟着收敛
            reasoning_effort: result.module_config?.reasoning_effort ?? prev[module].reasoning_effort,
          },
        }));
      } else {
        showMessage('error', '保存失败');
      }
    } catch (err) {
      showMessage('error', `保存失败: ${err instanceof Error ? err.message : '未知错误'}`);
    } finally {
      setSaving(null);
    }
  };

  const handleTest = async (module: ModuleKey) => {
    const state = config[module];
    if (!state.base_url.trim() || !state.model.trim()) {
      showMessage('error', `请先填写 ${MODULE_CONFIG[module].title} 的 base_url 和 model`);
      return;
    }

    setTesting(module);
    setTestResult(prev => ({ ...prev, [module]: null }));
    try {
      const probe = MODULE_CONFIG[module].probe || 'text';
      const result = await aiConfigApi.test(
        module,
        state.base_url.trim(),
        state.model.trim(),
        state.api_key.trim() || undefined,
        probe,
        30,
        state.reasoning_effort
      );
      setTestResult(prev => ({ ...prev, [module]: result }));
      if (result.success) {
        const ms = result.latency_ms ?? result.response_time_ms;
        // success 但没拿到正文时不要报「测试成功」——那会让人以为模型已就绪
        const partial = result.verdict !== 'ok';
        showMessage(
          partial ? 'error' : 'success',
          partial
            ? `${MODULE_CONFIG[module].title} 链路可达，但模型未返回正文（多为思考占用额度），请查看提示`
            : `${MODULE_CONFIG[module].title} 连接测试成功${typeof ms === 'number' ? ` (${ms}ms)` : ''}`
        );
      } else {
        showMessage('error', result.error || result.guide || '连接测试失败');
      }
    } catch (err) {
      setTestResult(prev => ({
        ...prev,
        [module]: { success: false, module, probe: 'text', error: err instanceof Error ? err.message : '未知错误' }
      }));
      showMessage('error', `测试失败: ${err instanceof Error ? err.message : '未知错误'}`);
    } finally {
      setTesting(null);
    }
  };

  // 清空配置：改为统一确认弹窗（原生 confirm 阻塞主线程、样式与深色主题脱节，
  // 也无法显示「处理中」状态，误点后没有可撤销的余地）
  const handleClear = (module: ModuleKey) => setClearTarget(module);

  const doClear = async () => {
    const module = clearTarget;
    if (!module) return;
    setClearing(true);
    try {
      const result = await aiConfigApi.clear(module);
      if (result.success) {
        setConfig(prev => ({
          ...prev,
          [module]: { ...EMPTY_MODULE },
        }));
        showMessage('success', result.message);
      }
    } catch (err) {
      showMessage('error', '清空失败');
    } finally {
      setClearing(false);
      setClearTarget(null);
    }
  };

  const updateField = (module: ModuleKey, field: keyof ModuleState, value: string) => {
    setConfig(prev => ({
      ...prev,
      [module]: { ...prev[module], [field]: value },
    }));
  };

  // ========== 水印设置操作 ==========
  // 只保存水印：ComfyUI 地址是只读（来自环境变量），LLM 引擎已并入「文本分析模型」。
  const handleSysSave = async () => {
    setSysSaving(true);
    try {
      await watermarkApi.update({
        enabled: sysSettings.watermark_enabled,
        text: sysSettings.watermark_text,
      });
      showMessage('success', '水印设置已保存');
    } catch (err) {
      showMessage('error', `保存失败: ${err instanceof Error ? err.message : '未知错误'}`);
    } finally {
      setSysSaving(false);
    }
  };

  const updateSysField = (field: keyof SystemSettings, value: string | boolean) => {
    setSysSettings(prev => ({ ...prev, [field]: value }));
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loading label="加载配置中..." />
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-ink-1">AI 配置中心</h2>
          <p className="text-sm text-ink-2 mt-1">
            配置各环节的 AI 模型接口与系统参数
          </p>
        </div>
        {message && (
          <div className={`px-4 py-2 rounded-lg text-sm ${
            message.type === 'success'
              ? 'bg-success-subtle text-success-strong'
              : 'bg-danger-subtle text-danger-strong'
          }`}>
            {message.text}
          </div>
        )}
      </div>

      {/* ===== 系统设置区 ===== */}
      <Card bodyClassName="p-6 space-y-4">
        <h3 className="text-lg font-semibold text-ink-1 flex items-center gap-2">
          系统设置
        </h3>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* ComfyUI 地址：只读展示。
              它由环境变量 COMFYUI_URL 决定，写进配置文件后没有任何代码读取
              （各 ComfyUI 客户端都直接取模块级常量）。此前是个可编辑输入框，
              但保存后不生效——典型的「改了也没用」控件，故改为如实展示。 */}
          <div className="md:col-span-2">
            <Input
              label="ComfyUI 地址"
              value={sysSettings.comfyui_url || '（未获取到）'}
              onChange={() => undefined}
              disabled
              className="font-mono text-sm"
            />
            <p className="text-xs text-ink-2 mt-1">
              由环境变量 <code className="font-mono">COMFYUI_URL</code> 决定，需修改环境变量后重启服务
            </p>
          </div>
        </div>

        {/* 水印设置 */}
        <div className="border-t border-line pt-4 mt-4">
          <div className="flex items-center gap-3 mb-3">
            <input
              type="checkbox"
              checked={sysSettings.watermark_enabled}
              onChange={e => updateSysField('watermark_enabled', e.target.checked)}
              className="w-5 h-5 rounded border-line-strong text-brand focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2"
            />
            <span className="text-ink-2 font-medium">启用视频水印</span>
          </div>
          {sysSettings.watermark_enabled && (
            <div>
              <Input
                label="水印文字"
                value={sysSettings.watermark_text}
                onChange={v => updateSysField('watermark_text', v)}
                placeholder="输入水印文字"
                className="text-sm"
              />
            </div>
          )}
        </div>

        <div className="flex justify-end pt-2">
          <Button
            variant="brand"
            onClick={handleSysSave}
            loading={sysSaving}
          >
            保存水印设置
          </Button>
        </div>
      </Card>

      {/* ===== AI 模块配置区 ===== */}
      <div className="grid grid-cols-1 gap-6">
        {(Object.keys(MODULE_CONFIG) as ModuleKey[]).map(moduleKey => {
          const meta = MODULE_CONFIG[moduleKey];
          const state = config[moduleKey];
          const isConfigured = state.base_url && state.model && state.has_api_key;
          const result = testResult[moduleKey];

          return (
            <Card key={moduleKey} bodyClassName="p-6 space-y-4">
              {/* Card Header */}
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${meta.tone}`}>
                    <meta.icon className="h-5 w-5" />
                  </div>
                  <div>
                    <h3 className="text-lg font-semibold text-ink-1">
                      {meta.title}
                    </h3>
                    <p className="text-xs text-ink-2">{meta.desc}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Badge variant={isConfigured ? 'success' : 'default'}>
                    {isConfigured ? '已配置' : '未配置'}
                  </Badge>
                  <button
                    type="button"
                    onClick={() => handleClear(moduleKey)}
                    className="rounded-md p-2 text-ink-3 transition-colors hover:bg-surface-2 hover:text-danger focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2"
                    title="清空配置"
                    aria-label="清空配置"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              </div>

              {/* Config Fields */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="md:col-span-2">
                  <Input
                    label="Base URL"
                    value={state.base_url}
                    onChange={v => updateField(moduleKey, 'base_url', v)}
                    placeholder={meta.placeholderUrl}
                    className="font-mono text-sm"
                  />
                </div>
                <div>
                  <Input
                    label="Model"
                    value={state.model}
                    onChange={v => updateField(moduleKey, 'model', v)}
                    placeholder={meta.placeholderModel}
                    className="font-mono text-sm"
                  />
                </div>
                <div>
                  <Input
                    type="password"
                    label="API Key"
                    value={state.api_key}
                    onChange={v => updateField(moduleKey, 'api_key', v)}
                    placeholder={state.has_api_key ? '••••••••' : 'sk-...'}
                    className="font-mono text-sm"
                  />
                  {state.has_api_key && (
                    <p className="text-xs text-ink-3 mt-1">密钥已保存，留空则保持原值</p>
                  )}
                </div>
                {/* 思考档位：只对「思考不可关闭」的模型（如 GLM-5.3-Flash）有意义。
                    可关思考的模型（Qwen/vLLM 系）用 enable_thinking=false，不在这里调。 */}
                <div className="md:col-span-2">
                  <Select
                    label="思考档位（可选）"
                    value={state.reasoning_effort}
                    onChange={v => updateField(moduleKey, 'reasoning_effort', v)}
                    options={reOptions.map(opt => ({
                      value: opt,
                      label: REASONING_EFFORT_LABELS[opt] || opt,
                    }))}
                  />
                  <p className="text-xs text-ink-3 mt-1">
                    仅「思考不可关闭」的模型需要设置（如 GLM-5.3-Flash，它没有关闭思考的开关，
                    只能调档）。留空 = 不注入该参数、由服务端取默认档；
                    <span className="text-warning-strong">
                      档位越高越贵（默认档通常是最贵的 max）
                    </span>
                    ，长 JSON 任务建议 low。
                  </p>
                </div>
              </div>

              {/* Action Buttons */}
              <div className="flex items-center gap-3 pt-2">
                <Button
                  variant="secondary"
                  onClick={() => handleTest(moduleKey)}
                  loading={testing === moduleKey}
                  disabled={!state.base_url || !state.model}
                >
                  测试连接
                </Button>
                <Button
                  variant="brand"
                  onClick={() => handleSave(moduleKey)}
                  loading={saving === moduleKey}
                  disabled={!state.base_url || !state.model}
                >
                  保存配置
                </Button>
              </div>

              {/* Test Result */}
              {result && (() => {
                // success=true 但没拿到正文（额度被思考占用）既不是「配置错」也不是「就绪」，
                // 用琥珀色单独区分，避免用户看到红叉/绿勾后被误导。
                const partial = result.success && result.verdict !== 'ok';
                const ms = result.latency_ms ?? result.response_time_ms;
                const box = partial
                  ? 'bg-warning-subtle text-warning-strong'
                  : result.success
                    ? 'bg-success-subtle text-success-strong'
                    : 'bg-danger-subtle text-danger-strong';
                    const ResultIcon = partial ? AlertTriangle : result.success ? CheckCircle2 : X;
                return (
                  <div className={`p-3 rounded-lg text-sm ${box}`}>
                    <div className="flex items-center gap-2 flex-wrap">
                      <ResultIcon className="h-4 w-4 shrink-0" />
                      <span className="font-medium">
                        {partial ? '链路可达（未返回正文）' : result.success ? '测试成功' : '测试失败'}
                      </span>
                      {typeof ms === 'number' && (
                        <span className="text-xs opacity-75">({ms}ms)</span>
                      )}
                      {result.max_tokens != null && (
                        <span className="text-xs opacity-75">max_tokens={result.max_tokens}</span>
                      )}
                      {result.disable_thinking === false && (
                        <span className="text-xs opacity-75">思考：开</span>
                      )}
                      {result.vision === false && <span className="text-xs opacity-75">不支持图像</span>}
                      {result.vision === null && result.uncertain && (
                        <span className="text-xs opacity-75">视觉能力未确认</span>
                      )}
                    </div>
                    {result.reply ? (
                      <p className="mt-1 text-xs opacity-75 break-all">模型回复：{result.reply}</p>
                    ) : null}
                    {result.hint && <p className="mt-1 text-xs opacity-75">{result.hint}</p>}
                    {!result.success && result.error && (
                      <p className="mt-1 text-xs opacity-75">{result.error}</p>
                    )}
                    {!result.success && result.guide && (
                      <p className="mt-1 text-xs opacity-75">{result.guide}</p>
                    )}
                  </div>
                );
              })()}
            </Card>
          );
        })}
      </div>

      {/* Info Card */}
      <Card title="配置说明" bodyClassName="p-4">
        <ul className="text-sm text-ink-2 space-y-1 list-disc list-inside">
          <li>三个 AI 模块完全独立：文本分析、质检、对话总控各有自己的接口配置</li>
          <li>「文本分析模型」就是 LLM 引擎（小说转剧本、提示词分析等纯文本任务都用它），无需另行配置</li>
          <li>质检模型需要支持图像输入（如 GPT-4o、Qwen-VL、GLM-4V）</li>
          <li>ComfyUI 地址由环境变量 COMFYUI_URL 决定，页面上只做只读展示</li>
          <li>API Key 加密存储，不会在界面明文显示</li>
          <li>建议先点「测试连接」确认接口可用后再保存</li>
        </ul>
      </Card>

      <ConfirmDialog
        isOpen={clearTarget !== null}
        onClose={() => (clearing ? undefined : setClearTarget(null))}
        onConfirm={doClear}
        title="清空配置"
        danger
        loading={clearing}
        confirmText="清空"
        message={
          clearTarget
            ? `确定要清空「${MODULE_CONFIG[clearTarget].title}」的接口配置吗？该模块的 base_url、模型与 API Key 都会被移除，需要重新填写。`
            : null
        }
      />
    </div>
  );
}
