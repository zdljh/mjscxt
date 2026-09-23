import React, { useState, useEffect, useCallback } from 'react';
import { useApp } from '@/context/AppContext';
import { exportApi, autopilotApi } from '@/api/client';
import { Button, Loading, Textarea } from '@/components/ui';
import { useToast } from '@/components/ui/toast';
import type { Deliverable } from '@/types';

interface AssetItem {
  name: string;
  url?: string;
  file?: string;
  size?: number;
  [key: string]: any;
}

interface OutputReviewTabProps {
  projectKey: string;
  assets: { final?: AssetItem[]; counts?: Record<string, number> } | null;
}

export function OutputReviewTab({ projectKey, assets }: OutputReviewTabProps) {
  const { t } = useApp();
  const [exportFiles, setExportFiles] = useState<any[]>([]);
  const [deliverables, setDeliverables] = useState<Deliverable[]>([]);
  const [pending, setPending] = useState(0);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState<number | null>(null);
  const [rejecting, setRejecting] = useState<number | null>(null);
  const [reason, setReason] = useState('');
  const [playing, setPlaying] = useState<string | null>(null);
  const toast = useToast();

  const loadAll = useCallback(async () => {
    if (!projectKey) return;
    setLoading(true);
    try {
      const [exportRes, deliverRes] = await Promise.all([
        exportApi.listFiles(projectKey),
        autopilotApi.deliverables(projectKey),
      ]);
      setExportFiles(exportRes?.files || []);
      setDeliverables(deliverRes?.items || []);
      setPending(deliverRes?.pending || 0);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, [projectKey]);

  useEffect(() => { void loadAll(); }, [loadAll]);

  const handleGenerate = async () => {
    setGenerating(true);
    setError('');
    setNotice('');
    try {
      const data = await exportApi.generate(projectKey, ['fcpml', 'edl', 'json']);
      setExportFiles(data.files || []);
      if (typeof data.shot_count === 'number' && data.shot_count === 0) {
        setNotice('导出文件已生成，但本项目还没有分镜／视频，导出内容为空');
      } else if (typeof data.shot_count === 'number') {
        setNotice(`导出文件已生成，共 ${data.shot_count} 个镜头（约 ${data.total_sec ?? 0} 秒）`);
      } else {
        setNotice('导出文件已生成');
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '生成失败');
    } finally {
      setGenerating(false);
    }
  };

  const review = async (episodeNo: number, verdict: 'accepted' | 'rejected', note = '') => {
    setBusy(episodeNo);
    setError('');
    try {
      await autopilotApi.reviewDeliverable({
        project: projectKey,
        episode_no: episodeNo,
        review: verdict,
        note,
      });
      toast.success(verdict === 'accepted' ? t('deliver.acceptedOk') : t('deliver.rejectedOk'));
      setRejecting(null);
      setReason('');
      await loadAll();
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('deliver.actionFailed');
      setError(msg);
      toast.error(msg);
    } finally {
      setBusy(null);
    }
  };

  const sizeText = (n?: number) => (n ? `${(n / 1048576).toFixed(1)} MB` : '—');

  const statusBadge = (d: Deliverable) => {
    if (d.review === 'accepted') {
      return { text: t('deliver.accepted'), cls: 'bg-success-subtle text-success-strong' };
    }
    if (d.review === 'rejected') {
      return { text: t('deliver.rejected'), cls: 'bg-danger-subtle text-danger-strong' };
    }
    return { text: t('deliver.pending'), cls: 'bg-warning-subtle text-warning-strong' };
  };

  const triggerDownload = (url: string, filename?: string) => {
    const a = document.createElement('a');
    a.href = url;
    if (filename) a.download = filename;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  const finals = assets?.final || [];

  if (loading) return <Loading />;

  return (
    <div className="space-y-6">
      {/* 页面标题 */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-ink-1">输出与验收</h3>
          <p className="text-sm text-ink-2 mt-0.5">
            导出工程文件 & 验收成片
          </p>
        </div>
        <Button size="sm" variant="secondary" onClick={loadAll} disabled={loading || busy !== null}>
          {t('common.refresh')}
        </Button>
      </div>

      {error && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-sm text-danger-strong">
          {error}
        </div>
      )}
      {/* 区域 1: 导出配置 */}
      <div className="bg-surface rounded-lg border border-line p-4">
        <h4 className="font-semibold text-ink-1 mb-3 flex items-center gap-2">
          <span>🎬</span> 导出配置
        </h4>
        
        <div className="flex gap-2 mb-4">
          <Button onClick={handleGenerate} disabled={generating}>
            {generating ? '生成中...' : '生成导出文件'}
          </Button>
        </div>

        {notice && (
          <div className="p-3 bg-success/10 border border-success/30 rounded-lg text-success-strong text-sm mb-4">
            {notice}
          </div>
        )}

        {/* 成片下载 */}
        <div className="mb-4">
          <h5 className="text-sm font-medium text-ink-1 mb-2">
            📁 成片清单 · {finals.length}
          </h5>
          {finals.length > 0 ? (
            <div className="space-y-2">
              {finals.map((item, idx) => (
                <div key={idx} className="flex items-center justify-between p-2 bg-surface-2 rounded">
                  <div className="min-w-0">
                    <p className="font-medium text-ink-1 truncate text-sm">{item.name}</p>
                    {item.size && <p className="text-xs text-ink-2">{sizeText(item.size)}</p>}
                  </div>
                  <div className="flex gap-2 shrink-0">
                    <Button size="sm" variant="secondary" onClick={() => window.open(item.url, '_blank')}>
                      预览
                    </Button>
                    <Button size="sm" onClick={() => triggerDownload(`${item.url}?download=1`, item.name)}>
                      下载
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-ink-2 py-2">暂无成片，请先完成视频生成</p>
          )}
        </div>

        {/* 剪辑工程文件 */}
        <div>
          <h5 className="text-sm font-medium text-ink-1 mb-2">
            🎞️ 工程文件 · {exportFiles.filter(f => f.exists).length}
          </h5>
          {exportFiles.filter(f => f.exists).length > 0 ? (
            <div className="space-y-2">
              {exportFiles.filter(f => f.exists).map((file: any, idx: number) => (
                <div key={idx} className="flex items-center justify-between p-2 bg-surface-2 rounded">
                  <div className="min-w-0">
                    <p className="font-medium text-ink-1 truncate text-sm">{file.filename}</p>
                    <p className="text-xs text-ink-2">{String(file.format || '').toUpperCase()}</p>
                  </div>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => triggerDownload(`/api/export/${encodeURIComponent(projectKey)}/${file.format}`, file.filename)}
                  >
                    下载
                  </Button>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-ink-2 py-2">暂无导出文件</p>
          )}
        </div>
      </div>

      {/* 区域 2: 成片验收 */}
      <div className="bg-surface rounded-lg border border-line p-4">
        <h4 className="font-semibold text-ink-1 mb-3 flex items-center gap-2">
          <span>📦</span> 成片验收
          <span className="ml-auto text-xs font-normal text-ink-2">
            待验收: {pending} / 共 {deliverables.length} 集
          </span>
        </h4>

        {deliverables.length === 0 ? (
          <div className="text-center py-8">
            <div className="text-4xl mb-3">📦</div>
            <p className="text-sm text-ink-2">暂无成片，请先运行自动生产</p>
            <p className="text-xs text-brand mt-2">
              请通过右侧「AI总控」下达生产指令，AI会先与您沟通生产风格
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {deliverables.map((d) => {
              const b = statusBadge(d);
              const canReview = busy === null;
              const playable = !!d.url && d.exists !== false;
              return (
                <div
                  key={`${d.project}-${d.episode_no}`}
                  className="border border-line rounded-lg p-3"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-semibold text-ink-1">
                          第 {d.episode_no} 集
                        </span>
                        {d.meta?.title && (
                          <span className="text-sm text-ink-2">{d.meta.title}</span>
                        )}
                        <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${b.cls}`}>{b.text}</span>
                        {d.exists === false && (
                          <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-danger-subtle text-danger-strong">
                            文件缺失
                          </span>
                        )}
                        {d.meta?.stale && (
                          <span
                            className="px-2 py-0.5 rounded-full text-xs font-medium bg-warning-subtle text-warning-strong"
                            title={d.meta.stale.reason || '成片已过期'}
                          >
                            成片已过期
                          </span>
                        )}
                        {d.meta?.incomplete_shots && (
                          <span
                            className="px-2 py-0.5 rounded-full text-xs font-medium bg-warning-subtle text-warning-strong"
                            title={d.meta?.warning || '镜头数不齐，成片可能不完整'}
                          >
                            可能不完整 {d.meta?.shots_ready ?? '?'}/{d.meta?.shots_total ?? '?'}
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-ink-2 mt-1">
                        {d.filename} · {sizeText(d.size)}
                        {typeof d.meta?.duration_sec === 'number' && d.meta.duration_sec > 0 && (
                          <span> · {d.meta.duration_sec.toFixed(1)}s</span>
                        )}
                      </p>
                      {d.meta?.stale && (
                        <p className="text-xs text-warning-strong mt-1">
                          {d.meta.stale.reason || '同集镜头已重做'}
                          {d.meta.stale.detail?.shot_id != null && (
                            <span>（涉及镜头 #{d.meta.stale.detail.shot_id}）</span>
                          )}
                          ，请重新混音合成后再验收
                        </p>
                      )}
                      {d.meta?.incomplete_shots && d.meta?.warning && (
                        <p className="text-xs text-warning-strong mt-1">
                          {d.meta.warning}
                        </p>
                      )}
                      {d.review === 'rejected' && d.review_note && (
                        <p className="text-xs text-ink-2 mt-1">
                          打回原因: {d.review_note}
                        </p>
                      )}
                    </div>

                    <div className="flex gap-2 shrink-0">
                      {playable && (
                        <>
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() => setPlaying(playing === d.filename ? null : d.filename)}
                          >
                            {playing === d.filename ? '关闭' : '播放'}
                          </Button>
                          <a
                            href={`${d.url}?download=1`}
                            className="inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-line-strong text-ink-1 hover:bg-surface-2 transition-colors"
                          >
                            下载
                          </a>
                        </>
                      )}
                      <Button
                        size="sm"
                        onClick={() => review(d.episode_no, 'accepted')}
                        disabled={!canReview || d.review === 'accepted'}
                      >
                        通过
                      </Button>
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => {
                          setRejecting(rejecting === d.episode_no ? null : d.episode_no);
                          setReason('');
                        }}
                        disabled={!canReview || d.review === 'rejected'}
                      >
                        打回
                      </Button>
                    </div>
                  </div>

                  {playing === d.filename && d.url && (
                    <video src={d.url} controls className="w-full mt-3 rounded-lg bg-black" />
                  )}

                  {rejecting === d.episode_no && (
                    <div className="mt-3 pt-3 border-t border-line space-y-2">
                      <Textarea
                        value={reason}
                        onChange={setReason}
                        label="打回原因"
                        rows={2}
                        placeholder="请输入打回原因..."
                      />
                      <div className="flex gap-2">
                        <Button size="sm" onClick={() => review(d.episode_no, 'rejected', reason)} disabled={!canReview}>
                          确认打回
                        </Button>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => {
                            setRejecting(null);
                            setReason('');
                          }}
                        >
                          取消
                        </Button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
