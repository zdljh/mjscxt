import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { ttsApi, mixApi, qcApi } from '@/api/client';
import { Button, Loading } from '@/components/ui';
import { useToast } from '@/components/ui/toast';

interface AudioTabProps {
  projectKey: string;
}

export function AudioTab({ projectKey }: AudioTabProps) {
  const { t } = useApp();
  const toast = useToast();
  const [activeStep, setActiveStep] = useState<1 | 2 | 3>(1);
  
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
      setTtsError(e instanceof Error ? e.message : '获取配音计划失败');
    }
  };

  // 加载混音计划
  const loadMixPlan = async () => {
    try {
      const data = await mixApi.plan({ project_name: projectKey });
      setMixPlan(data);
    } catch (e) {
      setMixError(e instanceof Error ? e.message : '获取混音计划失败');
    }
  };

  useEffect(() => {
    loadEnv();
    loadTtsPlan();
    loadMixPlan();
    loadQcCfg();
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
      toast.success(`配音生成任务已启动：${result.task_id}`);
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
            setTtsError(task.error || task.message || '配音失败');
          }
          break;
        }
      }
      await loadTtsPlan();
    } catch (e) {
      const msg = e instanceof Error ? e.message : '配音生成失败';
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
      setQcError(e instanceof Error ? e.message : '音频质检失败');
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
      toast.success('音频质检配置已保存');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '保存失败');
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
      setMixTask({ phase: '任务已下发', progress: 0 });
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
            setMixError(task.message || '混音失败');
          } else {
            setMixDeliverable(task.deliverable || null);
            setMixQc(task.audio_qc || null);
            await loadMixPlan();
          }
          return;
        }
      }
      setMixError('混音任务轮询超时，请到「成品验收」页刷新查看结果');
    } catch (e) {
      setMixError(e instanceof Error ? e.message : '混音生成失败');
    } finally {
      setMixGenerating(false);
    }
  };

  const steps = [
    { id: 1, label: 'TTS 配音', icon: '🎙️' },
    { id: 2, label: '音画混音', icon: '🔊' },
    { id: 3, label: '音频质检', icon: '✅' },
  ];

  return (
    <div className="space-y-6">
      {/* 步骤导航 */}
      <div className="flex items-center gap-2">
        {steps.map((step, idx) => (
          <React.Fragment key={step.id}>
            <button
              onClick={() => setActiveStep(step.id as 1 | 2 | 3)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                activeStep === step.id
                  ? 'bg-indigo-600 text-white shadow-lg'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              <span className="text-lg">{step.icon}</span>
              <span>{step.label}</span>
              {activeStep === step.id && (
                <span className="ml-1 text-xs opacity-75">●</span>
              )}
            </button>
            {idx < steps.length - 1 && (
              <span className="text-gray-400">→</span>
            )}
          </React.Fragment>
        ))}
      </div>

      {/* Step 1: TTS 配音 */}
      {activeStep === 1 && (
        <div className="space-y-4">
          <div className="bg-white rounded-lg border border-gray-200 p-4">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <span>🎙️</span> TTS 配音生成
            </h3>
            
            {ttsEnv && (
              <div className={`p-3 rounded-lg mb-4 ${
                ttsEnv.available
                  ? 'bg-green-500/10 border border-green-500/30 text-green-700'
                  : 'bg-red-500/10 border border-red-500/30 text-red-700'
              }`}>
                <span className="font-medium">
                  {ttsEnv.available ? '✅ TTS 环境可用' : '❌ TTS 环境不可用'}
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
                <h4 className="font-medium text-gray-700 mb-3">配音计划预览</h4>
                <div className="grid grid-cols-3 gap-4 mb-4">
                  <div className="bg-gray-50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-indigo-500">
                      {ttsPlan.line_count ?? (ttsPlan.lines?.length ?? 0)}
                    </div>
                    <div className="text-sm text-gray-500">总台词数</div>
                  </div>
                  <div className="bg-gray-50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-green-500">
                      {ttsPlan.characters?.length || 0}
                    </div>
                    <div className="text-sm text-gray-500">涉及角色</div>
                  </div>
                  <div className="bg-gray-50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-blue-500">{ttsPlan.episode}</div>
                    <div className="text-sm text-gray-500">集数</div>
                  </div>
                </div>

                {/* 剧本体检：兜底镜头（无台词、无 prompt_h3）在配音环节会变成
                    「一句也合不出来」，必须在这里就说清楚，而不是等用户白跑一轮 */}
                {Array.isArray(ttsPlan.warnings) && ttsPlan.warnings.length > 0 && (
                  <div className="mb-4 p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-700 text-xs space-y-1">
                    <div className="font-medium">剧本体检提醒</div>
                    {ttsPlan.warnings.map((w: string, i: number) => (
                      <div key={i}>· {w}</div>
                    ))}
                  </div>
                )}

                {ttsPlan.lines && ttsPlan.lines.length > 0 && (
                  <div className="max-h-40 overflow-y-auto space-y-1 mb-4">
                    {ttsPlan.lines.slice(0, 10).map((line: any, idx: number) => (
                      <div key={idx} className="text-sm text-gray-600 px-2 py-1 bg-gray-50 rounded">
                        <span className="font-mono text-gray-400">#{line.shot_id ?? idx + 1}</span>
                        <span className="ml-2">{line.text}</span>
                        {line.character && <span className="ml-2 text-indigo-500">[{line.character}]</span>}
                      </div>
                    ))}
                    {ttsPlan.lines.length > 10 && (
                      <div className="text-xs text-gray-400 text-center py-1">... 还有 {ttsPlan.lines.length - 10} 条台词</div>
                    )}
                  </div>
                )}

                {ttsPlan.lines && ttsPlan.lines.length === 0 && (
                  <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-500 text-xs">
                    该集没有可朗读台词，无法生成配音。请先补台词或重新生成剧本。
                  </div>
                )}

                <Button
                  onClick={handleGenerateTTS}
                  disabled={ttsGenerating || !ttsEnv?.available}
                  className="w-full bg-success py-3 text-white hover:bg-success-strong"
                >
                  {ttsGenerating ? '生成中...' : '生成配音'}
                </Button>
              </div>
            )}

            {ttsError && (
              <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
                {ttsError}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Step 2: 音画混音 */}
      {activeStep === 2 && (
        <div className="space-y-4">
          <div className="bg-white rounded-lg border border-gray-200 p-4">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <span>🔊</span> 音画混音合成
            </h3>

            {mixPlan && (
              <div className="mb-4">
                <div className="grid grid-cols-3 gap-4 mb-4">
                  <div className="bg-gray-50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-purple-500">{mixPlan.video_count ?? 0}</div>
                    <div className="text-sm text-gray-500">视频片段</div>
                  </div>
                  <div className="bg-gray-50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-yellow-500">{mixPlan.audio_count ?? 0}</div>
                    <div className="text-sm text-gray-500">音频片段</div>
                  </div>
                  <div className="bg-gray-50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-indigo-500">{mixPlan.to_generate ?? 0}</div>
                    <div className="text-sm text-gray-500">待合成</div>
                  </div>
                </div>

                {mixPlan.lines && mixPlan.lines.length > 0 && (
                  <div className="max-h-40 overflow-y-auto space-y-1 mb-4">
                    {mixPlan.lines.map((line: any, idx: number) => (
                      <div key={idx} className="text-sm text-gray-600 px-2 py-1 bg-gray-50 rounded flex justify-between">
                        <span>{line.video}</span>
                        <span>+</span>
                        <span>{line.audio}</span>
                        <span>→</span>
                        <span className="text-green-600">{line.output}</span>
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
                  {mixGenerating ? '混音中...' : '开始混音'}
                </Button>

                {mixGenerating && mixTask?.phase && (
                  <div className="mt-3">
                    <div className="flex justify-between text-xs text-gray-500 mb-1">
                      <span>{mixTask.phase}</span>
                      <span>{mixTask.progress ?? 0}%</span>
                    </div>
                    <div className="h-1.5 rounded-full bg-gray-200 overflow-hidden">
                      <div
                        className="h-full bg-violet-500 transition-all"
                        style={{ width: `${Math.max(0, Math.min(100, mixTask.progress ?? 0))}%` }}
                      />
                    </div>
                  </div>
                )}
              </div>
            )}

            {mixError && (
              <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
                {mixError}
              </div>
            )}

            {mixDeliverable && (
              <div
                className={`p-3 rounded-lg text-sm border ${
                  mixDeliverable.registered
                    ? 'bg-green-500/10 border-green-500/30 text-green-600'
                    : 'bg-amber-500/10 border-amber-500/30 text-amber-600'
                }`}
              >
                {mixDeliverable.registered
                  ? `带配音成片已生成，并自动加入「成品验收」第 ${mixDeliverable.episode_no} 集待验收`
                  : `带配音成片已生成，但未进入验收队列：${mixDeliverable.reason || '原因未知'}`}
              </div>
            )}

            {/* 成片音频质检：ffmpeg 返回成功 ≠ 配音真的混进去了（可能是合法但无声的音轨） */}
            {mixQc?.enabled && mixQc?.passed != null && (
              <div
                className={`p-3 rounded-lg text-sm border ${
                  mixQc.passed
                    ? 'bg-green-500/10 border-green-500/30 text-green-600'
                    : 'bg-amber-500/10 border-amber-500/30 text-amber-700'
                }`}
              >
                成片音频质检：{mixQc.passed ? '通过' : '未通过'}
                {mixQc.reason ? `（${mixQc.reason}）` : ''}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Step 3: 音频质检 */}
      {activeStep === 3 && (
        <div className="space-y-4">
          <div className="bg-white rounded-lg border border-gray-200 p-4">
            <h3 className="text-lg font-semibold mb-2 flex items-center gap-2">
              <span>✅</span> 音频质检
            </h3>
            <p className="text-sm text-gray-500 mb-4">
              两层判定：<b>客观层</b>用 ffmpeg 实测时长 / 平均电平 / 峰值 / 有声占比，
              零成本、毫秒级，挡「整段无声、削波、时长失控」；
              <b>AI 层</b>把音频渲染成频谱图与波形图交给多模态模型判读内容问题。
            </p>

            {/* 检验对象 */}
            <div className="mb-4">
              <div className="text-xs text-gray-500 mb-2">检验对象</div>
              <div className="flex flex-wrap gap-2">
                {([
                  { id: 'mix', label: '带配音成片', hint: '整轨口径' },
                  { id: 'merged', label: '整集音轨', hint: '整轨口径' },
                  { id: 'line', label: '单句配音', hint: '单句口径' },
                ] as const).map((s) => (
                  <button
                    key={s.id}
                    onClick={() => setQcSource(s.id)}
                    className={`px-3 py-1.5 rounded text-sm border transition ${
                      qcSource === s.id
                        ? 'bg-indigo-600 text-white border-indigo-600'
                        : 'bg-white text-gray-600 border-gray-200 hover:border-indigo-400'
                    }`}
                  >
                    {s.label}
                    <span className="ml-1 text-xs opacity-70">{s.hint}</span>
                  </button>
                ))}
              </div>
            </div>

            {/* AI 层开关 + 配置状态 */}
            <div className="mb-4 p-3 rounded-lg bg-gray-50 space-y-2">
              <label className="flex items-center gap-2 text-sm text-gray-700">
                <input
                  type="checkbox"
                  checked={qcWithAi}
                  onChange={(e) => setQcWithAi(e.target.checked)}
                  className="rounded"
                />
                启用 AI 层（频谱图 + 波形图送多模态模型）
                <span className="text-xs text-gray-400">关掉则只跑客观层，不消耗模型调用</span>
              </label>
              {qcCfg && (
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className={`px-2 py-0.5 rounded ${
                    qcCfg.audio_qc_active
                      ? 'bg-green-100 text-green-700'
                      : 'bg-gray-100 text-gray-500'
                  }`}>
                    客观层 {qcCfg.audio_qc_active ? '可用' : '已关闭'}
                  </span>
                  <span className={`px-2 py-0.5 rounded ${
                    qcCfg.audio_ai_active
                      ? 'bg-indigo-100 text-indigo-700'
                      : 'bg-yellow-100 text-yellow-700'
                  }`}>
                    AI 层 {qcCfg.audio_ai_active ? '可用' : '未配置质检接口'}
                  </span>
                </div>
              )}
            </div>

            {/* 阈值（此前这些配置项在后端存在但前端没有任何入口） */}
            {qcCfg && (
              <div className="mb-4 grid grid-cols-1 md:grid-cols-3 gap-3">
                <div>
                  <div className="text-xs text-gray-500 mb-1">有声占比下限</div>
                  <input
                    type="number" step="0.05" min="0" max="1"
                    value={qcCfg.audio_min_speech_ratio ?? 0.5}
                    onChange={(e) => setQcCfg({ ...qcCfg, audio_min_speech_ratio: e.target.value })}
                    className="w-full px-2 py-1 text-sm rounded border border-gray-200 bg-white text-gray-900"
                  />
                  <div className="text-xs text-gray-400 mt-1">低于此值提示「静音过多」</div>
                </div>
                <div>
                  <div className="text-xs text-gray-500 mb-1">平均电平下限（dB）</div>
                  <input
                    type="number" step="1" min="-100" max="0"
                    value={qcCfg.audio_min_mean_db ?? -45}
                    onChange={(e) => setQcCfg({ ...qcCfg, audio_min_mean_db: e.target.value })}
                    className="w-full px-2 py-1 text-sm rounded border border-gray-200 bg-white text-gray-900"
                  />
                  <div className="text-xs text-gray-400 mt-1">低于此值提示「音量偏小」</div>
                </div>
                <div>
                  <div className="text-xs text-gray-500 mb-1">时长偏差上限</div>
                  <input
                    type="number" step="0.05" min="0" max="5"
                    value={qcCfg.audio_max_drift ?? 0.5}
                    onChange={(e) => setQcCfg({ ...qcCfg, audio_max_drift: e.target.value })}
                    className="w-full px-2 py-1 text-sm rounded border border-gray-200 bg-white text-gray-900"
                  />
                  <div className="text-xs text-gray-400 mt-1">实测与期望时长的比例偏差</div>
                </div>
                <div className="md:col-span-3">
                  <Button onClick={handleSaveQcThresholds} disabled={qcSaving} className="text-sm">
                    {qcSaving ? '保存中...' : '保存音频质检配置'}
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
              {qcLoading ? '质检中...' : '运行音频质检'}
            </Button>

            {qcError && (
              <div className="mt-3 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
                {qcError}
              </div>
            )}
          </div>

          {/* 质检结论 */}
          {qcResult && (
            <div className="bg-white rounded-lg border border-gray-200 p-4">
              <div className="flex items-center gap-2 mb-3 flex-wrap">
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                  qcResult.blocked
                    ? 'bg-red-100 text-red-700'
                    : qcResult.passed
                      ? 'bg-green-100 text-green-700'
                      : 'bg-amber-100 text-amber-700'
                }`}>
                  {qcResult.blocked ? '关键缺陷' : qcResult.passed ? '通过' : '不通过'}
                </span>
                <span className="text-sm text-gray-500">
                  评分 {qcResult.score ?? '—'}
                </span>
                <span className="text-xs text-gray-400">
                  {qcResult.objective_only ? '仅客观层' : qcResult.ai_used ? '客观层 + AI 层' : '客观层'}
                </span>
                {qcResult.check_speech_ratio === false && (
                  <span className="text-xs text-gray-400">（整轨口径：不做有声占比判定）</span>
                )}
              </div>

              <div className="text-sm text-gray-700 mb-3">{qcResult.reason}</div>

              {qcResult.ai_skip_reason && (
                <div className="mb-3 p-2 rounded bg-amber-500/10 text-amber-700 text-xs">
                  AI 层未参与：{qcResult.ai_skip_reason}
                </div>
              )}

              {/* 客观指标 */}
              {qcResult.metrics && (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
                  {[
                    { k: '时长', v: qcResult.metrics.duration != null ? `${qcResult.metrics.duration}s` : '—' },
                    { k: '有声占比', v: qcResult.metrics.speech_ratio != null ? `${(qcResult.metrics.speech_ratio * 100).toFixed(1)}%` : '—' },
                    { k: '平均电平', v: qcResult.metrics.mean_db != null ? `${qcResult.metrics.mean_db} dB` : '—' },
                    { k: '峰值电平', v: qcResult.metrics.max_db != null ? `${qcResult.metrics.max_db} dB` : '—' },
                  ].map((m) => (
                    <div key={m.k} className="bg-gray-50 rounded p-2 text-center">
                      <div className="text-sm font-semibold text-gray-900">{m.v}</div>
                      <div className="text-xs text-gray-500">{m.k}</div>
                    </div>
                  ))}
                </div>
              )}

              {Array.isArray(qcResult.critical_issues) && qcResult.critical_issues.length > 0 && (
                <div className="mb-3">
                  <div className="text-xs font-medium text-red-500 mb-1">关键缺陷</div>
                  <ul className="text-sm text-red-500 space-y-0.5 list-disc list-inside">
                    {qcResult.critical_issues.map((x: string, i: number) => <li key={i}>{x}</li>)}
                  </ul>
                </div>
              )}
              {Array.isArray(qcResult.issues) && qcResult.issues.length > 0 && (
                <div className="mb-3">
                  <div className="text-xs font-medium text-amber-500 mb-1">可优化项</div>
                  <ul className="text-sm text-amber-600 space-y-0.5 list-disc list-inside">
                    {qcResult.issues.map((x: string, i: number) => <li key={i}>{x}</li>)}
                  </ul>
                </div>
              )}

              {/* 频谱图 + 波形图（AI 层送检用的同一批图，顺序固定：先频谱后波形） */}
              {Array.isArray(qcResult.visuals) && qcResult.visuals.length > 0 && (
                <div className="space-y-2">
                  <div className="text-xs text-gray-500">
                    送检图（第 1 张频谱图、第 2 张波形图）
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    {qcResult.visuals.map((src: string, i: number) => (
                      <div key={i} className="bg-black/5 rounded p-1">
                        <img src={src} alt={i === 0 ? '频谱图' : '波形图'} className="w-full rounded" />
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {qcResult.file_url && (
                <audio controls src={qcResult.file_url} className="w-full mt-3" />
              )}
              <div className="mt-2 text-xs text-gray-400 break-all">对象：{qcResult.path}</div>
            </div>
          )}

          {/* 未通过的句子（来自最近一次配音任务：任务级质检结论） */}
          {ttsQc?.audio_qc?.enabled && (
            <div className="bg-white rounded-lg border border-gray-200 p-4">
              <h4 className="font-medium text-gray-800 mb-2">
                上次配音的逐句质检
                <span className="ml-2 text-xs font-normal text-gray-500">
                  通过 {ttsQc.audio_qc.passed}/{ttsQc.audio_qc.checked}
                  {ttsQc.audio_qc.retried ? `，重配 ${ttsQc.audio_qc.retried} 句` : ''}
                  {ttsQc.audio_qc.recovered ? `，恢复 ${ttsQc.audio_qc.recovered} 句` : ''}
                </span>
              </h4>
              {Array.isArray(ttsQc.audio_qc.problems) && ttsQc.audio_qc.problems.length > 0 ? (
                <ul className="space-y-2">
                  {ttsQc.audio_qc.problems.map((p: any, i: number) => (
                    <li key={i} className="text-sm p-2 rounded bg-red-500/5 border border-red-500/20">
                      <span className="font-mono text-xs text-gray-400">
                        #{p.shot_id ?? i + 1}
                      </span>
                      <span className="ml-2 text-gray-700">
                        {p.character ? `${p.character}：` : ''}{p.reason}
                      </span>
                      {p.visuals && p.visuals.length > 0 && (
                        <div className="mt-2 grid grid-cols-2 gap-2">
                          {p.visuals.map((src: string, k: number) => (
                            <img key={k} src={src} alt="质检图" className="w-full rounded bg-black/5" />
                          ))}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="text-sm text-green-600">逐句质检全部通过</div>
              )}
            </div>
          )}

          {/* 台词预检结论（结构化残留会被 TTS 念出来，这里先摊开给用户看） */}
          {ttsQc?.prompt_qc?.enabled && (
            <div className="bg-white rounded-lg border border-gray-200 p-4">
              <h4 className="font-medium text-gray-800 mb-2">
                台词预检
                <span className="ml-2 text-xs font-normal text-gray-500">
                  检查 {ttsQc.prompt_qc.checked} 句，已自愈 {ttsQc.prompt_qc.repaired_lines} 句
                  {ttsQc.prompt_qc.blocked ? `，${ttsQc.prompt_qc.blocked} 句致命` : ''}
                </span>
              </h4>
              {Array.isArray(ttsQc.prompt_qc.problem_lines) && ttsQc.prompt_qc.problem_lines.length > 0 ? (
                <ul className="space-y-2 text-sm">
                  {ttsQc.prompt_qc.problem_lines.map((p: any, i: number) => (
                    <li key={i} className="p-2 rounded bg-amber-500/5 border border-amber-500/20">
                      <span className="font-mono text-xs text-gray-400">#{p.shot_id ?? i + 1}</span>
                      {p.repairs && p.repairs.length > 0 && (
                        <span className="ml-2 text-green-600">
                          已自愈：{p.repairs.join('、')}
                        </span>
                      )}
                      {Array.isArray(p.issues) && p.issues.length > 0 && (
                        <div className="text-amber-600 mt-0.5">
                          {p.issues.join('；')}
                        </div>
                      )}
                      {p.rebuild_hint && (
                        <div className="text-xs text-gray-500 mt-0.5">建议：{p.rebuild_hint}</div>
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="text-sm text-green-600">台词预检全部通过</div>
              )}
            </div>
          )}
        </div>
      )}

      {/* 流程说明 */}
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
        <h4 className="font-medium text-blue-900 mb-2">💡 工作流程说明</h4>
        <ol className="list-decimal list-inside space-y-1 text-sm text-blue-800">
          <li>先生成 TTS 配音（每集台词合成音频）</li>
          <li>再进行音画混音（将配音与视频片段合成）</li>
          <li>最后进行音频质检（确认音质达标）</li>
        </ol>
      </div>
    </div>
  );
}
