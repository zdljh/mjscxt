"""
漫剧生成系统配置

配置来源（优先级从高到低）：
1. 环境变量（含项目根目录 .env，由 _load_dotenv 自动加载）
2. 代码内默认值

P0-2 改造：原先硬编码的 ComfyUI / 模型路径已全部改为环境变量驱动，
换机只需修改 .env（见项目根目录 .env.example），无需改代码。
"""
import logging
import os

# 环境加载与项目根目录统一由 env_loader 负责（导入即生效，避免模块导入顺序导致 .env 未加载）
from env_loader import PROJECT_ROOT_DIR, env as _env, env_int as _env_int  # noqa: E402

logger = logging.getLogger(__name__)


def _norm_path(p: str) -> str:
    """规范化路径：统一分隔符，兼容 Windows / Linux 写法"""
    return os.path.normpath(p) if p else ""


# ===================== ComfyUI 配置 =====================
COMFYUI_URL = _env("COMFYUI_URL", "http://127.0.0.1:8188")

# ComfyUI 安装根目录：其余路径默认基于此推导，只需配置这一项即可换机
COMFYUI_ROOT = _env("COMFYUI_ROOT", "")

# 是否启用路径推导（未显式配置各路径时，按 ComfyUI 根目录推导）
# 支持两种常见目录结构：
#   A) COMFYUI_ROOT/ComfyUI/ComfyUI/{workflows,input,output,models}   （portable 版）
#   B) COMFYUI_ROOT/{user/default/workflows,input,output,models}       （标准安装）
def _derive_comfyui_paths(root: str) -> dict:
    if not root:
        return {"workflows": "", "input": "", "output": "", "models": ""}
    candidates = [
        root,
        os.path.join(root, "ComfyUI", "ComfyUI"),
        os.path.join(root, "ComfyUI"),
    ]
    for base in candidates:
        if os.path.isdir(os.path.join(base, "models")):
            return {
                "workflows": os.path.join(base, "user", "default", "workflows"),
                "input": os.path.join(base, "input"),
                "output": os.path.join(base, "output"),
                "models": os.path.join(base, "models"),
            }
    # 无法探测时按 portable 结构兜底
    base = os.path.join(root, "ComfyUI", "ComfyUI")
    return {
        "workflows": os.path.join(base, "user", "default", "workflows"),
        "input": os.path.join(base, "input"),
        "output": os.path.join(base, "output"),
        "models": os.path.join(base, "models"),
    }


_DERIVED = _derive_comfyui_paths(COMFYUI_ROOT)

COMFYUI_WORKFLOWS_DIR = _norm_path(_env("COMFYUI_WORKFLOWS_DIR", _DERIVED["workflows"]))
COMFYUI_INPUT_DIR = _norm_path(_env("COMFYUI_INPUT_DIR", _DERIVED["input"]))
COMFYUI_OUTPUT_DIR = _norm_path(_env("COMFYUI_OUTPUT_DIR", _DERIVED["output"]))

# 模型路径
MODELS_DIR = _norm_path(_env("MODELS_DIR", _DERIVED["models"]))
QWEN_IMAGE_MODEL = os.path.join(MODELS_DIR, "diffusion_models", "qwen-image-2512",
                                "qwen_image_2512_fp8_e4m3fn.safetensors") if MODELS_DIR else ""
H3_MODEL = os.path.join(MODELS_DIR, "diffusion_models", "minimax-h3",
                        "minimax_h3_ref2va_pruned_int8_convrot.safetensors") if MODELS_DIR else ""
FLASHVSR_MODEL = os.path.join(MODELS_DIR, "FlashVSR-v1.1",
                              "diffusion_pytorch_model_streaming_dmd.safetensors") if MODELS_DIR else ""

# FlashVSR 超分模型目录（ComfyUI-FlashVSR_Ultra_Fast 约定：models/FlashVSR-v1.1）
FLASHVSR_MODEL_DIR = os.path.join(MODELS_DIR, "FlashVSR-v1.1") if MODELS_DIR else ""
FLASHVSR_REQUIRED_FILES = [
    "diffusion_pytorch_model_streaming_dmd.safetensors",   # DiT 主权重
    "Wan2.1_VAE.pth",                                      # VAE
    "LQ_proj_in.ckpt",                                     # 低质特征投影
    "TCDecoder.ckpt",                                      # 时序解码器
]

# LLM 配置（P0-3：密钥推荐走环境变量，代码内不保存明文）
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
WORKBUDDY_API_KEY = _env("WORKBUDDY_API_KEY")
LLM_PROVIDER = _env("LLM_PROVIDER", "anthropic")  # anthropic or workbuddy

# 部署档案（8G / 16G / server）：影响超分与视频生成的默认档位
DEPLOY_PROFILE = _env("DEPLOY_PROFILE", "16G")

# ===================== 输出目录（须先于各产物子目录定义） =====================
PROJECT_OUTPUT_DIR = os.path.join(PROJECT_ROOT_DIR, "output")

# ===================== 小说上传与 LLM 配置路径 =====================
# 小说上传目录（原始文件 + 解析后的标准化文本 + 元数据索引）
NOVELS_DIR = os.path.join(PROJECT_ROOT_DIR, "novels")

# 自定义 LLM API 配置（base_url / api_key / model 持久化，api_key 不明文回显）
LLM_CONFIG_PATH = os.path.join(PROJECT_ROOT_DIR, "llm_config.json")

# AI 质检配置（总开关 / 图片·视频独立开关 / 模型 / 判定标准 / 最大重试次数）
QC_CONFIG_PATH = os.path.join(PROJECT_ROOT_DIR, "qc_config.json")
QC_DIR = os.path.join(PROJECT_OUTPUT_DIR, "qc")   # 质检与重试历史 + 视频抽帧
QC_CHECK_INTERVAL = 3          # 生成任务状态里质检阶段的轮询提示间隔（秒，仅前端用）

# 视频水印配置（C 项：默认关闭；支持文案/图片、位置、字号、透明度、边距、全视频移动模式）
WATERMARK_CONFIG_PATH = os.path.join(PROJECT_ROOT_DIR, "watermark_config.json")
WATERMARK_DIR = os.path.join(PROJECT_OUTPUT_DIR, "watermark")   # 带水印视频产物目录

# P0-4 持久化任务队列（SQLite）：任务全生命周期落盘，支持断点续跑
TASKS_DB_PATH = os.path.join(PROJECT_OUTPUT_DIR, "tasks.db")
TASK_QUEUE_CONCURRENCY = _env_int("TASK_QUEUE_CONCURRENCY", 1)   # 单 GPU 建议保持 1
TASK_UNIT_MIN_BYTES = _env_int("TASK_UNIT_MIN_BYTES", 1024)      # 单元产物视为有效的最小字节数

# 统一「AI 设置」：文本分析 / 质检 / 对话总控 三个相互独立的模型模块（各自 base_url / api_key / model）
AI_CONFIG_PATH = os.path.join(PROJECT_ROOT_DIR, "ai_config.json")
AI_MODULES = ("text", "qc", "chat")

# 小说解析与转换参数
NOVEL_CHUNK_CHARS = 3000        # 长篇小说分块字符数
NOVEL_MAX_CHUNKS = 8            # 单次转换最多送入模型的块数（抽样上限，避免超出上下文）
NOVEL_DEFAULT_SHOTS = 12        # 默认目标镜头数
NOVEL_PREVIEW_CHARS = 4000      # 前端预览单页字符数
NOVEL_BRIEF_CHARS = 800         # 「原著简报」正文取样字符数（喂给 AI 总控做风格判断，≤ agent 结果窗口）
# 单次 LLM 请求超时（秒）。⚠️ 必须可 env 覆盖：reasoning_effort=max + 长章节（数千字正文）
# 的剧本生成会一路提额 max_tokens（9300→12288→16384→24576），单次最重调用实测连 900s 都
# 不够（2026-09-19 ep002 第一节 3297 字，900s 仍 ReadTimeout 反复 5 次）。
# 默认放宽到 1800s（覆盖最重调用）；需要更严/更松可设 env LLM_REQUEST_TIMEOUT。
LLM_REQUEST_TIMEOUT = int(os.environ.get("LLM_REQUEST_TIMEOUT", "1800"))

# ===================== 项目级隔离（每部小说 = 一个独立项目） =====================
# 注册表与每项目配置/隔离目录；各产物仍落在既有 output/<kind>/<项目键>/ 下，
# 由「项目键（dir_key）」实现物理隔离，注册表负责把项目键与小说/剧本绑定起来。
PROJECTS_DIR = os.path.join(PROJECT_OUTPUT_DIR, "projects")
PROJECT_INDEX_PATH = os.path.join(PROJECTS_DIR, "index.json")           # 项目列表（注册表）
PROJECT_TRASH_DIR = os.path.join(PROJECTS_DIR, "_trash")               # 删除项目的回收站（可恢复）
PROJECT_MIGRATE_REPORT = os.path.join(PROJECTS_DIR, "migration_report.json")   # 历史数据归属迁移报告

# 新项目默认配置（每项目一份，落在 output/projects/<项目ID>/config.json）
PROJECT_DEFAULT_CONFIG = {
    "style": "3D动漫渲染",              # 创作风格
    "episodes": 1,                      # 目标集数
    "target_shots": 12,                 # 目标镜头数（AI 转剧本时自动判定，此处为期望值）
    "shots_per_episode": 12,
    "episode_duration_sec": 60,         # 每集期望时长（秒）
    "resolution": "768p_vertical",
    "fps": 24,
    "duration_per_shot": 5,             # 单镜头默认秒数
    "voice_map": {},                    # 角色→音色映射（按项目隔离）
    "qc_enabled": False,                # 质检开关（按项目隔离）
}

# ===================== 跨集连贯性（相邻两章转剧本改进 A/B/C/D） =====================
# 项目级设定库 / 风格指南 / 金句清单 / 口吻词典 / 运镜术语表 / 各集 state 与校验结果
CONTINUITY_DIR = os.path.join(PROJECT_OUTPUT_DIR, "continuity")
SCRIPT_DIR = os.path.join(PROJECT_OUTPUT_DIR, "scripts")
ASSETS_DIR = os.path.join(PROJECT_OUTPUT_DIR, "assets")
CHARACTERS_DIR = os.path.join(ASSETS_DIR, "characters")   # 角色资产（含多视图）
ITEMS_DIR = os.path.join(ASSETS_DIR, "items")             # 物品资产（含3D多视角）
SCENES_DIR = os.path.join(ASSETS_DIR, "scenes")           # 场景资产（含3D多视角）
STORYBOARDS_DIR = os.path.join(PROJECT_OUTPUT_DIR, "storyboards")  # 分镜图（按项目名分子目录）
# 关键帧目录（P1-2 关键帧驱动视频模式）：output/keyframes/<项目>/shot_NN_start.png + shot_NN_end.png
KEYFRAMES_DIR = os.path.join(PROJECT_OUTPUT_DIR, "keyframes")
VIDEOS_DIR = os.path.join(PROJECT_OUTPUT_DIR, "videos")
FINAL_DIR = os.path.join(PROJECT_OUTPUT_DIR, "final")
# 超分结果目录（与原始视频严格隔离：原始在 videos/ final/，超分结果在 upscale/）
UPSCALE_DIR = os.path.join(PROJECT_OUTPUT_DIR, "upscale")

# 超分默认参数（前端可覆盖；mode 对应 FlashVSR Ultra-Fast 的 tiny / tiny-long / full）
UPSCALE_DEFAULT_PARAMS = {
    "scale": 2,                     # 倍率：2 或 4
    "mode": "tiny",                 # tiny / tiny-long / full（8G 显存笔记本默认 tiny）
    "tile_size": 256,
    "tile_overlap": 24,
    "tiled_vae": True,
    "tiled_dit": True,
    "unload_dit": False,
    "color_fix": True,
    "sparse_ratio": 2.0,
    "kv_ratio": 3.0,
    "local_range": 11,
    "precision": "bf16",
    "attention_mode": "sparse_sage_attention",
    "force_offload": True,
    "seed": 0,
    "timeout": 3600,                # 单次超分等待上限（秒）
}

# ===================== 超分引擎：TE-Speed-flashVSR 加速链路（默认） =====================
# 模板来源（只读解析，不修改 ComfyUI 原始工作流文件）：
#   D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\user\default\workflows\TE-Speed-flashVSR 视频超分放大加速工作流.json
# 加速链：TEFlashVSRModelLoader(mode=tiny/precision=bf16) → TEFlashVSRTuning(sparse_sage2 稀疏注意力
#         + 分块 max_tile_edge/blend_overlap) → TEFlashVSRRestore(scale/color_fix) → TESpeedVideoCombine
UPSCALE_ENGINE = os.getenv("UPSCALE_ENGINE", "te-speed-flashvsr")   # te-speed-flashvsr / legacy-flashvsr
UPSCALE_TE_TEMPLATE_PATH = os.path.join(
    COMFYUI_WORKFLOWS_DIR, "TE-Speed-flashVSR 视频超分放大加速工作流.json")

# TE-Speed-flashVSR 默认参数（与模板工作流 JSON 中的接线值一致；前端可覆盖）
# 模板实测值：ModelLoader=[FlashVSR-v1.1, tiny, bf16, auto]
#             Tuning=[balanced, 1, auto, auto, 2, 3, 11, 256, 24, 4, sparse_sage2]
#             Restore=[scale 2, color_fix True]
TE_UPSCALE_DEFAULT_PARAMS = {
    "scale": 2,                     # 倍率：2 / 3 / 4
    "mode": "tiny",                 # tiny / tiny-long / full（加速工作流的显存档位，模板=tiny）
    "precision": "bf16",            # bf16 / fp16
    "device": "auto",               # auto / cuda:0
    "quality_profile": "balanced",  # detail / balanced / throughput
    "intensity": 1.0,               # 0.0 - 1.0
    "spatial_strategy": "auto",     # auto / full_frame / adaptive_tiles（低显存优先 adaptive_tiles）
    "memory_policy": "auto",        # auto / resident / staged（低显存可选 staged）
    "attention_backend": "sparse_sage2",   # 加速关键：稀疏 SageAttention2
    "attention_budget": 2.0,        # 1.0 - 2.0
    "kv_retention": 3.0,            # 1.0 - 3.0
    "local_radius": 11,             # 9 - 11
    "max_tile_edge": 256,           # 128 - 2048（分块最大边长，越小越省显存）
    "blend_overlap": 24,            # 0 - 512
    "preprocess_batch": 4,          # 1 - 32
    "color_fix": True,
    "quality_value": 3,             # TESpeedVideoCombine 压缩质量档（1-8）
    "frame_load_cap": 0,            # 0 = 全部帧
    "skip_first_frames": 0,
    "free_vram": True,              # 提交前请求 ComfyUI /free 释放已缓存模型（8G 显存友好）
    "seed": 0,
    "timeout": 3600,                # 单次超分等待上限（秒）
}

# 8G 显存低显存档（前端一键切换；显存吃紧时用）
TE_UPSCALE_LOWVRAM_PARAMS = {
    "mode": "tiny",
    "spatial_strategy": "adaptive_tiles",
    "memory_policy": "staged",
    "attention_budget": 1.0,
    "kv_retention": 1.0,
    "local_radius": 9,
    "max_tile_edge": 256,
    "blend_overlap": 24,
    "preprocess_batch": 2,
    "quality_profile": "throughput",
}

# 超分耗时标定（离线预估用）：output/upscale/calibration.json
UPSCALE_CALIBRATION_PATH = os.path.join(UPSCALE_DIR, "calibration.json")

# ===================== 配音（QwenTTS） =====================
# 配音产物目录：output/dub/<项目>/lines/ 单句 + output/dub/<项目>/<项目>_epNN_配音.wav 合并音轨
DUB_DIR = os.path.join(PROJECT_OUTPUT_DIR, "dub")

# QwenTTS 默认合成参数（前端可按角色覆盖 speaker / instruct / seed 等）
TTS_DEFAULT_PARAMS = {
    "model_choice": "1.7B",         # 1.7B / 0.6B
    "language": "Chinese",
    "precision": "bf16",
    "device": "auto",
    "attention": "auto",
    "temperature": 1.0,
    "top_p": 0.8,
    "top_k": 20,
    "repetition_penalty": 1.05,
    "max_new_tokens": 2048,
    "batch_lines": 4,               # 一次 ComfyUI 提交内合并的台词句数（模型只加载一次）
    # 情绪化配音：把剧本每镜的 emotion 送进 TTS，有情绪时自动切 VoiceDesign 模式。
    # 关掉则退回「每个角色一个固定 preset 音色」的老行为（所有台词一个调）。
    "emotion_aware": True,
    "keep_model_loaded": False,     # 批次结束后卸载模型，避免与 H3 抢显存
    "timeout": 1800,                # 单批配音等待上限（秒）
}

# ===================== 音画合成（配音轨 × 成片视频） =====================
# 带配音成片落盘目录：output/final_dub/<项目>/<成片名>_dubbed.mp4
DUB_MIX_DIR = os.path.join(PROJECT_OUTPUT_DIR, "final_dub")

# 合成默认参数
MIX_DEFAULT_PARAMS = {
    "mode": "timeline",         # timeline = 按镜头时间轴对齐；concat = 顺次拼接（整轨）
    "lead_in_sec": 0.0,         # 逐句整体提前/延后（秒，可正可负）
    "gap_sec": 0.0,             # 每句之间额外间隔（秒）
    "max_line_sec": 8.0,        # 单句最长占用（0 = 不限制；超出按 atempo 变速压缩，不裁切内容）。
    #                            ⚠️ 2026-09-19 之前这里是 0.0，导致 dub_mix.build_timeline_entries
    #                            里的 `if max_line > 0 and dur > max_line` 恒为假 —— 变速兜底是死代码
    #                            （实测 ep04 全部条目 fit_ratio 恒为 1.0）。台词写超预算时不再有人兜底，
    #                            只能沿时间轴溢出到后面几镜，尾部被成片 `-shortest` 静默截掉。
    #                            取值依据：单镜台词预算 30 字 ÷ 4.5 字/秒 ≈ 6.7 秒，这里留到 8 秒，
    #                            即「正常预算内的台词不动，明显超预算的才压」。
    "video_codec": "copy",      # copy = 不重编码画面（快）；reencode = libx264 重编码
    "audio_bitrate": "192k",
    "sample_rate": 48000,
    "keep_original_audio": True,    # 成片原音轨（H3 生成的环境音/打斗音效）保留并垫底，
                                    # 2026-09-17 由 False 改为 True：此前直接丢弃，导致成片只剩 TTS 人声
    "original_audio_volume": 0.3,   # 原音效垫底音量（0~1）。1.0 会盖住台词；0.3 是"听得到但不抢戏"
    "timeout": 900,             # 单次合成等待上限（秒）
    # 逐句微调（离线标定）：{line_id: 秒}，正值延后
    "line_offsets": {},
}

# 超分 / 合成任务的离线校准（人工听感微调后落盘，可复用）
MIX_CALIBRATION_PATH = os.path.join(PROJECT_OUTPUT_DIR, "final_dub", "mix_calibration.json")

# ===================== H3 视频音频策略 =====================
# H3 是「音视频联合生成」模型（主模型 minimax_h3_ref2va ＋ 专属音频 VAE
# minimax_h3_audio_vae_fp32），工作流里 VAEDecodeAudio → CreateVideo.audio 线路本来就是通的
# → 画面与音效（打斗/雨声/环境音）**本来就一起产出**。
#
# 2026-09-17 起改为「分层保留」：
#   保留 H3 原生音轨 → 用 HDEMUCS 人声分离剔掉 H3 自己生成的对白人声
#   （否则会与全剧统一的 QwenTTS 音色打架）→ 音效以 MIX_DEFAULT_PARAMS.original_audio_volume
#   垫底，QwenTTS 台词叠在上面。
# 旧策略是 H3_EMIT_AUDIO=False + H3_STRIP_AUDIO=True **全部丢弃**，代价是成片完全没有音效
# （实测 output/videos/** 与 output/final/** 全部无音频流，唯一音轨来自 TTS）。
H3_EMIT_AUDIO = True                # True = 保留 CreateVideo.audio 输入（H3 正常产出音轨）
H3_STRIP_AUDIO = False              # False = 不再 ffprobe 剥离音轨（改由人声分离环节处理）
# 末镜音轨人声分离（只留音效/环境声，去掉 H3 自带的说话声）
H3_SFX_ISOLATE = True               # True = 逐镜跑 HDEMUCS 分离；失败 **fail-open** 保留原音轨
H3_SFX_STEMS = (1, 2)               # HDEMUCS 输出下标：0=Bass 1=Drums 2=Other(音效/环境) 3=Vocals
                                    # 取 Drums+Other：打斗的撞击/鼓点常被判进 Drums
H3_SFX_DIR = os.path.join(PROJECT_OUTPUT_DIR, "sfx")   # 分离产物：output/sfx/<项目>/...

# AI 对话（创作总控）：多轮对话历史 + 已生效的项目创作设定
AI_CHAT_DIR = os.path.join(PROJECT_OUTPUT_DIR, "ai_chat")
AI_CHAT_HISTORY_PATH = os.path.join(AI_CHAT_DIR, "chat_history.json")
AI_SETTINGS_PATH = os.path.join(AI_CHAT_DIR, "project_settings.json")

# 工作流文件
WORKFLOW_TEMPLATE = {
    # H3 视频生成：该文件仅作为"段链 + 无缝拼接"结构母版，
    # 实际段数由 h3_episode_builder 按调用方分镜数动态重建
    # （1 段 = 逐镜头；22/44 段 = 整集一次生成），不再固定 10 段。
    "h3_video": "H3信号10段测试001.json",
    # ---- 图片链路：2026-09-23 起统一切换到 QwenImage2.1 + TE-Speed 加速链 ----
    # 母版参考 ComfyUI 工作流「TE-Speed-QwenImage21 加速插件-提速30%(1).json」；
    # *_Qwen21.json 由 .workbuddy/tools/build_qwen21_workflows.py 生成（可复现）。
    # 旧的 2512 / 2511 工作流文件**保留在同目录**，改回本表即可整体回滚。
    #
    # ⚠️ 新模板的提示词节点是 TextEncodeQwenImage21：**正负同体**（prompt + negative_prompt
    #    在同一节点），参考图槽位是 autogrow 点号键（images.image_1..3）。
    #    改图片链路前务必先跑 verify_qwen21_migration.py / verify_watermark_slot.py。
    "character_gen": "角色生成_Qwen21.json",     # QwenImage2.1 角色基础图（T2I）
    "item_gen": "物品生成_Qwen21.json",          # QwenImage2.1 物品基础图（T2I）
    "scene_gen": "场景生成_Qwen21.json",         # QwenImage2.1 场景基础图（T2I）
    "multiview_gen": "分镜生成_Qwen21.json",     # QwenImage2.1 多视角编辑（角色多视图/物品场景3D多视角）
    "storyboard_gen": "分镜生成_Qwen21.json",    # QwenImage2.1 分镜生成（参考图编辑）
}

# 关键帧「跨镜链式」默认模式：上一镜尾帧 = 下一镜首帧（与参考工作流一致）
#   auto   = 仅相邻两镜同场景时串帧（默认，跨场景切场不串，避免把上一场的画面带进新场）
#   always = 无条件串帧
#   off    = 关闭（旧行为：每镜用自己的分镜图当首帧，镜与镜画面各画各的）
KEYFRAME_CHAIN_MODE = _env("KEYFRAME_CHAIN_MODE", "auto").strip().lower() or "auto"

# 多视角生成配置
MULTIVIEW_CONFIG = {
    # 角色多视图：正面 / 左侧面 / 右侧面 / 背面（三视图）
    "character_views": [
        {"key": "front", "label": "正面全身", "azimuth": "front view", "elevation": "eye-level", "distance": "full-body shot"},
        {"key": "left", "label": "左侧半侧面", "azimuth": "quarter view from left", "elevation": "eye-level", "distance": "full-body shot"},
        {"key": "right", "label": "右侧半侧面", "azimuth": "quarter view from right", "elevation": "eye-level", "distance": "full-body shot"},
        {"key": "back", "label": "背面全身", "azimuth": "back view", "elevation": "eye-level", "distance": "full-body shot"},
    ],
    # 物品/场景 3D 多视角：正面 / 左45° / 右45° / 俯视（3D环绕）
    "item_scene_views": [
        {"key": "front", "label": "正面视角", "azimuth": "front view", "elevation": "eye-level", "distance": "medium shot"},
        {"key": "left45", "label": "左前45°视角", "azimuth": "quarter view from left", "elevation": "eye-level", "distance": "medium shot"},
        {"key": "right45", "label": "右前45°视角", "azimuth": "quarter view from right", "elevation": "eye-level", "distance": "medium shot"},
        {"key": "top", "label": "俯视视角", "azimuth": "front view", "elevation": "high-angle shot", "distance": "wide shot"},
    ],
}

# ===================== 正负提示词冲突清理（P0：风格冲突修复） =====================
# 物品生成.json / 分镜生成.json 的负向词表含「3D渲染、二次元动漫」，场景生成.json 含「3D」，
# 而正向提示词要求「国漫3D渲染风格」——正负自相矛盾会把 3D 风格压掉（画面风格撕裂）。
# 提交前从负向槽位剔除这些词（按长度降序匹配，避免「3D渲染」被「3D」提前截断）。
CONFLICT_NEGATIVE_TOKENS = ("3D渲染", "二次元动漫", "3D动漫", "3D渲染风格", "3D")

DEFAULT_PARAMS = {
    "resolution": "768p_vertical",  # 768p_vertical, 768p_horizontal, 480p
    "steps": 8,  # Turbo LoRA 默认 8 步
    "seed": -1,  # -1 表示随机
    "reference_strength": 0.9,  # 参考强度
    "fps": 24,
    "duration_per_shot": 5,  # 每个镜头默认 5 秒
}

# 创建必要的目录
def ensure_dirs():
    for d in [PROJECT_OUTPUT_DIR, SCRIPT_DIR, ASSETS_DIR, CHARACTERS_DIR,
              ITEMS_DIR, SCENES_DIR, STORYBOARDS_DIR, VIDEOS_DIR, FINAL_DIR, NOVELS_DIR, QC_DIR,
              AI_CHAT_DIR, UPSCALE_DIR, DUB_DIR, DUB_MIX_DIR, PROJECTS_DIR, PROJECT_TRASH_DIR,
              CONTINUITY_DIR]:
        os.makedirs(d, exist_ok=True)

ensure_dirs()
