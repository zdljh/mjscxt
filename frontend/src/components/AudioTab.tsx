import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { ttsApi, mixApi } from '@/api/client';
import { Button, Loading } from '@/components/ui';

interface AudioTabProps {
  projectKey: string;
}

export function AudioTab({ projectKey }: AudioTabProps) {
  const { t } = useApp();
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
  
  // Step 3: 音频质检
  const [qcResult, setQcResult] = useState<any>(null);
  const [qcLoading, setQcLoading] = useState(false);

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
  }, [projectKey]);

  // 生成配音
  const handleGenerateTTS = async () => {
    setTtsGenerating(true);
    setTtsError('');
    try {
      const result = await ttsApi.generate({ project_name: projectKey });
      alert(`配音生成任务已启动: ${result.task_id}`);
      await loadTtsPlan();
    } catch (e) {
      setTtsError(e instanceof Error ? e.message : '配音生成失败');
    } finally {
      setTtsGenerating(false);
    }
  };

  // 生成混音
  const handleGenerateMix = async () => {
    setMixGenerating(true);
    setMixError('');
    try {
      const result = await mixApi.generate({ project_name: projectKey });
      alert(`混音任务已启动: ${result.task_id}`);
      await loadMixPlan();
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
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-white/5 dark:text-gray-400 dark:hover:bg-white/10'
              }`}
            >
              <span className="text-lg">{step.icon}</span>
              <span>{step.label}</span>
              {activeStep === step.id && (
                <span className="ml-1 text-xs opacity-75">①②③[idx]</span>
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
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <span>🎙️</span> TTS 配音生成
            </h3>
            
            {ttsEnv && (
              <div className={`p-3 rounded-lg mb-4 ${
                ttsEnv.available 
                  ? 'bg-green-500/10 border border-green-500/30 text-green-700 dark:text-green-400' 
                  : 'bg-red-500/10 border border-red-500/30 text-red-700 dark:text-red-400'
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
                <h4 className="font-medium text-gray-700 dark:text-gray-300 mb-3">配音计划预览</h4>
                <div className="grid grid-cols-3 gap-4 mb-4">
                  <div className="bg-gray-50 dark:bg-gray-700/50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-indigo-500">
                      {ttsPlan.line_count ?? (ttsPlan.lines?.length ?? 0)}
                    </div>
                    <div className="text-sm text-gray-500">总台词数</div>
                  </div>
                  <div className="bg-gray-50 dark:bg-gray-700/50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-green-500">
                      {ttsPlan.characters?.length || 0}
                    </div>
                    <div className="text-sm text-gray-500">涉及角色</div>
                  </div>
                  <div className="bg-gray-50 dark:bg-gray-700/50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-blue-500">{ttsPlan.episode}</div>
                    <div className="text-sm text-gray-500">集数</div>
                  </div>
                </div>

                {ttsPlan.lines && ttsPlan.lines.length > 0 && (
                  <div className="max-h-40 overflow-y-auto space-y-1 mb-4">
                    {ttsPlan.lines.slice(0, 10).map((line: any, idx: number) => (
                      <div key={idx} className="text-sm text-gray-600 dark:text-gray-400 px-2 py-1 bg-gray-50 dark:bg-gray-700/30 rounded">
                        <span className="font-mono text-gray-400">#{line.seq}</span>
                        <span className="ml-2">{line.text}</span>
                        {line.character && <span className="ml-2 text-indigo-500">[{line.character}]</span>}
                      </div>
                    ))}
                    {ttsPlan.lines.length > 10 && (
                      <div className="text-xs text-gray-400 text-center py-1">... 还有 {ttsPlan.lines.length - 10} 条台词</div>
                    )}
                  </div>
                )}

                <Button
                  onClick={handleGenerateTTS}
                  disabled={ttsGenerating || !ttsEnv?.available}
                  style={{ background: 'linear-gradient(135deg, #10b981 0%, #059669 100%)' }}
                  className="w-full py-3"
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
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <span>🔊</span> 音画混音合成
            </h3>

            {mixPlan && (
              <div className="mb-4">
                <div className="grid grid-cols-3 gap-4 mb-4">
                  <div className="bg-gray-50 dark:bg-gray-700/50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-purple-500">{mixPlan.video_count ?? 0}</div>
                    <div className="text-sm text-gray-500">视频片段</div>
                  </div>
                  <div className="bg-gray-50 dark:bg-gray-700/50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-yellow-500">{mixPlan.audio_count ?? 0}</div>
                    <div className="text-sm text-gray-500">音频片段</div>
                  </div>
                  <div className="bg-gray-50 dark:bg-gray-700/50 rounded p-3 text-center">
                    <div className="text-2xl font-bold text-indigo-500">{mixPlan.to_generate ?? 0}</div>
                    <div className="text-sm text-gray-500">待合成</div>
                  </div>
                </div>

                {mixPlan.lines && mixPlan.lines.length > 0 && (
                  <div className="max-h-40 overflow-y-auto space-y-1 mb-4">
                    {mixPlan.lines.map((line: any, idx: number) => (
                      <div key={idx} className="text-sm text-gray-600 dark:text-gray-400 px-2 py-1 bg-gray-50 dark:bg-gray-700/30 rounded flex justify-between">
                        <span>{line.video}</span>
                        <span>+</span>
                        <span>{line.audio}</span>
                        <span>→</span>
                        <span className="text-green-600 dark:text-green-400">{line.output}</span>
                      </div>
                    ))}
                  </div>
                )}

                <Button
                  onClick={handleGenerateMix}
                  disabled={mixGenerating}
                  style={{ background: 'linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)' }}
                  className="w-full py-3"
                >
                  {mixGenerating ? '混音中...' : '开始混音'}
                </Button>
              </div>
            )}

            {mixError && (
              <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
                {mixError}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Step 3: 音频质检 */}
      {activeStep === 3 && (
        <div className="space-y-4">
          <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <span>✅</span> 音频质检
            </h3>
            
            <div className="text-center py-8 text-gray-500 dark:text-gray-400">
              <div className="text-4xl mb-3">📊</div>
              <p>音频质检功能正在开发中...</p>
              <p className="text-sm mt-2">将支持波形预览、时长校验、格式检测等功能</p>
            </div>
          </div>
        </div>
      )}

      {/* 流程说明 */}
      <div className="bg-blue-50 dark:bg-blue-500/10 border border-blue-200 dark:border-blue-500/30 rounded-lg p-4">
        <h4 className="font-medium text-blue-900 dark:text-blue-300 mb-2">💡 工作流程说明</h4>
        <ol className="list-decimal list-inside space-y-1 text-sm text-blue-800 dark:text-blue-400">
          <li>先生成 TTS 配音（每集台词合成音频）</li>
          <li>再进行音画混音（将配音与视频片段合成）</li>
          <li>最后进行音频质检（确认音质达标）</li>
        </ol>
      </div>
    </div>
  );
}
