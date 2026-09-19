// ============================================
// API 类型定义（匹配后端实际返回结构）
// ============================================

// --- Projects ---
export interface ProjectConfig {
  duration_per_shot: number;
  episode_duration_sec: number;
  episodes: number;
  fps: number;
  qc_enabled: boolean;
  resolution: string;
  shots_per_episode: number;
  style: string;
  target_shots: number;
  voice_map: Record<string, string>;
}

export interface Project {
  config: ProjectConfig;
  created_at: string;
  dir_key: string;
  episode_count: number;
  episode_duration_sec: number;
  from_migration?: boolean;
  id: string;
  name: string;
  note?: string;
  novel_id: string;
  status?: 'running' | 'paused' | 'done' | 'failed';
}

export interface ProjectsResponse {
  success: boolean;
  total: number;
  index_path: string;
  projects: Project[];
}

// --- Novels ---
export interface Novel {
  bound: boolean;
  chapter_count: number;
  char_count: number;
  encoding: string;
  encoding_note?: string;
  ext: string;
  format: string;
  line_count: number;
  name: string;
  novel_id: string;
  project_id: string;
}

export interface NovelsResponse {
  success: boolean;
  count: number;
  all_count: number;
  project_id: string | null;
  novels: Novel[];
  supported_exts: string[];
  total_chars: number;
}

// --- Tasks ---
export interface Task {
  id: string;
  kind: 'keyframe' | 'storyboard' | 'video' | 'voice' | 'assemble' | 'qc';
  label: string;
  project: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  progress: number;
  created_at: string;
  started_at: string;
  finished_at: string;
  error: string;
  result_path: string;
  payload: Record<string, unknown>;
}

export interface TasksResponse {
  success: boolean;
  count: number;
  items: Task[];
  queue: Task[];
}

// --- Characters ---
/**
 * 与后端 app/character_manager.py 落盘的结构（characters.json → characters.<id>）对齐：
 *   { id, name, role, description, outfit, status, views, references, created_at, updated_at }
 *
 * ⚠️ 后端**没有 images 字段** —— 图片分别存在 views（五视图）与 references（参考图）里。
 * 这里曾把 images 声明成必填的 string[]，调用方照着类型写 `char.images.length`，
 * 运行时读到 undefined → TypeError → 整页白屏（测试报告 #1）。
 * 教训：类型必须跟后端真实结构一致，可为空的一律标可选，前端渲染再做兜底。
 */
export interface Character {
  id: string;
  name: string;
  /** 后端不做枚举约束（AI 可能产出其它值），别用字面量联合把类型写死 */
  role: string;
  description?: string;
  outfit?: string;
  status?: string;
  /** 五视图：front / three_quarter / side / back / expressions，未生成为 null */
  views?: Record<string, string | null>;
  /** 参考图路径列表（后端实际字段） */
  references?: string[];
  /** @deprecated 后端无此字段，仅为兼容历史前端数据保留；新代码请用 references / views */
  images?: string[];
  /** 仅请求参数使用：POST /api/characters 必带，缺了后端返回 400 */
  project?: string;
  project_id?: string;
  created_at?: string;
  updated_at?: string;
}

// --- Relations ---
export interface Relation {
  id: string;
  project_id: string;
  char_a: string;
  char_b: string;
  type: string;
  strength: number;
}

// --- Memory ---
/** 后端 ai_memory 实际写入的类型集合（见 app/ai_memory.py 的 mem_type 说明） */
export type MemoryType = 'lesson' | 'success' | 'insight' | 'pattern' | 'failure' | 'test';

export interface Memory {
  /** 后端主键字段名是 mem_id / mem_type。
   *  此前的类型定义写成了 id / type，页面据此取值恒为 undefined，
   *  于是列表渲染出字面量 `memory.type.undefined`、React key 也为空。 */
  mem_id: string;
  mem_type: MemoryType;
  content: string;
  tags: string[];
  created_at: string;
  confidence?: number;
  source?: string;
  usage_count?: number;
  last_used?: string | null;
  context?: Record<string, unknown>;
  /** 兼容旧字段名（部分接口可能回传） */
  id?: string;
  type?: MemoryType;
}

export interface MemoryStats {
  total: number;
  lessons: number;
  successes: number;
  insights: number;
  /** 质检教训库（生成链路自动学习）条数 —— 与上面的手动记忆是两套数据 */
  promptLessons?: number;
}

/** 质检教训库条目（prompt_memory：质检不达标时自动沉淀，重试前召回改写提示词） */
export interface PromptLesson {
  ts?: string;
  project?: string;
  kind?: string;
  phash?: string;
  prompt?: string;
  issues?: string[];
  reason?: string;
  score?: number | null;
  terms?: string[];
}

// --- Settings ---
export interface AppSettings {
  llm_provider: string;
  llm_api_key: string;
  comfyui_url: string;
  tts_provider: string;
  tts_api_key: string;
  watermark_enabled: boolean;
  watermark_text: string;
}

// --- i18n ---
export interface I18nData {
  success: boolean;
  lang: string;
  available: string[];
  messages: Record<string, any>;
}

// --- Analytics ---
export interface AnalyticsData {
  total_projects: number;
  total_tasks: number;
  total_cost: number;
  tasks_by_kind: Record<string, number>;
  projects_by_status: Record<string, number>;
}

// --- Keyframes ---
export interface KeyframePlan {
  shot_id: string;
  seq: number;
  has_start: boolean;
  has_end: boolean;
  need_gen: boolean;
  url?: string;
  status?: string;
}

export interface KeyframePlanResponse {
  success: boolean;
  project: string;
  shot_count: number;
  keyframes_dir: string;
  start_frames_ready: number;
  end_frames_ready: number;
  to_generate: number;
  plan: KeyframePlan[];
}

// --- Storyboard ---
export interface StoryboardShot {
  shot_id: string;
  seq: number;
  camera: string;
  duration: string;
  location: string;
  emotion: string;
  description: string;
  dialogue_text: string;
  storyboard?: {
    exists: boolean;
    url: string;
    qc?: Record<string, unknown>;
    success?: boolean;
    blocked?: boolean;
  };
  video?: {
    exists: boolean;
    url: string;
  };
  keyframe?: {
    start: boolean;
    end_exists: boolean;
    end_url: string;
  };
  consistency?: Record<string, unknown>;
  coverage?: Record<string, unknown>;
}

export interface StoryboardCanvasResponse {
  success: boolean;
  project: string;
  episode_no?: number;
  episode_title?: string;
  title?: string;
  summary: {
    shot_count: number;
    storyboard_ready: number;
    video_ready: number;
    keyframe_end_ready: number;
    qc_blocked: number;
  };
  cards: StoryboardShot[];
  shot_order: string[];
}

// --- TTS ---
export interface TTSEnv {
  available: boolean;
  reasons?: string[];
  voices?: string[];
  comfyui_online?: boolean;
  qwen_tts_available?: boolean;
  dub_dir?: string;
}

export interface TTSPlanLine {
  shot_id: string;
  seq: number;
  text: string;
  character: string;
  voice: string;
  duration?: number;
  url?: string;
  exists?: boolean;
  out_path?: string;
}

export interface TTSPlanResponse {
  success: boolean;
  script_path: string;
  script_source: string;
  episode: number;
  line_count: number;
  characters: Array<{ name: string; voice: string; line_count: number }>;
  lines: TTSPlanLine[];
  voice_map: Record<string, unknown>;
  out_dir: string;
}

export interface TTSTask {
  task_id: string;
  status: 'running' | 'completed' | 'failed';
  progress: number;
  phase: string;
  message: string;
  project_name: string;
  total: number;
  current: number;
  plan?: TTSPlanResponse;
  results?: Array<{ ok: boolean; path?: string; error?: string }>;
}

// --- Mix ---
export interface MixEnv {
  available: boolean;
  reasons?: string[];
}

export interface MixPlanResponse {
  success: boolean;
  video_count: number;
  audio_count: number;
  to_generate: number;
  lines: Array<{ video: string; audio: string; output: string }>;
  out_dir: string;
}

export interface MixTask {
  task_id: string;
  status: 'running' | 'completed' | 'failed';
  progress: number;
  phase: string;
  message: string;
  project_name: string;
  total: number;
  current: number;
  /** 产物路径（完成后才有） */
  output_path?: string;
  /** 播放地址（后端用 _mix_audio_url 计算） */
  url?: string;
  /** 混音报告路径 */
  report_path?: string;
  /** 合成完成后的统计（entry_count / elapsed_sec / warnings…） */
  result?: { entry_count?: number; elapsed_sec?: number; warnings?: string[] };
  /**
   * 后端新增：带配音成片是否已自动进入「成品验收」队列。
   * registered=false 时看 reason（例如成片过小被判为半成品）。
   */
  deliverable?: {
    registered: boolean;
    reason: string;
    episode_no: number;
    path: string;
  };
}

/** ⚠️ /api/mix/status 返回的是信封 {success, task}，不是裸任务对象 */
export interface MixStatusResponse {
  success: boolean;
  task: MixTask;
}

// --- QC ---
export interface QCConfig {
  enabled: boolean;
  max_retries: number;
  threshold: number;
  endpoint?: string;
}

export interface QCHistoryItem {
  shot_id: string;
  seq: number;
  score: number;
  verdict: 'pass' | 'fail' | 'retry';
  timestamp: string;
  error?: string;
}

export interface QCResponse {
  success: boolean;
  config: QCConfig;
  history: QCHistoryItem[];
  stats?: {
    total: number;
    passed: number;
    failed: number;
    retry_count: number;
  };
}

// --- Episodes ---
export interface Episode {
  episode_no: number;
  title?: string;
  status: 'pending' | 'producing' | 'done' | 'failed';
  shot_count: number;
  completed_shots: number;
  created_at?: string;
  updated_at?: string;
}

export interface EpisodeListResponse {
  success: boolean;
  novel_id: string;
  episodes: Episode[];
  total: number;
}

// --- Autopilot ---
export interface AutopilotStatus {
  running: boolean;
  paused: boolean;
  current_project?: string;
  current_episode?: number;
  totals: {
    episodes_done: number;
    episodes_failed: number;
    retries: number;
  };
}

export interface AutopilotProgress {
  project: string;
  total_episodes: number;
  done: number;
  failed: number;
  current_episode?: number;
  exceptions?: Array<{ episode: number; error: string }>;
}

// --- Deliverables（成品验收） ---
/**
 * 成片清单条目，对应 pipeline.list_deliverables() 的返回。
 * 真实落盘结构（output/autopilot/<项目>/deliverables.json）里是：
 *   { project, episode_no, path, filename, size, meta:{title,...} }
 * 后端另外**计算**出两个字段：
 *   exists（文件是否真在磁盘上）
 *   url（/api/autopilot/deliverable/file/<项目>/<文件名>，已防目录穿越）
 * 播放直接用 url；下载用 url + '?download=1'。**不要自己拼文件名猜路径**。
 */
export interface Deliverable {
  project: string;
  episode_no: number;
  filename: string;
  /** 落盘绝对路径（仅展示用，不要拿去请求） */
  path?: string;
  /** 字节数 */
  size?: number;
  /** 元信息，含 title（章节标题）等 */
  meta?: {
    title?: string;
    chapter_index?: number;
    elapsed_sec?: number;
    retries?: number;
    /** 成片来源：final_video（合并）/ mix（带配音混音）/ probe 等 */
    source?: string;
    /** 成片时长（秒） */
    duration_sec?: number | null;
    /** 剧本镜头总数 / 已发现镜头视频数（后端自动登记时写入） */
    shots_total?: number;
    shots_ready?: number;
    /** 镜头数不齐时后端打的标记：true 表示成片可能不完整 */
    incomplete_shots?: boolean;
    /** 配合 incomplete_shots 的提示文案 */
    warning?: string;
    /** 成片登记后被标记为「已过期」（同集镜头重做过，成片需重新合成） */
    stale?: {
      reason?: string;
      marked_at?: string;
      detail?: { shot_id?: string | number; seq?: number; mode?: string; video?: string };
    };
  };
  /** 验收状态：pending 待验收 / accepted 已验收 / rejected 已打回 */
  review?: 'pending' | 'accepted' | 'rejected';
  /** ⚠️ 后端字段名是 `review_note`，不是 `note`（见 pipeline.set_deliverable_review） */
  review_note?: string;
  /** 最近一次验收/打回的时间 */
  reviewed_at?: string;
  created_at?: string;
  updated_at?: string;
  /** 后端计算：文件是否真实存在 */
  exists?: boolean;
  /** 后端计算：播放/下载地址 */
  url?: string;
}

export interface DeliverablesResponse {
  success: boolean;
  count: number;
  /** 待验收条数 */
  pending: number;
  items: Deliverable[];
}

// --- Providers ---
export interface Provider {
  id: string;
  name: string;
  model: string;
  base_url: string;
  is_default: boolean;
}

export interface ProvidersResponse {
  success: boolean;
  providers: Provider[];
}

// --- AI Config (Unified: text / qc / chat) ---
export interface AIConfigModule {
  base_url: string;
  api_key: string;    // masked on read
  model: string;
  /** 思考档位：'' = 不注入（服务端默认），其余为 low / high / max。思考不可关闭的模型用 */
  reasoning_effort?: string;
  updated_at: string | null;
  has_api_key?: boolean;  // whether a real key is stored
  key?: string;       // module key
  label?: string;
  desc?: string;
  need_vision?: boolean;
  placeholder_model?: string;
  used_by?: string[];
}

export interface AIConfigResponse {
  success: boolean;
  // 后端把三个模块放在 config.modules 下（不是平铺），模块内 api_key 一律脱敏为 api_key_masked
  config: {
    modules: Record<string, AIConfigModule>;
    module_order?: string[];
    modules_meta?: Record<string, {
      label: string;
      desc: string;
      need_vision: boolean;
      placeholder_model: string;
      used_by: string[];
    }>;
    config_path?: string;
    legacy_path?: string;
    updated_at?: string;
    migrated_from?: string;
    /** 思考档位可选项（含开头的空串，表示「不注入」） */
    reasoning_effort_options?: string[];
    /** ComfyUI 地址的实际来源（环境变量），因此只能只读展示 */
    comfyui?: {
      url: string;
      source: string;
      editable: boolean;
    };
  };
}

export interface AITestResult {
  success: boolean;
  module: string;
  probe: string;
  model?: string;
  base_url?: string;
  chat_url?: string;
  /** ⚠️ 后端真实字段是 latency_ms；response_time_ms 仅为兼容旧值保留 */
  latency_ms?: number;
  response_time_ms?: number;
  error?: string;
  guide?: string;
  /**
   * 后端给出的人类可读结论：
   * - `ok`：链路通且拿到了正文
   * - `reachable_but_no_content`：链路通，但模型这次没输出正文
   *   （允许思考时额度被思考吃掉）——**不要当成配置错误**
   * - `failed`：确实连不上 / 鉴权失败
   */
  verdict?: 'ok' | 'reachable_but_no_content' | 'failed';
  /** 模型回复片段（用于人工确认返回的确实是模型内容） */
  reply?: string;
  /** 配合非 ok 结论的处置建议 */
  hint?: string;
  /** 结束原因（length 表示被截断） */
  finish_reason?: string;
  truncated?: boolean;
  /** 正文为空（把额度全用在思考上） */
  thinking_only?: boolean;
  /** 该次探测实际使用的 max_tokens */
  max_tokens?: number;
  disable_thinking?: boolean;
  /** 视觉探测：true 支持 / false 不支持 / null 未确认（无正文） */
  vision?: boolean | null;
  /** 视觉探测未确认时为 true */
  uncertain?: boolean;
  attempts?: number;
  retries_used?: number;
}

// ========== 超分（FlashVSR） ==========
// 后端能力早已完整实现（app/upscale_client.py + 8 个 /api/upscale/* 端点），
// 但此前没有任何界面入口。以下类型对齐 app.py `api_upscale_*` 的真实返回。

/** GET /api/upscale/env —— 链路自检 */
export interface UpscaleEnv {
  available: boolean;
  /** ComfyUI 是否在线 */
  comfy_online: boolean;
  comfy_version?: string;
  comfy_url?: string;
  /** FlashVSR 模型文件是否齐备 */
  model_ready: boolean;
  model_dir?: string;
  model_files?: { name: string; exists: boolean; size_mb: number }[];
  /** TE-Speed 加速链路是否可用 */
  te_ready: boolean;
  /** 旧 FlashVSR 链路是否可用 */
  legacy_ready: boolean;
  te_nodes?: Record<string, boolean>;
  nodes?: Record<string, boolean>;
  /** 不可用原因（给人看的中文说明） */
  reasons: string[];
  default_engine?: string;
  te_defaults?: Record<string, unknown>;
  defaults?: Record<string, unknown>;
}

/** GET /api/upscale/sources —— 可作为超分输入的候选视频 */
export interface UpscaleSource {
  /** 分类标签：成片 / 视频片段 / 超分产物 / ComfyUI/xxx */
  kind: string;
  name: string;
  path: string;
  url: string;
  size_mb: number;
  mtime: string;
}

/** POST /api/upscale/video 的返回 */
export interface UpscaleSubmitResponse {
  success: boolean;
  task_id: string;
  input_path: string;
  scale: number;
  engine: string;
  error?: string;
}

/** GET /api/upscale/status/<task_id> —— 任务状态（pending/running/done/error） */
export interface UpscaleTask {
  task_id: string;
  status: 'pending' | 'running' | 'done' | 'error';
  progress: number;
  message: string;
  project_name?: string;
  input_path?: string;
  scale?: number;
  engine?: string;
  error?: string;
  result?: {
    output_path?: string;
    output_url?: string;
    input_url?: string;
    output_filename?: string;
    engine?: string;
    accelerated?: boolean;
    elapsed_sec?: number;
    size_delta_mb?: number;
    before?: { width?: number; height?: number; duration?: number; size_mb?: number; has_audio?: boolean };
    after?: { width?: number; height?: number; duration?: number; size_mb?: number; has_audio?: boolean };
  };
}

/** GET /api/upscale/list —— 已生成的超分产物 */
export interface UpscaleArtifact {
  name: string;
  path: string;
  url: string;
  size_mb: number;
  mtime: string;
}

// ===================== 总控 AI 自主执行（agent） =====================

/** 一次工具调用的执行记录 */
export interface AgentStep {
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  cached?: boolean;
  blocked?: boolean;
  elapsed_sec?: number;
  summary: string;
  result?: string;
}

/** GET /api/agent/job/<id> —— 总控任务状态 */
export interface AgentJob {
  id: string;
  project: string;
  message: string;
  status: 'running' | 'done' | 'failed' | 'killed' | 'timeout';
  steps: AgentStep[];
  reply: string;
  error: string;
  created: number;
  updated: number;
  expensive_used: number;
}

/** GET /api/agent/tools —— 工具清单 */
export interface AgentTool {
  name: string;
  description: string;
  risk: 'safe' | 'write' | 'expensive';
  expensive: boolean;
}

export interface AgentGuards {
  max_steps: number;
  max_expensive: number;
  max_turn_sec: number;
  cooldown_sec: number;
}

export interface AgentKillState {
  on: boolean;
  reason: string;
  at: number;
}
