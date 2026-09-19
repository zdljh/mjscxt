// ============================================
// API 客户端（TypeScript）
// ============================================
import type {
  Project, ProjectsResponse,
  Novel, NovelsResponse,
  Task, TasksResponse,
  Character, Relation,
  Memory, MemoryStats, PromptLesson,
  AppSettings, I18nData,
  AnalyticsData,
  KeyframePlanResponse,
  StoryboardCanvasResponse,
  TTSEnv, TTSPlanResponse, TTSTask,
  MixEnv, MixPlanResponse, MixTask, MixStatusResponse,
  QCConfig, QCResponse,
  Episode, EpisodeListResponse,
  AutopilotStatus, AutopilotProgress,
  Deliverable, DeliverablesResponse,
  Provider, ProvidersResponse,
  AIConfigResponse, AITestResult,
  UpscaleEnv, UpscaleSource, UpscaleSubmitResponse, UpscaleTask, UpscaleArtifact,
  AgentJob, AgentTool, AgentGuards, AgentKillState,
} from '../types';

const API_BASE = '/api';

// 后端统一返回 {success, error?, message?}。此前 request() 直接抛
// `HTTP 500: Internal Server Error`，把后端精心脱敏过的中文错误丢掉了，
// 界面上只能看到无信息的英文报错。这里优先取后端的可读文案。
async function readError(response: Response): Promise<string> {
  let detail = '';
  try {
    const data = await response.clone().json();
    const raw = data?.error || data?.message || data?.detail;
    if (typeof raw === 'string' && raw.trim()) detail = raw.trim();
    else if (raw) detail = JSON.stringify(raw);
    // 后端常带 `hint`（例如「这集可能是兜底生成的，没有台词」）或 `guide`
    // （例如视觉模型不适配的替代建议）。这些是给用户看的处置办法，
    // 只把 error 抛出去会让用户看到问题却不知道怎么办。
    const extra = data?.hint || data?.guide;
    if (typeof extra === 'string' && extra.trim()) {
      detail = detail ? `${detail}（${extra.trim()}）` : extra.trim();
    }
  } catch {
    try {
      const text = (await response.text()).trim();
      if (text) detail = text;
    } catch {
      /* 响应体不可读，退化为状态码 */
    }
  }
  const status = `HTTP ${response.status}`;
  return detail ? `${detail}` : `${status} ${response.statusText || ''}`.trim();
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return response.json() as Promise<T>;
}

// --- Projects ---
export const projectsApi = {
  list: () => request<ProjectsResponse>('/projects'),
  get: (id: string) => request<{ success: boolean; project: Project }>(`/projects/${id}`).then(d => d.project),
  // 后端实际返回 {success, project, created}，此前类型写成 Project 会误导调用方
  create: (data: Partial<Project>) => request<{ success: boolean; project: Project; created?: boolean }>('/projects', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  delete: (id: string) => request<void>(`/projects/${id}`, { method: 'DELETE' }),
  deleteV2: (id: string, confirm?: boolean) =>
    request<{ success: boolean }>(`/projects/${id}/delete`, {
      method: 'POST',
      body: JSON.stringify({ confirm: confirm ?? true }),
    }),
  rename: (id: string, name: string) =>
    request<{ success: boolean; project: Project; note?: string }>(`/projects/${id}/rename`, {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
};

// --- Novels ---
export interface NovelUploadItem {
  filename: string;
  success: boolean;
  error?: string;
  novel?: Novel & { title?: string };
  project_id?: string;
  project_key?: string;
}
export interface NovelUploadResponse {
  success: boolean;
  uploaded: number;
  failed: number;
  results: NovelUploadItem[];
  supported_exts: string[];
}

export const novelsApi = {
  list: (projectId?: string) => {
    // 后端只认 project_id / project_name；原来的 ?project= 会被静默忽略，
    // 导致「按项目筛选小说」实际返回全量。
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    return request<NovelsResponse>(`/novels${query}`);
  },
  /** 上传小说文件（后端路由是 /novels/upload，且要求 multipart 的 file 字段） */
  upload: async (
    file: File,
    opts: { projectId?: string; autoProject?: boolean } = {}
  ): Promise<NovelUploadResponse> => {
    const formData = new FormData();
    formData.append('file', file);
    if (opts.projectId) formData.append('project_id', opts.projectId);
    if (opts.autoProject === false) formData.append('auto_project', '0');
    const response = await fetch(`${API_BASE}/novels/upload`, {
      method: 'POST',
      body: formData,
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    return response.json() as Promise<NovelUploadResponse>;
  },
};

// --- Tasks ---
export const tasksApi = {
  list: () => request<TasksResponse>('/tasks'),
  get: (id: string) => request<Task>(`/tasks/${id}`),
};

// --- Characters ---
/**
 * 后端资产接口的返回形态并不统一：
 *  - /api/characters → { success, characters: { "<id>": {...} } }   （**字典**）
 *  - /api/items      → { success, items: { ... } } / { items: [ ... ] }
 *  - /api/scenes     → 同上
 * 早先 client.ts 把 list() 的返回类型直接声明成 Character[]，与运行时不符，
 * 调用方只能自己 `Object.values(res.characters)`。一旦后端少给一层字段，
 * `Object.values(undefined)` 就会抛错并导致整页白屏（测试报告 #1）。
 * 这里在 API 层统一归一化成数组，且对任何异常形态都退化为空数组，不再抛错。
 */
function toAssetArray<T>(payload: unknown, ...keys: string[]): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (!payload || typeof payload !== 'object') return [];
  const obj = payload as Record<string, unknown>;
  let inner: unknown = obj;
  for (const k of keys) {
    if (obj[k] !== undefined) { inner = obj[k]; break; }
  }
  if (Array.isArray(inner)) return inner as T[];
  if (inner && typeof inner === 'object') return Object.values(inner) as T[];
  return [];
}

export const charactersApi = {
  /** 后端返回字典，这里统一解包成数组（见 toAssetArray 说明） */
  list: async (projectId: string): Promise<Character[]> =>
    toAssetArray<Character>(
      await request<unknown>(`/characters?project=${encodeURIComponent(projectId)}`),
      'characters'
    ),
  add: (data: Partial<Character>) =>
    request<Character>('/characters', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: Partial<Character>) =>
    request<Character>(`/characters/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/characters/${id}`, { method: 'DELETE' }),
};

// --- Relations ---
export const relationsApi = {
  list: (projectId: string) =>
    request<Relation[]>(`/relations?project=${encodeURIComponent(projectId)}`),
  graph: (projectId: string) =>
    request<Record<string, unknown>>(
      `/relations/graph?project=${encodeURIComponent(projectId)}`
    ),
  add: (data: Partial<Relation>) =>
    request<Relation>('/relations', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: Partial<Relation>) =>
    request<Relation>(`/relations/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/relations/${id}`, { method: 'DELETE' }),
};

// --- AI Chat ---
export const chatApi = {
  send: (message: string, project?: string) =>
    request<{ success: boolean; reply: string; settings?: Record<string, unknown> }>(
      '/ai/chat',
      { method: 'POST', body: JSON.stringify({ message, project_name: project }) }
    ),
  history: (project?: string) =>
    request<{ messages: Array<{ role: string; content: string; timestamp: string }> }>(
      `/ai/chat/history${project ? `?project=${encodeURIComponent(project)}` : ''}`
    ),
  clearHistory: (project?: string) =>
    request<void>('/ai/chat/clear', {
      method: 'POST',
      body: JSON.stringify({ project: project || '' }),
    }),
  applySettings: () =>
    request<{ success: boolean }>('/ai/chat/apply', { method: 'POST' }),
};

// --- Autonomous ---
export const autonomousApi = {
  status: () => request<{ running: boolean; project?: string; message?: string }>('/autonomous/status'),
  start: (projectName: string, novelId: string, overrides?: Record<string, unknown>) =>
    request<{ success: boolean; message?: string }>('/autonomous/start', {
      method: 'POST',
      body: JSON.stringify({ project_name: projectName, novel_id: novelId, ...(overrides || {}) }),
    }),
  stop: () => request<{ success: boolean }>('/autonomous/stop', { method: 'POST' }),
  resume: () => request<{ success: boolean }>('/autonomous/resume', { method: 'POST' }),
  chat: (message: string) =>
    request<{ success: boolean; reply: string }>('/autonomous/chat', {
      method: 'POST',
      body: JSON.stringify({ message }),
    }),
};

// --- 总控 AI 自主执行（function-calling agent） ---
//
// 与 chatApi 的区别：chatApi 只聊天 + 抽取创作设定，不执行任何动作；
// agentApi 会把指令交给模型，由模型自己决定调哪些工具、直接把活干完。
// 安全靠后端护栏（工具白名单 / 昂贵动作配额 / 冷却 / 急停），不需要人工确认。
export const agentApi = {
  send: (message: string, project?: string) =>
    request<{ success: boolean; job_id: string; project: string }>('/agent/chat', {
      method: 'POST',
      // 后端 _chat_project 只认 project_name，这里必须用它，否则会落到「上一个活跃项目」
      body: JSON.stringify({ message, project_name: project || '' }),
    }),
  job: (jobId: string) => request<{ success: boolean } & AgentJob>(`/agent/job/${jobId}`),
  tools: () =>
    request<{
      success: boolean;
      count: number;
      tools: AgentTool[];
      guards: AgentGuards;
      kill: AgentKillState;
    }>('/agent/tools'),
  setKill: (on: boolean, reason?: string) =>
    request<{ success: boolean; kill: AgentKillState }>('/agent/kill', {
      method: 'POST',
      body: JSON.stringify({ on, reason: reason || '' }),
    }),
  log: (limit = 100) =>
    request<{ success: boolean; count: number; items: Record<string, unknown>[] }>(
      `/agent/log?limit=${limit}`
    ),
};

// --- Memory ---
//
// 注意：后端 /api/memory/list 返回的是信封对象 { success, memories: [...], total }，
// /api/memory/stats 返回 { success, stats: { total, by_type: {...} }, ... }。
// 这里必须在这一层拆封，否则页面拿到的是对象而非数组（列表渲染会崩），
// 统计卡片也会因为读不到 total/lessons 而全部显示为空——这正是导航恢复后
// 记忆页「数字全空 + 列表崩溃」的根因。
export const memoryApi = {
  stats: async (): Promise<MemoryStats> => {
    const d = await request<{ success?: boolean; stats?: Record<string, unknown> }>('/memory/stats');
    const s = (d?.stats || {}) as Record<string, unknown>;
    const by = (s.by_type || {}) as Record<string, number>;
    return {
      total: Number(s.total ?? 0),
      lessons: Number(by.lesson ?? 0),
      successes: Number(by.success ?? 0),
      insights: Number(by.insight ?? 0),
      promptLessons: Number(s.prompt_lessons ?? 0),
    };
  },
  /** 质检教训库（生成链路自动学习成果；与手动记忆是两套数据） */
  lessons: async (params?: { kind?: string; limit?: number }): Promise<PromptLesson[]> => {
    const qs = new URLSearchParams();
    if (params?.kind) qs.set('kind', params.kind);
    if (params?.limit) qs.set('limit', String(params.limit));
    const q = qs.toString() ? `?${qs.toString()}` : '';
    const d = await request<{ lessons?: PromptLesson[] }>(`/memory/lessons${q}`);
    return d?.lessons || [];
  },
  /** 试算：给定提示词会召回哪些历史修正建议（把「自动学习」变得可见可验证） */
  recall: async (params: { kind: string; prompt: string; project?: string; style?: string }) => {
    const qs = new URLSearchParams({ kind: params.kind, prompt: params.prompt });
    if (params.project) qs.set('project', params.project);
    if (params.style) qs.set('style', params.style);
    return request<{
      success?: boolean; hints?: string[]; learned_prompt?: string; changed?: boolean;
    }>(`/memory/lessons/search?${qs.toString()}`);
  },
  list: async (params?: { query?: string; type?: string }): Promise<Memory[]> => {
    const qs = new URLSearchParams();
    if (params?.query) qs.set('query', params.query);
    if (params?.type) qs.set('type', params.type);
    const query = qs.toString() ? `?${qs.toString()}` : '';
    const d = await request<{ memories?: Memory[] } | Memory[]>(`/memory/list${query}`);
    if (Array.isArray(d)) return d;
    return d?.memories || [];
  },
  record: (data: Partial<Memory>) =>
    request<Memory>('/memory/record', { method: 'POST', body: JSON.stringify(data) }),
  recordLesson: (lesson: string, tags: string[]) =>
    request<Memory>('/memory/record-lesson', {
      method: 'POST',
      body: JSON.stringify({ lesson, tags }),
    }),
  recordSuccess: (content: string, tags: string[]) =>
    request<Memory>('/memory/record-success', {
      method: 'POST',
      body: JSON.stringify({ content, tags }),
    }),
  clearOld: (days: number = 90) =>
    request<void>('/memory/clear-old', {
      method: 'POST',
      body: JSON.stringify({ days }),
    }),
  insights: () => request<Record<string, unknown>>('/memory/insights'),
};

// --- i18n ---
export const i18nApi = {
  get: (lang: string) => request<I18nData>(`/i18n/${lang}`),
};

// --- Analytics ---
export const analyticsApi = {
  // 后端没有 /api/analytics，聚合数据在 /api/analytics/summary
  get: () => request<AnalyticsData>('/analytics/summary'),
  // 手工登记一条耗时事件（字段与后端 api_analytics_record 对齐）
  record: (data: {
    kind?: string;
    project?: string;
    label?: string;
    duration_sec?: number;
    units?: number;
    success?: boolean;
    meta?: Record<string, unknown>;
  }) =>
    request<{ success: boolean }>('/analytics/event', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
};

// --- Keyframes ---
export const keyframesApi = {
  plan: (project: string, episode?: number) =>
    request<KeyframePlanResponse>(
      `/keyframes/plan?project_name=${encodeURIComponent(project)}${episode ? `&episode_no=${episode}` : ''}`
    ),
  generate: (data: { project_name: string; episode_no?: number; shots?: any[]; only_missing?: boolean }) =>
    request<{ success: boolean; task_id: string; total: number }>(
      '/keyframes/generate',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  list: (project: string, episode?: number) =>
    request<any>(
      `/keyframes/list/${encodeURIComponent(project)}${episode ? `?episode_no=${episode}` : ''}`
    ),
  file: (filename: string) => `${API_BASE}/keyframes/file/${filename}`,
};

// --- Storyboard ---
export const storyboardApi = {
  canvas: (project: string, episode?: number) =>
    request<StoryboardCanvasResponse>(
      `/storyboard/canvas/${encodeURIComponent(project)}${episode ? `?episode_no=${episode}` : ''}`
    ),
  reorder: (data: { project_name: string; episode_no?: number; order: string[] }) =>
    request<{ success: boolean; shot_order: string[] }>(
      '/storyboard/shot/reorder',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  retryShot: (data: { project_name: string; shot_id: string; episode_no?: number }) =>
    request<{ success: boolean }>(
      '/storyboard/retry-shot',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  file: (project: string, filename: string) =>
    `${API_BASE}/storyboards/file/${encodeURIComponent(project)}/${filename}`,
  generateNineGrid: (project: string, scene_description: string) =>
    request<{
      success: boolean;
      grid_id: string;
      project: string;
      scene_description: string;
      created_at?: string;
      filepath: string;
      shots: any[];
    }>(
      '/storyboard/nine-grid',
      { method: 'POST', body: JSON.stringify({ project, scene_description }) }
    ),
  selectNineGridShot: (grid_id: string, project: string, selected_index: number) =>
    request<{ success: boolean; shot: any }>(
      `/storyboard/nine-grid/${encodeURIComponent(grid_id)}/select`,
      { method: 'POST', body: JSON.stringify({ project, selected_index }) }
    ),
};

// --- Video（单镜视频） ---
// 接口：POST /api/video/retry-shot（同步，直到 ComfyUI 出片才返回）
// 之前后端早已可用，但前端零引用 —— 用户对某一镜不满意时无法只重做这一镜。
// mode: reference（分镜图+主角锚点，默认）/ keyframe（首尾帧插值，需已生成尾帧）
export const videoApi = {
  retryShot: (data: {
    project_name: string;
    shot_id: string | number;
    episode_no?: number;
    mode?: 'reference' | 'keyframe';
    seed?: number;
    timeout?: number;
  }) =>
    request<{
      success: boolean;
      project: string;
      shot_id: string | number;
      seq: number;
      mode: string;
      path: string;
      /** 可直接播放/下载：/api/videos/<项目>/[epNN/]shot_NN.mp4 */
      url: string;
      ref_count: number;
      duration: number;
      /** 后端已把同集旧成片标记为「需重新合成」 */
      deliverable_marked_stale?: boolean;
    }>('/video/retry-shot', { method: 'POST', body: JSON.stringify(data) }),
};

// --- TTS ---
export const ttsApi = {
  env: (project?: string) =>
    request<TTSEnv>(`/tts/env${project ? `?project_name=${encodeURIComponent(project)}` : ''}`),
  plan: (data: { project_name: string; episode?: number }) =>
    request<TTSPlanResponse>('/tts/plan', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  voiceMap: (project: string, voiceMap: Record<string, unknown>) =>
    request<{ success: boolean }>('/tts/voice-map', {
      method: 'POST',
      body: JSON.stringify({ project_name: project, voice_map: voiceMap }),
    }),
  preview: (data: { project_name: string; text: string; character?: string }) =>
    request<{ success: boolean; url: string }>(
      '/tts/preview',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  generate: (data: { project_name: string; episode?: number }) =>
    request<{ success: boolean; task_id: string; line_count: number }>(
      '/tts/generate',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  status: (taskId: string) => request<TTSTask>(`/tts/status/${taskId}`),
  tasks: () => request<{ success: boolean; items: TTSTask[] }>('/tts/tasks'),
  list: (project: string) =>
    request<{ success: boolean; lines: any[]; merged: any[] }>(
      `/tts/list?project_name=${encodeURIComponent(project)}`
    ),
  file: (project: string, filename: string) =>
    `${API_BASE}/tts/file/${encodeURIComponent(project)}/${filename}`,
};

// --- Mix ---
export const mixApi = {
  env: () => request<MixEnv>('/mix/env'),
  plan: (data: { project_name: string; episode?: number }) =>
    request<MixPlanResponse>('/mix/plan', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  generate: (data: { project_name: string; episode?: number }) =>
    request<{ success: boolean; task_id: string }>(
      '/mix/generate',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  status: (taskId: string) => request<MixStatusResponse>(`/mix/status/${taskId}`),
  tasks: () => request<{ success: boolean; items: MixTask[] }>('/mix/tasks'),
  list: (project: string) =>
    request<{ success: boolean; items: any[] }>(
      `/mix/list?project_name=${encodeURIComponent(project)}`
    ),
  file: (project: string, filename: string) =>
    `${API_BASE}/mix/file/${encodeURIComponent(project)}/${filename}`,
};

// --- QC ---
/** 音频质检结结论（两层：客观层 ffmpeg 指标 + AI 层频谱/波形送检） */
export interface QCAudioResult {
  success: boolean;
  project_name?: string;
  /** 检验对象来源：mix=带配音成片 / merged=整集音轨 / line=单句 / path=显式路径 */
  source?: string;
  path?: string;
  passed: boolean | null;
  blocked: boolean;
  score: number | null;
  reason?: string;
  issues: string[];
  critical_issues: string[];
  metrics: {
    duration?: number;
    mean_db?: number | null;
    max_db?: number | null;
    silence_sec?: number;
    speech_ratio?: number | null;
    codec?: string;
    sample_rate?: number;
    channels?: number;
    size_bytes?: number;
  };
  /** AI 层是否真的参与了判定 */
  ai_used?: boolean;
  ai_skipped?: boolean;
  ai_skip_reason?: string;
  objective_only?: boolean;
  /** 频谱图 / 波形图（顺序固定：先频谱后波形），可直接作为 img src */
  visuals?: string[];
  expect_sec?: number | null;
  check_speech_ratio?: boolean;
  audio_qc_active?: boolean;
  audio_ai_active?: boolean;
  /** 原文件的可播放地址（成品可直接试听） */
  file_url?: string;
  error?: string;
}

export const qcApi = {
  config: () => request<QCResponse>('/qc/config'),
  updateConfig: (config: QCConfig) =>
    request<{ success: boolean }>('/qc/config', {
      method: 'POST',
      body: JSON.stringify({ config }),
    }),
  clearConfig: () => request<{ success: boolean }>('/qc/config/clear', { method: 'POST' }),
  resetEndpoint: () => request<{ success: boolean }>('/qc/config/reset-endpoint', { method: 'POST' }),
  syncFromAI: () => request<{ success: boolean }>('/qc/config/sync-from-ai', { method: 'POST' }),
  test: (data: { project: string; shot_id: string }) =>
    request<{ success: boolean; verdict: string; score: number }>(
      '/qc/test',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  history: (project: string) =>
    request<QCResponse>(`/qc/project-summary?project=${encodeURIComponent(project)}`),
  frames: (project: string) =>
    request<{ success: boolean; frames: any[] }>(
      `/qc/frames/${encodeURIComponent(project)}`
    ),
  /** 音频成品质检：客观层（ffmpeg 指标）始终执行；AI 层需配置质检接口 */
  checkAudio: (data: {
    project_name?: string;
    /** 显式指定产物文件（必须在 output/ 内） */
    path?: string;
    /** 未给 path 时按此推导：mix > merged > line */
    source?: 'mix' | 'merged' | 'line';
    expect_sec?: number;
    line_text?: string;
    /** 有声占比下限判定。单句传 true；整集/成片必须 false（天然有留白） */
    check_speech_ratio?: boolean;
    /** false=只跑客观层（毫秒级、零模型调用） */
    with_ai?: boolean;
  }) => request<QCAudioResult>('/qc/audio', { method: 'POST', body: JSON.stringify(data) }),
  /** 提示词预检（生成前质检）：确定性检查 + 自愈。kind 支持 storyboard/h3/asset/keyframe/audio */
  promptCheck: (data: {
    kind: string;
    prompt: string;
    style?: string;
    context?: Record<string, unknown>;
    ref_count?: number;
    expect_refs?: boolean;
    repair?: boolean;
  }) => request<any>('/qc/prompt', { method: 'POST', body: JSON.stringify(data) }),
};

// --- Episodes ---
export const episodesApi = {
  list: (novelId: string) =>
    request<EpisodeListResponse>(`/episodes/${encodeURIComponent(novelId)}`),
  get: (novelId: string, episodeNo: number) =>
    request<Episode>(`/episodes/${encodeURIComponent(novelId)}/${episodeNo}`),
  generate: (novelId: string) =>
    request<{ success: boolean; episode_count: number }>(
      `/novels/${encodeURIComponent(novelId)}/episodes/generate`,
      { method: 'POST' }
    ),
};

// --- Continuity ---
export const continuityApi = {
  get: (novelId: string) =>
    request<any>(`/continuity/${encodeURIComponent(novelId)}`),
  getByEpisode: (novelId: string, episodeNo: number) =>
    request<any>(`/continuity/${encodeURIComponent(novelId)}/${episodeNo}`),
  revalidate: (novelId: string, episodeNo: number) =>
    request<{ success: boolean }>(
      `/continuity/${encodeURIComponent(novelId)}/${episodeNo}/revalidate`,
      { method: 'POST' }
    ),
};

// --- Coverage ---
export const coverageApi = {
  get: (novelId: string) =>
    request<any>(`/coverage/${encodeURIComponent(novelId)}`),
  getByEpisode: (novelId: string, episodeNo: number) =>
    request<any>(`/coverage/${encodeURIComponent(novelId)}/${episodeNo}`),
};

// --- Autopilot ---
export const autopilotApi = {
  status: () => request<AutopilotStatus>('/autopilot/status'),
  ready: () => request<{ ready: boolean }>('/autopilot/ready'),
  curve: () => request<any>('/autopilot/curve'),
  plans: () => request<{ success: boolean; plans: any[] }>('/autopilot/plans'),
  plan: (project: string) =>
    request<any>(`/autopilot/plan/${encodeURIComponent(project)}`),
  /**
   * 更新某项目的自动生产计划。
   *
   * ⚠️ 后端只接受 `autopilot.PLAN_DEFAULTS` 里声明过的字段
   * （app.py `api_autopilot_plan_set` 用 `k in PLAN_DEFAULTS` 过滤），
   * 传入未声明的键会被静默丢弃、甚至整体报「没有可更新字段」。
   * 因此新增可配置项时必须同时加进 PLAN_DEFAULTS。
   */
  setPlan: (project: string, patch: Record<string, unknown>) =>
    request<{ success: boolean; project: string; plan: Record<string, unknown> }>(
      `/autopilot/plan/${encodeURIComponent(project)}`,
      { method: 'POST', body: JSON.stringify(patch) }
    ),
  enable: () => request<{ success: boolean }>('/autopilot/enable', { method: 'POST' }),
  disable: () => request<{ success: boolean }>('/autopilot/disable', { method: 'POST' }),
  pause: () => request<{ success: boolean }>('/autopilot/pause', { method: 'POST' }),
  resume: () => request<{ success: boolean }>('/autopilot/resume', { method: 'POST' }),
  progress: () => request<{ success: boolean; projects: AutopilotProgress[] }>('/autopilot/progress'),
  progressByProject: (project: string) =>
    request<AutopilotProgress>(`/autopilot/progress/${encodeURIComponent(project)}`),
  /** 成片清单。传 project 只取该项目的（工作台用），不传则取全部项目。 */
  deliverables: (project?: string) =>
    request<DeliverablesResponse>(
      project ? `/autopilot/deliverables?project=${encodeURIComponent(project)}` : '/autopilot/deliverables'
    ),
  /**
   * 验收 / 打回成片。打回会在下次托管轮转时自动重跑该集。
   *
   * ⚠️ 字段名必须与后端一致（app.py `api_autopilot_review`）：
   *   project / episode_no(int) / review('accepted'|'rejected'|'pending') / note?
   * 这里曾经误写成 { project, deliverable, verdict } —— 接口一直是 400，
   * 即「验收/打回」功能从未真正生效过。
   */
  reviewDeliverable: (data: {
    project: string;
    episode_no: number;
    review: 'accepted' | 'rejected' | 'pending';
    note?: string;
  }) =>
    request<{ success: boolean; item: Deliverable }>('/autopilot/deliverables/review', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  exceptions: () =>
    request<{ success: boolean; exceptions: any[] }>('/autopilot/exceptions'),
  resolveException: (data: { project: string; exception_id: string; action: string }) =>
    request<{ success: boolean }>('/autopilot/exceptions/resolve', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  runOnce: (data: { project_name: string }) =>
    request<{ success: boolean; task_id: string }>(
      '/autopilot/run-once',
      { method: 'POST', body: JSON.stringify(data) }
    ),
  planFromSettings: (project: string) =>
    request<{ success: boolean; plan: any }>(
      `/autopilot/plan-from-settings/${encodeURIComponent(project)}`,
      { method: 'POST' }
    ),
};

// --- Providers ---
export const providersApi = {
  list: () => request<ProvidersResponse>('/providers'),
  select: (providerId: string) =>
    request<{ success: boolean }>('/providers/select', {
      method: 'POST',
      body: JSON.stringify({ provider_id: providerId }),
    }),
};

// --- Generation ---
export const generationApi = {
  status: (taskId: string) =>
    request<{ success: boolean; task: any }>(`/generation/status/${taskId}`),
};

// --- Export ---
export interface ExportedFile {
  format: string;
  filename: string;
  exists: boolean;
  path?: string;
  dir?: string;
  project?: string;
  exported_at?: string | null;
  shot_count?: number | null;
  total_sec?: number | null;
  size_mb?: number | null;
}
export const exportApi = {
  generate: (projectName: string, formats: string[]) =>
    request<{
      success: boolean;
      files: ExportedFile[];
      /** 后端据剧本实际构建出的镜头数，0 表示导出内容为空 */
      shot_count?: number;
      total_sec?: number;
    }>(
      `/export/${encodeURIComponent(projectName)}`,
      { method: 'POST', body: JSON.stringify({ formats }) }
    ),
  listFiles: (projectName: string) =>
    request<{ success: boolean; files: ExportedFile[]; items?: any[] }>(
      `/export/list?project=${encodeURIComponent(projectName)}`
    ),
};

// --- AI Config (Unified: text / qc / chat) ---
// 注意：这里的路径不要再写 '/api' 前缀——request() 已经统一加了 API_BASE('/api')，
// 之前写成 '/api/ai/config' 实际会请求 /api/api/ai/config → 404，AI 配置页整体不可用。
export const aiConfigApi = {
  get: () => request<AIConfigResponse>('/ai/config'),
  /**
   * 保存单个模块。
   * reasoning_effort = 思考档位（'' | 'low' | 'high' | 'max'），只对「思考不可关闭」的模型
   * （如 GLM-5.3-Flash）有意义：留空 = 不注入该参数，由服务端取默认档。
   * 用 undefined 表示「不改动」，用空串表示「清空」——两者语义不同，别合并。
   */
  save: (module: string, base_url: string, model: string, api_key?: string, reasoning_effort?: string) =>
    request<{ success: boolean; module_config: any; config: AIConfigResponse['config']; message: string }>(
      '/ai/config',
      { method: 'POST', body: JSON.stringify({ module, base_url, model, api_key, reasoning_effort }) }
    ),
  clear: (module?: string) =>
    request<{ success: boolean; config: AIConfigResponse['config']; message: string }>(
      '/ai/config/clear',
      { method: 'POST', body: JSON.stringify({ module }) }
    ),
  test: (module: string, base_url: string, model: string, api_key?: string, probe?: string, timeout?: number,
         reasoning_effort?: string) =>
    request<AITestResult>(
      '/ai/test',
      { method: 'POST', body: JSON.stringify({ module, base_url, model, api_key, probe, timeout, reasoning_effort }) }
    ),
};

// --- System Settings (LLM engine, ComfyUI, Watermark) ---
export const settingsApi = {
  get: () => request<{ success: boolean; settings: Record<string, unknown> }>('/ai/settings'),
  update: (data: Record<string, unknown>) =>
    request<{ success: boolean; message?: string }>('/ai/settings', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
};

/**
 * 视频水印配置。此前 AI 配置页把水印开关塞进 `/ai/settings`，
 * 而那个接口的 GET 返回的是「创作设定」（art_style/genre/tone…），
 * 从来不包含 watermark_enabled —— 于是开关永远显示为关，
 * 保存也写不进真正的配置。这里直接对接专用的 /watermark/config。
 */
export const watermarkApi = {
  get: () =>
    request<{ success?: boolean; config: { enabled: boolean; text?: string; [k: string]: unknown } }>(
      '/watermark/config'
    ),
  update: (patch: Record<string, unknown>) =>
    request<{ success?: boolean; config: Record<string, unknown> }>('/watermark/config', {
      method: 'POST',
      body: JSON.stringify(patch),
    }),
};

/**
 * 超分（FlashVSR）—— app/upscale_client.py 的界面入口。
 *
 * 后端 8 个 `/api/upscale/*` 端点早已实现且可用（含 TE-Speed 加速链路与
 * 旧链路自动回退），但此前前端零引用，属于「建好没入口」的能力。
 *
 * ⚠️ attach_audio 必须显式传 true：TE-Speed 链路默认 attach_audio=False，
 *    对「成片」超分时会把已合成的 TTS 配音整轨丢掉，产出无声视频。
 *    该参数此前也不在后端白名单里，已一并补上。
 */
export const upscaleApi = {
  /** 链路自检：ComfyUI 在线 / FlashVSR 模型 / 节点是否齐备 */
  env: () => request<UpscaleEnv>('/upscale/env'),

  /** 可作为超分输入的候选视频（成片、视频片段、已有超分产物、ComfyUI 产出） */
  sources: (projectName: string) =>
    request<{ success: boolean; project_name: string; items: UpscaleSource[] }>(
      `/upscale/sources?project_name=${encodeURIComponent(projectName)}`
    ),

  /** 已生成的超分产物 */
  list: (projectName: string) =>
    request<{ success: boolean; project_name: string; items: UpscaleArtifact[] }>(
      `/upscale/list?project_name=${encodeURIComponent(projectName)}`
    ),

  /** 发起超分（异步）：返回 task_id，用 status() 轮询 */
  submit: (data: {
    project_name: string;
    video_path: string;
    scale?: 2 | 3 | 4;
    mode?: string;
    engine?: 'te-speed-flashvsr' | 'legacy-flashvsr';
    /** 保留源视频音轨（成片必开，否则丢配音） */
    attach_audio?: boolean;
    [k: string]: unknown;
  }) =>
    request<UpscaleSubmitResponse>('/upscale/video', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  /** 查询单个超分任务进度/结果 */
  status: (taskId: string) =>
    request<UpscaleTask>(`/upscale/status/${encodeURIComponent(taskId)}`),

  /** 全部超分任务（按创建时间倒序） */
  tasks: () => request<{ success: boolean; items: UpscaleTask[] }>('/upscale/tasks'),
};
