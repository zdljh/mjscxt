import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { ttsApi, mixApi, qcApi } from '@/api/client';
import { Button, Loading, Skeleton } from '@/components/ui';
import { CheckCircle2, Lightbulb, Mic, Volume2, X } from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';

interface AudioTabProps {
  projectKey: string;
}

export function AudioTab({ projectKey }: AudioTabProps) {
  const { t } = useApp();
  const toast = useToast();
  const [activeStep, setActiveStep] = useState<1 | 2 | 3>(1);
  /**
   * C4（2026-09-23 收口）：首屏 4 个 loader（env / 配音计划 / 混音计划 / 质检配置）
   * 此前是 fire-and-forget —— 首屏一片空白且没有任何加载提示，用户无法区分
   * 「正在加载」和「就是没数据」。
   */
  const [bootLoading, setBootLoading] = useState(true);
  
  // Step 1: TTS 配音
  const [ttsEnv, setTtsEnv] = useState<any>(null);
  const [ttsPlan, setTtsPlan] = useState<any>(null);
  const [ttsGenerating, setTtsGenerating] = useState(false);
  const [ttsError, setTtsError] = useState('');
  
  // Step 2: 音画混音
  const [mixPlan, setMixPlan] = useState<any>(null);
  const [mixGenerating, setMixGenerating] = useState(false);
  const [mixError, setMixError] = useState('');
  /** 混音完成后后端回传的「自动登记待验收」结果（null = 尚未出结果） */
  const [mixDeliverable, setMixDeliverable] = useState<
    { registered: boolean; reason: string; episode_no: number } | null
  >(null);
  /** 混音进度（轮询展示，替代原来只 alert 一下就结束） */
  const [mixTask, setMixTask] = useState<{ phase?: string; progress?: number } | null>(null);
  /** 成片音频质检结论（混音完成后由任务下发） */
  const [mixQc, setMixQc] = useState<any>(null);
  
  // Step 3: 音频质检（两层：客观层 ffmpeg 指标 + AI 层频谱/波形送检）
  const [qcResult, setQcResult] = useState<any>(null);
  const [qcLoading, setQcLoading] = useState(false);
  const [qcError, setQcError] = useState('');
  /** 检验对象：mix=带配音成片 / merged=整集音轨 / line=单句 */
  const [qcSource, setQcSource] = useState<'mix' | 'merged' | 'line'>('mix');
  /** 是否带 AI 层（频谱/波形送多模态）。关掉=纯客观层，毫秒级不花模型调用 */
  const [qcWithAi, setQcWithAi] = useState(true);
  /** 质检配置（音频开关 + 三个客观层阈值，可在本页直接调档） */
  const [qcCfg, setQcCfg] = useState<any>(null);
  const [qcSaving, setQcSaving] = useState(false);
  /** 最近一次配音任务的质检结论（任务级下发，用于列出未通过的句子） */
  const [ttsQc, setTtsQc] = useState<any>(null);

  // 加载环境信息
  const loadEnv = async () => {
    try {
      const [ttsRes, mixRes] = await Promise.all([
        ttsApi.env(),
        mixApi.env(),
      ]);
      setTtsEnv(ttsRes);
      if (mixRes?.available) {
        setMixPlan({ available: true });
      }
    } catch (e) {
      console.error('Failed to load env:', e);
    }
  };

  // 加载配音计划
  const loadTtsPlan = async () => {
    try {
      const data = await ttsApi.plan({ project_name: projectKey });
      setTtsPlan(data);
    } catch (e) {
      setTtsError(e instanceof Error ? e.message : t('audio.ttsPlanFailed'));
    }
  };

  // 加载混音计划
  const loadMixPlan = async () => {
    try {
      const data = await mixApi.plan({ project_name: projectKey });
      setMixPlan(data);
    } catch (e) {
      setMixError(e instanceof Error ? e.message : t('audio.mixPlanFailed'));
    }
  };

  useEffect(() => {
    let cancelled = false;
    setBootLoading(true);
    // C4：4 个 loader 各自内部已 catch（Promise.all 不会 reject），只需 finally 收尾
    Promise.all([loadEnv(), loadTtsPlan(), loadMixPlan(), loadQcCfg()])
      .finally(() => { if (!cancelled) setBootLoading(false); });
    return () => { cancelled = true; };
  }, [projectKey]);

  // 读取质检配置（音频开关 + 客观层阈值）
  const loadQcCfg = async () => {
    try {
      const resp: any = await qcApi.config();
      setQcCfg(resp?.config ?? null);
    } catch {
      /* 质检未配置时不影响配音/混音主流程 */
    }
  };

  // 生成配音
  const handleGenerateTTS = async () => {
    setTtsGenerating(true);
    setTtsError('');
    setTtsQc(null);
    try {
      const result = await ttsApi.generate({ project_name: projectKey });
      toast.success(t('audio.ttsStarted', { id: result.task_id }));
      // 轮询到终态：配音结果里带着「台词预检」与「配音质检」两组结论，
      // 不轮询就拿不到（此前只 toast 一句就结束，用户看不到质检发现的问题）
      for (let i = 0; i < 400; i += 1) {
        await new Promise((r) => setTimeout(r, 3000));
        let task: any = null;
        try {
          task = (await ttsApi.status(result.task_id)) as any;
        } catch {
          continue; // 单次轮询失败不中断
        }
        if (task?.status === 'completed' || task?.status === 'failed') {
          setTtsQc({
            prompt_qc: task.prompt_qc || null,
            audio_qc: task.audio_qc || null,
            message: task.message || '',
            status: task.status,
          });
          if (task.status === 'failed') {
            setTtsError(task.error || task.message || t('audio.ttsFailed'));
          }
          break;
        }
      }
      await loadTtsPlan();
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('audio.ttsGenerateFailed');
      setTtsError(msg);
      toast.error(msg);
    } finally {
      setTtsGenerating(false);
    }
  };

  // 运行音频质检
  const handleRunAudioQc = async () => {
    setQcLoading(true);
    setQcError('');
    try {
      const r = await qcApi.checkAudio({
        project_name: projectKey,
        source: qcSource,
        with_ai: qcWithAi,
      });
      setQcResult(r);
    } catch (e) {
      setQcError(e instanceof Error ? e.message : t('audio.qcFailed'));
      setQcResult(null);
    } finally {
      setQcLoading(false);
    }
  };

  // 保存音频质检阈值（只提交音频相关字段，其余配置保持不动）
  const handleSaveQcThresholds = async () => {
    if (!qcCfg) return;
    setQcSaving(true);
    try {
      await qcApi.updateConfig({
        audio_enabled: qcCfg.audio_enabled,
        audio_min_speech_ratio: Number(qcCfg.audio_min_speech_ratio),
        audio_min_mean_db: Number(qcCfg.audio_min_mean_db),
        audio_max_drift: Number(qcCfg.audio_max_drift),
      } as any);
      toast.success(t('audio.qcConfigSaved'));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('settings.saveFailed'));
    } finally {
      setQcSaving(false);
    }
  };

  // 生成混音（异步任务：下发后轮询到终态，顺便把「是否进了验收队列」告诉用户）
  const handleGenerateMix = async () => {
    setMixGenerating(true);
    setMixError('');
    setMixDeliverable(null);
    setMixTask(null);
    setMixQc(null);
    try {
      const result = await mixApi.generate({ project_name: projectKey });
      setMixTask({ phase: t('audio.mixSubmitted'), progress: 0 });
      // 混音本身很快，但配音未就绪时可能排队；最多轮询 10 分钟
      for (let i = 0; i < 200; i += 1) {
        await new Promise((r) => setTimeout(r, 3000));
        let task: any = null;
        try {
          const resp = await mixApi.status(result.task_id);
          task = (resp as any)?.task || resp;
        } catch {
          continue; // 单次轮询失败不中断
        }
        setMixTask({ phase: task?.phase, progress: task?.progress });
        if (task?.status === 'completed' || task?.status === 'failed') {
          if (task.status === 'failed') {
            setMixError(task.message || t('audio.mixFailed'));
          } else {
            setMixDeliverable(task.deliverable || null);
            setMixQc(task.audio_qc || null);
            await loadMixPlan();
          }
          return;
        }
      }
      setMixError(t('audio.mixTimeout'));
    } catch (e) {
      setMixError(e instanceof Error ? e.message : t('audio.mixGenerateFailed'));
    } finally {
      setMixGenerating(false);
    }
  };

  const steps: { id: 1 | 2 | 3; label: string; icon: React.ReactNode }[] = [
    { id: 1, label: t('tts.title'), icon: <Mic className="h-4 w-4" /> },
    { id: 2, label: t('mix.title'), icon: <Volume2 className="h-4 w-4" /> },
    { id: 3, label: t('audio.qcTitle'), icon: <CheckCircle2 className="h-4 w-4" /> },
  ];

  // 首屏加载态：与真实结构同形（步骤导航 + 卡片），避免高度跳变
  if (bootLoading) {
    return (
      <div className="space-y-6" role="status" aria-live="polite" aria-label={t('common.loading')}>
        <div className="flex flex-wrap items-center gap-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-32 rounded-lg" />
          ))}
        </div>
        <Skeleton className="h-48 rounded-lg" />
        <Skeleton className="h-32 rounded-lg" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* 步骤导航 */}
      {/* 保留原生：步骤切换器是「选中态共用同一元素」的分段控件，
          Button 的 rounded-md / h-9 与这里的 rounded-lg 填充块不一致 */}
      {/* flex-wrap：375 视口下三个步骤按钮同排会被裁掉（实测末个按钮 right=389 > 375） */}
      <div className="flex flex-wrap items-center gap-2">
        {steps.map((step, idx) => (
          <React.Fragment key={step.id}>
            <button
              onClick={() => setActiveStep(step.id as 1 | 2 | 3)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ${
                activeStep === step.id
                  ? 'bg-brand text-white shadow-lg'
                  : 'bg-surface-2 text-ink-2 hover:bg-line'
              }`}
            >
              <span>{step.icon}</span>
              <span>{step.label}</span>
              {activeStep === step.id && (
                <span className="ml-1 text-xs opacity-75">●</span>
              )}
            </button>
            {idx < steps.length - 1 && (
              <span className="text-ink-3">→</span>
            )}
          </React.Fragment>
        ))}
      </div>

      {/* Step 1: TTS 配音 */}
      {activeStep === 1 && (
        <div className="space-y-4">
          <div className="bg-surface rounded-lg border border-line p-4">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <Mic className="h-5 w-5" />{t('audio.ttsTitle')}
            </h3>
            
            {ttsEnv && (
              <div className={`p-3 rounded-lg mb-4 ${
                ttsEnv.available
                  ? 'bg-success-subtle border border-success/30 text-success-strong'
                  : 'bg-danger-subtle border border-danger/30 text-danger-strong'
              }`}>
                <span className="font-medium inline-flex items-center gap-1.5">
                  {ttsEnv.available ? (
                    <>
                      <CheckCircle2 className="h-4 w-4" />{t('tts.envAvailable')}
                    </>
                  ) : (
                    <>
                      <X className="h-4 w-4" />{t('tts.envUnavailable')}
                    </>
                  )}
                </span>
                {!ttsEnv.available && ttsEnv.reasons && (
                  <ul className="mt-2 text-sm list-disc list-inside">
                    {ttsEnv.reasons.map((r: string, i: number) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            {ttsPlan && (
              <div className="mb-4">
                <h4 className="font-medium text-ink-1 mb-3">{t('tts.planPreview')}</h4>
                <div className="grid grid-cols-3 gap-4 mb-4">
                  <div className="bg-surface-2 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-brand">
                      {ttsPlan.line_count ?? (ttsPlan.lines?.length ?? 0)}
                    </div>
                    <div className="text-sm text-ink-2">{t('tts.totalLines')}</div>
                  </div>
                  <div className="bg-surface-2 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-success-strong">
                      {ttsPlan.characters?.length || 0}
                    </div>
                    <div className="text-sm text-ink-2">{t('tts.characters')}</div>
                  </div>
                  <div className="bg-surface-2 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-brand">{ttsPlan.episode}</div>
                    <div className="text-sm text-ink-2">{t('tts.episode')}</div>
                  </div>
                </div>

                {/* 剧本体检：兜底镜头（无台词、无 prompt_h3）在配音环节会变成
                    「一句也合不出来」，必须在这里就说清楚，而不是等用户白跑一轮 */}
                {Array.isArray(ttsPlan.warnings) && ttsPlan.warnings.length > 0 && (
                  <div className="mb-4 p-3 rounded-lg bg-warning-subtle border border-warning/30 text-warning-strong text-xs space-y-1">
                    <div className="font-medium">{t('audio.scriptCheck')}</div>
                    {ttsPlan.warnings.map((w: string, i: number) => (
                      <div key={i}>· {w}</div>
                    ))}
                  </div>
                )}

                {ttsPlan.lines && ttsPlan.lines.length > 0 && (
                  <div className="max-h-40 overflow-y-auto space-y-1 mb-4">
                    {ttsPlan.lines.slice(0, 10).map((line: any, idx: number) => (
                      <div key={idx} className="text-sm text-ink-2 px-2 py-1 bg-surface-2 rounded">
                        <span className="font-mono text-ink-3">#{line.shot_id ?? idx + 1}</span>
                        <span className="ml-2">{line.text}</span>
                        {line.character && <span className="ml-2 text-brand">[{line.character}]</span>}
                      </div>
                    ))}
                    {ttsPlan.lines.length > 10 && (
                      <div className="text-xs text-ink-3 text-center py-1">{t('audio.moreLines', { n: ttsPlan.lines.length - 10 })}</div>
                    )}
                  </div>
                )}

                {ttsPlan.lines && ttsPlan.lines.length === 0 && (
                  <div className="mb-4 p-3 rounded-lg bg-danger-subtle border border-danger/30 text-danger-strong text-xs">
                    {t('audio.noLines')}
                  </div>
                )}

                <Button
                  onClick={handleGenerateTTS}
                  disabled={ttsGenerating || !ttsEnv?.available}
                  className="w-full bg-success py-3 text-white hover:bg-success-strong"
                >
                  {ttsGenerating ? t('common.generating') : t('tts.generate')}
                </Button>
              </div>
            )}

            {ttsError && (
              <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
                {ttsError}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Step 2: 音画混音 */}
      {activeStep === 2 && (
        <div className="space-y-4">
          <div className="bg-surface rounded-lg border border-line p-4">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <Volume2 className="h-5 w-5" />{t('audio.mixTitle')}
            </h3>

            {mixPlan && (
              <div className="mb-4">
                <div className="grid grid-cols-3 gap-4 mb-4">
                  <div className="bg-surface-2 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-brand">{mixPlan.video_count ?? 0}</div>
                    <div className="text-sm text-ink-2">{t('mix.videoCount')}</div>
                  </div>
                  <div className="bg-surface-2 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-warning-strong">{mixPlan.audio_count ?? 0}</div>
                    <div className="text-sm text-ink-2">{t('mix.audioCount')}</div>
                  </div>
                  <div className="bg-surface-2 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-brand">{mixPlan.to_generate ?? 0}</div>
                    <div className="text-sm text-ink-2">{t('audio.pendingMix')}</div>
                  </div>
                </div>

                {mixPlan.lines && mixPlan.lines.length > 0 && (
                  <div className="max-h-40 overflow-y-auto space-y-1 mb-4">
                    {mixPlan.lines.map((line: any, idx: number) => (
                      <div key={idx} className="text-sm text-ink-2 px-2 py-1 bg-surface-2 rounded flex justify-between">
                        <span>{line.video}</span>
                        <span>+</span>
                        <span>{line.audio}</span>
                        <span>→</span>
                        <span className="text-success-strong">{line.output}</span>
                      </div>
                    ))}
                  </div>
                )}

                <Button
                  onClick={handleGenerateMix}
                  disabled={mixGenerating}
                  variant="brand"
                  className="w-full py-3"
                >
                  {mixGenerating ? t('audio.mixing') : t('mix.generate')}
                </Button>

                {mixGenerating && mixTask?.phase && (
                  <div className="mt-3">
                    <div className="flex justify-between text-xs text-ink-2 mb-1">
                      <span>{mixTask.phase}</span>
                      <span>{mixTask.progress ?? 0}%</span>
                    </div>
                    <div className="h-1.5 rounded-full bg-line overflow-hidden">
                      <div
                        className="h-full bg-brand transition-all"
                        style={{ width: `${Math.max(0, Math.min(100, mixTask.progress ?? 0))}%` }}
                      />
                    </div>
                  </div>
                )}
              </div>
            )}

            {mixError && (
              <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
                {mixError}
              </div>
            )}

            {mixDeliverable && (
              <div
                className={`p-3 rounded-lg text-sm border ${
                  mixDeliverable.registered
                    ? 'bg-success-subtle border-success/30 text-success-strong'
                    : 'bg-warning-subtle border-warning/30 text-warning-strong'
                }`}
              >
                {mixDeliverable.registered
                  ? t('audio.mixRegistered', { n: mixDeliverable.episode_no })
                  : t('audio.mixNotRegistered', { reason: mixDeliverable.reason || t('audio.reasonUnknown') })}
              </div>
            )}

            {/* 成片音频质检：ffmpeg 返回成功 ≠ 配音真的混进去了（可能是合法但无声的音轨） */}
            {mixQc?.enabled && mixQc?.passed != null && (
              <div
                className={`p-3 rounded-lg text-sm border ${
                  mixQc.passed
                    ? 'bg-success-subtle border-success/30 text-success-strong'
                    : 'bg-warning-subtle border-warning/30 text-warning-strong'
                }`}
              >
                {t('audio.finalQcResult', { result: mixQc.passed ? t('qc.passed') : t('audio.mixQcNotPassed') })}
                {mixQc.reason ? `（${mixQc.reason}）` : ''}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Step 3: 音频质检 */}
      {activeStep === 3 && (
        <div className="space-y-4">
          <div className="bg-surface rounded-lg border border-line p-4">
            <h3 className="text-lg font-semibold mb-2 flex items-center gap-2">
              <CheckCircle2 className="h-5 w-5" />{t('audio.qcTitle')}
            </h3>
            <p className="text-sm text-ink-2 mb-4">
              {t('audio.qcDescIntro')}<b>{t('audio.layerObjective')}</b>{t('audio.qcDescObjective')}<b>{t('audio.layerAi')}</b>{t('audio.qcDescAi')}
            </p>

            {/* 检验对象 */}
            <div className="mb-4">
              <div className="text-xs text-ink-2 mb-2">{t('audio.qcSource')}</div>
              {/* 保留原生：同上的分段单选（rounded 4px 与 Button 的 rounded-md 不一致） */}
              <div className="flex flex-wrap gap-2">
                {([
                  { id: 'mix', label: t('audio.sourceMix'), hint: t('audio.scopeFullTrack') },
                  { id: 'merged', label: t('audio.sourceMerged'), hint: t('audio.scopeFullTrack') },
                  { id: 'line', label: t('audio.sourceLine'), hint: t('audio.scopeSingleLine') },
                ] as const).map((s) => (
                  <button
                    key={s.id}
                    onClick={() => setQcSource(s.id)}
                    className={`px-3 py-1.5 rounded text-sm border transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ${
                      qcSource === s.id
                        ? 'bg-brand text-white border-brand'
                        : 'bg-surface text-ink-2 border-line hover:border-brand'
                    }`}
                  >
                    {s.label}
                    <span className="ml-1 text-xs opacity-70">{s.hint}</span>
                  </button>
                ))}
              </div>
            </div>

            {/* AI 层开关 + 配置状态 */}
            <div className="mb-4 p-3 rounded-lg bg-surface-2 space-y-2">
              <label className="flex items-center gap-2 text-sm text-ink-1">
                {/* 保留原生：共享组件未覆盖 checkbox */}
                <input
                  type="checkbox"
                  checked={qcWithAi}
                  onChange={(e) => setQcWithAi(e.target.checked)}
                  className="rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
                />
                {t('audio.enableAiLayer')}
                <span className="text-xs text-ink-3">{t('audio.aiLayerHint')}</span>
              </label>
              {qcCfg && (
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className={`px-2 py-0.5 rounded ${
                    qcCfg.audio_qc_active
                      ? 'bg-success-subtle text-success-strong'
                      : 'bg-surface-2 text-ink-2'
                  }`}>
                    {t('audio.layerObjective')} {qcCfg.audio_qc_active ? t('audio.available') : t('audio.disabled')}
                  </span>
                  <span className={`px-2 py-0.5 rounded ${
                    qcCfg.audio_ai_active
                      ? 'bg-brand-subtle text-brand-hover'
                      : 'bg-warning-subtle text-warning-strong'
                  }`}>
                    {t('audio.layerAi')} {qcCfg.audio_ai_active ? t('audio.available') : t('audio.aiNotConfigured')}
                  </span>
                </div>
              )}
            </div>

            {/* 阈值（此前这些配置项在后端存在但前端没有任何入口） */}
            {/* 保留原生：这三个 number 输入带 step/min/max 约束与数值型默认值（?? 0.5 / -45），
                Input 组件未开放 step/min/max，换成 Input 会静默丢掉步进与取值范围 */}
            {qcCfg && (
              <div className="mb-4 grid grid-cols-1 md:grid-cols-3 gap-3">
                <div>
                  <div className="text-xs text-ink-2 mb-1">{t('audio.minSpeechRatio')}</div>
                  <input
                    type="number" step="0.05" min="0" max="1"
                    value={qcCfg.audio_min_speech_ratio ?? 0.5}
                    onChange={(e) => setQcCfg({ ...qcCfg, audio_min_speech_ratio: e.target.value })}
                    className="w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
                  />
                  <div className="text-xs text-ink-3 mt-1">{t('audio.minSpeechRatioHint')}</div>
                </div>
                <div>
                  <div className="text-xs text-ink-2 mb-1">{t('audio.minMeanDb')}</div>
                  <input
                    type="number" step="1" min="-100" max="0"
                    value={qcCfg.audio_min_mean_db ?? -45}
                    onChange={(e) => setQcCfg({ ...qcCfg, audio_min_mean_db: e.target.value })}
                    className="w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
                  />
                  <div className="text-xs text-ink-3 mt-1">{t('audio.minMeanDbHint')}</div>
                </div>
                <div>
                  <div className="text-xs text-ink-2 mb-1">{t('audio.maxDrift')}</div>
                  <input
                    type="number" step="0.05" min="0" max="5"
                    value={qcCfg.audio_max_drift ?? 0.5}
                    onChange={(e) => setQcCfg({ ...qcCfg, audio_max_drift: e.target.value })}
                    className="w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
                  />
                  <div className="text-xs text-ink-3 mt-1">{t('audio.maxDriftHint')}</div>
                </div>
                <div className="md:col-span-3">
                  <Button onClick={handleSaveQcThresholds} disabled={qcSaving} className="text-sm">
                    {qcSaving ? t('audio.saving') : t('audio.saveQcConfig')}
                  </Button>
                </div>
              </div>
            )}

            <Button
              onClick={handleRunAudioQc}
              disabled={qcLoading}
              variant="brand"
              className="w-full py-2"
            >
              {qcLoading ? t('audio.qcRunning') : t('audio.runQc')}
            </Button>

            {qcError && (
              <div className="mt-3 p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
                {qcError}
              </div>
            )}
          </div>

          {/* 质检结论 */}
          {qcResult && (
            <div className="bg-surface rounded-lg border border-line p-4">
              <div className="flex items-center gap-2 mb-3 flex-wrap">
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                  qcResult.blocked
                    ? 'bg-danger-subtle text-danger-strong'
                    : qcResult.passed
                      ? 'bg-success-subtle text-success-strong'
                      : 'bg-warning-subtle text-warning-strong'
                }`}>
                  {qcResult.blocked ? t('audio.criticalIssues') : qcResult.passed ? t('qc.passed') : t('audio.resultNotPassed')}
                </span>
                <span className="text-sm text-ink-2">
                  {t('audio.scoreLabel')} {qcResult.score ?? '—'}
                </span>
                <span className="text-xs text-ink-3">
                  {qcResult.objective_only ? t('audio.objectiveOnly') : qcResult.ai_used ? t('audio.objectivePlusAi') : t('audio.layerObjective')}
                </span>
                {qcResult.check_speech_ratio === false && (
                  <span className="text-xs text-ink-3">{t('audio.fullTrackNote')}</span>
                )}
              </div>

              <div className="text-sm text-ink-1 mb-3">{qcResult.reason}</div>

              {qcResult.ai_skip_reason && (
                <div className="mb-3 p-2 rounded bg-warning-subtle text-warning-strong text-xs">
                  {t('audio.aiSkipped', { reason: qcResult.ai_skip_reason })}
                </div>
              )}

              {/* 客观指标 */}
              {qcResult.metrics && (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
                  {[
                    { k: t('audio.metricDuration'), v: qcResult.metrics.duration != null ? `${qcResult.metrics.duration}s` : '—' },
                    { k: t('audio.metricSpeechRatio'), v: qcResult.metrics.speech_ratio != null ? `${(qcResult.metrics.speech_ratio * 100).toFixed(1)}%` : '—' },
                    { k: t('audio.metricMeanDb'), v: qcResult.metrics.mean_db != null ? `${qcResult.metrics.mean_db} dB` : '—' },
                    { k: t('audio.metricMaxDb'), v: qcResult.metrics.max_db != null ? `${qcResult.metrics.max_db} dB` : '—' },
                  ].map((m) => (
                    <div key={m.k} className="bg-surface-2 rounded p-2 text-center">
                      <div className="text-sm font-semibold text-ink-1">{m.v}</div>
                      <div className="text-xs text-ink-2">{m.k}</div>
                    </div>
                  ))}
                </div>
              )}

              {Array.isArray(qcResult.critical_issues) && qcResult.critical_issues.length > 0 && (
                <div className="mb-3">
                  <div className="text-xs font-medium text-danger-strong mb-1">{t('audio.criticalIssues')}</div>
                  <ul className="text-sm text-danger-strong space-y-0.5 list-disc list-inside">
                    {qcResult.critical_issues.map((x: string, i: number) => <li key={i}>{x}</li>)}
                  </ul>
                </div>
              )}
              {Array.isArray(qcResult.issues) && qcResult.issues.length > 0 && (
                <div className="mb-3">
                  <div className="text-xs font-medium text-warning-strong mb-1">{t('audio.improvable')}</div>
                  <ul className="text-sm text-warning-strong space-y-0.5 list-disc list-inside">
                    {qcResult.issues.map((x: string, i: number) => <li key={i}>{x}</li>)}
                  </ul>
                </div>
              )}

              {/* 频谱图 + 波形图（AI 层送检用的同一批图，顺序固定：先频谱后波形） */}
              {Array.isArray(qcResult.visuals) && qcResult.visuals.length > 0 && (
                <div className="space-y-2">
                  <div className="text-xs text-ink-2">
                    {t('audio.visualsNote')}
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    {qcResult.visuals.map((src: string, i: number) => (
                      <div key={i} className="bg-black/5 rounded p-1">
                        <img src={src} alt={i === 0 ? t('audio.spectrogram') : t('audio.waveform')} className="w-full rounded" />
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {qcResult.file_url && (
                <audio controls src={qcResult.file_url} className="w-full mt-3" />
              )}
              <div className="mt-2 text-xs text-ink-3 break-all">{t('audio.target', { path: qcResult.path })}</div>
            </div>
          )}

          {/* 未通过的句子（来自最近一次配音任务：任务级质检结论） */}
          {ttsQc?.audio_qc?.enabled && (
            <div className="bg-surface rounded-lg border border-line p-4">
              <h4 className="font-medium text-ink-1 mb-2">
                {t('audio.lastTtsQc')}
                <span className="ml-2 text-xs font-normal text-ink-2">
                  {t('qc.passed')} {ttsQc.audio_qc.passed}/{ttsQc.audio_qc.checked}
                  {ttsQc.audio_qc.retried ? t('audio.retriedLines', { n: ttsQc.audio_qc.retried }) : ''}
                  {ttsQc.audio_qc.recovered ? t('audio.recoveredLines', { n: ttsQc.audio_qc.recovered }) : ''}
                </span>
              </h4>
              {Array.isArray(ttsQc.audio_qc.problems) && ttsQc.audio_qc.problems.length > 0 ? (
                <ul className="space-y-2">
                  {ttsQc.audio_qc.problems.map((p: any, i: number) => (
                    <li key={i} className="text-sm p-2 rounded bg-danger/5 border border-danger/20">
                      <span className="font-mono text-xs text-ink-3">
                        #{p.shot_id ?? i + 1}
                      </span>
                      <span className="ml-2 text-ink-1">
                        {p.character ? `${p.character}：` : ''}{p.reason}
                      </span>
                      {p.visuals && p.visuals.length > 0 && (
                        <div className="mt-2 grid grid-cols-2 gap-2">
                          {p.visuals.map((src: string, k: number) => (
                            <img key={k} src={src} alt={t('audio.qcImage')} className="w-full rounded bg-black/5" />
                          ))}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="text-sm text-success-strong">{t('audio.allLinesPassed')}</div>
              )}
            </div>
          )}

          {/* 台词预检结论（结构化残留会被 TTS 念出来，这里先摊开给用户看） */}
          {ttsQc?.prompt_qc?.enabled && (
            <div className="bg-surface rounded-lg border border-line p-4">
              <h4 className="font-medium text-ink-1 mb-2">
                {t('audio.promptQc')}
                <span className="ml-2 text-xs font-normal text-ink-2">
                  {t('audio.promptQcSummary', { checked: ttsQc.prompt_qc.checked, repaired: ttsQc.prompt_qc.repaired_lines })}
                  {ttsQc.prompt_qc.blocked ? t('audio.promptQcBlocked', { n: ttsQc.prompt_qc.blocked }) : ''}
                </span>
              </h4>
              {Array.isArray(ttsQc.prompt_qc.problem_lines) && ttsQc.prompt_qc.problem_lines.length > 0 ? (
                <ul className="space-y-2 text-sm">
                  {ttsQc.prompt_qc.problem_lines.map((p: any, i: number) => (
                    <li key={i} className="p-2 rounded bg-warning/5 border border-warning/20">
                      <span className="font-mono text-xs text-ink-3">#{p.shot_id ?? i + 1}</span>
                      {p.repairs && p.repairs.length > 0 && (
                        <span className="ml-2 text-success-strong">
                          {t('audio.repaired', { items: p.repairs.join('、') })}
                        </span>
                      )}
                      {Array.isArray(p.issues) && p.issues.length > 0 && (
                        <div className="text-warning-strong mt-0.5">
                          {p.issues.join('；')}
                        </div>
                      )}
                      {p.rebuild_hint && (
                        <div className="text-xs text-ink-2 mt-0.5">{t('audio.suggestion', { hint: p.rebuild_hint })}</div>
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="text-sm text-success-strong">{t('audio.promptQcAllPassed')}</div>
              )}
            </div>
          )}
        </div>
      )}

      {/* 流程说明 */}
      <div className="bg-info-subtle border border-info/30 rounded-lg p-4">
        <h4 className="font-medium text-info-strong mb-2 flex items-center gap-1.5"><Lightbulb className="h-4 w-4" />{t('audio.workflowTitle')}</h4>
        <ol className="list-decimal list-inside space-y-1 text-sm text-info-strong">
          <li>{t('audio.workflowStep1')}</li>
          <li>{t('audio.workflowStep2')}</li>
          <li>{t('audio.workflowStep3')}</li>
        </ol>
      </div>
    </div>
  );
}
