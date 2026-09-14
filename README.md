# 本地漫剧自动化生成系统

基于 **云端 LLM + ComfyUI（Qwen 2512/2511 + MiniMax H3 + FlashVSR）** 的完整 AI 漫剧生成流程

## 🎯 系统架构

```
┌────────────────────────────────────────────────────────────────────┐
│                       云端 LLM（Claude API）                        │
│   输入：故事主题 → 输出：结构化 JSON 剧本                            │
│   （角色 / 物品 / 场景 / 镜头 分镜 + 各类生成提示词）                 │
└────────────────────────────────┬───────────────────────────────────┘
                                 ↓
┌────────────────────────────────────────────────────────────────────┐
│                     ComfyUI 本地执行（三阶段资产管线）                │
│                                                                    │
│  ┌───────────────────────── 图片资产（三类） ─────────────────────┐  │
│  │                                                              │  │
│  │  角色 Characters                    物品 Items               │  │
│  │  ┌──────────┐   ┌──────────────┐   ┌──────────┐              │  │
│  │  │Qwen 2512 │ → │Qwen Edit 2511│   │Qwen 2512 │              │  │
│  │  │基础图生成 │   │多视图生成     │   │基础图生成 │              │  │
│  │  └──────────┘   └──────────────┘   └────┬─────┘              │  │
│  │   正面/左侧/右侧/背面              ┌────▼──────────────┐       │  │
│  │                                   │Qwen Edit 2511     │       │  │
│  │   场景 Scenes                     │3D多视角生成        │       │  │
│  │   ┌──────────┐   ┌──────────────┐ └───────────────────┘       │  │
│  │   │Qwen 2512 │ → │Qwen Edit 2511│  正面/左45°/右45°/俯视      │  │
│  │   │基础图生成 │   │3D多视角生成  │  （物品和场景共用此管线）    │  │
│  │   └──────────┘   └──────────────┘                              │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─────────────────────── 视频生成 ────────────────────────────┐   │
│  │  MiniMax H3 (Ref2VA) 动态段数无缝视频生成                     │   │
│  │  三种模式：逐镜头 / 整集一次 / 关键帧驱动（首尾帧插值）        │   │
│  │  参考图（分镜图+角色锚点 / 首帧+尾帧）→ 视频 + 原生音频        │   │
│  │  Turbo LoRA 8步加速 + H3ContinuousSeamlessJoin 无缝拼接      │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                    │
│  ┌─────────────────────── 后期处理 ────────────────────────────┐   │
│  │  FlashVSR 4x 超分（768p → 2K/4K）→ FFmpeg 合并 + 字幕       │   │
│  └─────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────┘
```

## 🆕 能力增强（对标 GitHub 开源漫剧项目后的优化落地）

本节记录相对初版的实质性增强。所有条目均已在本机实测通过（含真实模型调用）。

### 工程底座

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 配置外置 | `app/config.py` + `.env.example` | 移除全部硬编码路径，`COMFYUI_ROOT` 单点配置自动推导 workflows/input/output/models；换机只改 `.env` |
| 部署档案 | `DEPLOY_PROFILE` | `8G` / `16G` / `server` 三档，影响超分与视频默认档位 |
| 密钥加密 | `app/secret_store.py` | Fernet 对称加密；优先级 环境变量 > 加密库 `output/secrets.enc` > json 明文（首次读取**自动迁移**）；加密不可用时拒绝明文落盘 |
| 持久化任务队列 | `app/task_store.py` | SQLite（`output/tasks.db`）+ 单元级进度 + 串行队列；启动自动 `recycle_interrupted()` |
| 断点续跑 | `is_unit_done` / `filter_pending_units` | 判据以**磁盘产物**为准——产物存在且非空即视为完成，状态表丢失也能正确跳过 |
| 统一环境加载 | `app/env_loader.py` | 任一模块单独导入（不经过 `config`）也能读到 `.env`，避免「换入口就丢密钥」 |

### 质量与一致性

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 跨镜头一致性校验 | `app/consistency.py` | 双通道：感知哈希（aHash/dHash，零依赖快筛）+ 多模态语义判定「是否同一角色」；输出差异点 |
| 一致性看板 | 步骤9 · 一致性看板 | 报告落盘 `output/continuity/<项目>/consistency.json`，按镜头/角色展示相似度与判定 |
| 原文承载归属 | `_shot_coverage_map` | 每个镜头承载了原文哪几句（4-gram 字面比对，与 `coverage.py` 同口径），画布上直接可见 |
| 单镜重跑 | `/api/storyboard/retry-shot`、`/api/video/retry-shot` | 只影响目标镜头，不触碰其它产物；分镜重跑同样受质检闸门约束 |

### 生成可控性

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 关键帧驱动视频 | `app/keyframe.py` + 步骤6 | 先用分镜图生成「尾帧」，再把 `[首帧, 尾帧]` 注入 H3 参考图槽位，让运动在两端之间插值；缺尾帧自动退化为首帧单锚并标注 |
| 可视化分镜画布 | 步骤9 · 分镜画布 | 卡片展示分镜图/视频/尾帧/质检分/一致性分/承载原文；拖拽排序写回剧本 `metadata.shot_order` |
| 引擎 Provider 抽象 | `app/providers/` | `ImageProvider` / `VideoProvider` / `TTSProvider` 三接口；本地 ComfyUI 实现 + 云端预留（豆包/Wan/Kling/Vidu/Veo/CosyVoice）；`MJSCXT_PROVIDER_*` 切换 |
| 插件化扩展 | `app/plugin_registry.py` + `app/plugins/` | 生产环节抽象为可注册插件（含依赖拓扑排序）；放入 `app/plugins/*.py` 暴露 `register(reg)` 即被自动加载 |

### 生态与交付

| 能力 | 落地位置 | 说明 |
|------|---------|------|
| 剪映草稿导出 | `app/nle_export.py` | `draft_content.json` + `draft_meta_info.json`，可直接在剪映打开继续精修 |
| Premiere 导出 | `nle_export.export_fcpxml` | FCPXML，供 Premiere / Final Cut |
| 字幕与帧清单 | `nle_export.export_srt` / `export_frames` | SRT 字幕 + 帧序列清单 |
| 成本与耗时看板 | `app/analytics.py` + 步骤9 | 任务完成钩子自动登记耗时；ComfyUI 调用计数（提交/失败/等待秒数/生成段数）；按部署档案估算电费 |
| 国际化 | `locales/zh-CN.json` / `en-US.json` + `/api/i18n` | 前端 `data-i18n` 键值替换，顶栏可切换 |
| Docker | `Dockerfile` + `docker-compose.yml` | 只容器化后端（ComfyUI 依赖 GPU 与数十 GB 权重，留在宿主机）；产物与任务库挂载持久化 |
| 桌面端 | `deploy/desktop/` | Electron 外壳，自动编排 ComfyUI + 后端双进程并在退出时回收 |

### 顺带修复的真实缺陷

| 缺陷 | 症状 | 修复 |
|------|------|------|
| ComfyUI 状态探测超时过长 | ComfyUI 离线时 `/api/status` 卡死约 4 分钟 | 3s 短超时 + `(connect, read)` 元组 |
| 任务库全局锁自锁 | `import app` 挂起约 2 分钟 | `threading.Lock` → `RLock` |
| 分镜图错配 | manifest 只记录部分镜头时，缺项镜头会**错配到别的镜头的分镜图**当关键帧首帧 | `_keyframe_sb_map` 改为合并式映射；`plan_keyframes` 取消数组下标兜底 |
| 嵌套明文密钥 | `qc_config.json` 的 `endpoint_override.api_key` 未被迁移，仍为明文 | `secret_store` 递归处理嵌套结构 |
| 单独导入即失败 | 只 `import qc_client` 时 `.env` 未加载，主密钥取不到、解密失败 | 新增 `env_loader.py` 统一加载 |
| 参考图静默丢失 | 前端传结构不完整的角色对象时，视频退化为无角色锚点 | 后端 `_collect_asset_refs` 从磁盘资产兜底 |

> ⚠️ **密钥提醒**：若 `ai_config.json` / `qc_config.json` 曾以明文形式进过 git、云盘同步或被分享，
> 请到对应平台**轮换密钥**——加密只防未来，已暴露的无法追回。

详见 [`优化方案_对标GitHub开源漫剧项目.md`](优化方案_对标GitHub开源漫剧项目.md) 与 [`deploy/README.md`](deploy/README.md)。

## 📦 图片资产三类规范

### 1. 角色 Characters（多视图）
| 视图 | 文件名 | 说明 |
|------|--------|------|
| 基础图 | `base.png` | Qwen 2512 文生图，含完整外貌描述 |
| 正面 | `front.png` | 全身正面，eye-level |
| 左侧 | `left.png` | 左侧半侧面全身 |
| 右侧 | `right.png` | 右侧半侧面全身 |
| 背面 | `back.png` | 背面全身 |

**用途**：H3 视频生成的角色锁定参考图（Ref2VA 模式）

### 2. 物品 Items（3D 多视角）
| 视图 | 文件名 | 说明 |
|------|--------|------|
| 基础图 | `base.png` | Qwen 2512 文生图，3D渲染白底展示 |
| 正面 | `front.png` | 正面视角 |
| 左前45° | `left45.png` | 左前45°视角 |
| 右前45° | `right45.png` | 右前45°视角 |
| 俯视 | `top.png` | 高角度俯视 |

**用途**：武器、法宝、关键道具的资产库，供分镜图和视频参考

### 3. 场景 Scenes（3D 多视角）
| 视图 | 文件名 | 说明 |
|------|--------|------|
| 基础图 | `base.png` | Qwen 2512 文生图，含构图光影 |
| 正面 | `front.png` | 正面视角 |
| 左前45° | `left45.png` | 左前45°视角 |
| 右前45° | `right45.png` | 右前45°视角 |
| 俯视 | `top.png` | 高角度俯视全景 |

**用途**：场景氛围锁定，H3 视频生成的环境参考图

### 资产目录结构
```
output/
├── scripts/                    # 剧本 JSON
│   └── 修仙少年复仇记_20260909.json
├── assets/
│   ├── characters/             # 角色资产
│   │   └── {项目名}/
│   │       └── {角色名}/
│   │           ├── base.png    # 基础图
│   │           ├── front.png   # 正面
│   │           ├── left.png    # 左侧
│   │           ├── right.png   # 右侧
│   │           └── back.png    # 背面
│   ├── items/                  # 物品资产
│   │   └── {项目名}/
│   │       └── {物品名}/
│   │           ├── base.png
│   │           ├── front.png
│   │           ├── left45.png
│   │           ├── right45.png
│   │           └── top.png
│   └── scenes/                 # 场景资产
│       └── {项目名}/
│           └── {场景名}/
│               ├── base.png
│               ├── front.png
│               ├── left45.png
│               ├── right45.png
│               └── top.png
├── videos/                     # 视频片段
│   └── {项目名}/shot_01.mp4 ...
└── final/                      # 最终成片
    └── {项目名}/{项目名}.mp4
```

## 🔄 完整流程（6 步）

| 步骤 | 功能 | 技术 | 工作流模板 |
|------|------|------|-----------|
| 1. 剧本生成 | 主题 → 结构化 JSON（角色/物品/场景/镜头） | Claude API（云端） | — |
| 2. 角色资产 | 基础图 + 多视图（正/左/右/背） | Qwen 2512 + Qwen Edit 2511 | 角色生成.json + 分镜生成.json |
| 3. 物品资产 | 基础图 + 3D多视角（正/左45/右45/俯视） | Qwen 2512 + Qwen Edit 2511 | 物品生成.json + 分镜生成.json |
| 4. 场景资产 | 基础图 + 3D多视角（正/左45/右45/俯视） | Qwen 2512 + Qwen Edit 2511 | 场景生成.json + 分镜生成.json |
| 5. 视频生成 | 10段无缝视频 + 原生音频 | MiniMax H3 (Ref2VA) + Turbo LoRA | H3信号10段测试001.json |
| 6. 成片输出 | 合并 + 字幕 | FFmpeg | — |

## 📝 剧本 JSON 格式

```json
{
  "title": "修仙少年复仇记",
  "characters": [
    {
      "name": "林风",
      "appearance": "18岁少年，黑发束起，左眉有伤疤，青色道袍",
      "reference_prompt_zh": "18岁古风少年，正面全身像，黑发束起..."
    }
  ],
  "items": [
    {
      "name": "青霜剑",
      "category": "武器",
      "reference_prompt_zh": "古风长剑，3D渲染白底展示，剑身青色..."
    }
  ],
  "scenes": [
    {
      "name": "云霄宗山门",
      "location": "室外",
      "reference_prompt_zh": "仙侠风山门，云雾缭绕，石阶向上..."
    }
  ],
  "shots": [
    {
      "shot_id": 1,
      "duration": 5,
      "camera": "中景",
      "description": "林风站在悬崖边俯瞰宗门",
      "dialogue": "三年了，我回来了。",
      "prompt_h3": "A young man stands at cliff edge...",
      "characters_in_shot": ["林风"],
      "items_in_shot": ["青霜剑"]
    }
  ]
}
```

## 🚀 快速开始

### 1. 启动 ComfyUI（手动）
```batch
cd D:\ComfyUI_portable_TE_v260619
.\ComfyUI_windows_portable.exe
```

### 2. 设置环境变量并启动应用
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-xxx..."
cd "C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\app"
pip install -r requirements.txt
python app.py
```

或直接双击 `run_app.bat`

### 3. 访问 Web 界面
打开 **http://localhost:5000**

### 4. 操作流程
1. 输入故事主题 → 点击"生成剧本"（云端 LLM）
2. 点击"生成角色资产" → 自动生成角色多视图
3. 点击"生成物品资产" → 自动生成物品 3D 多视角
4. 点击"生成场景资产" → 自动生成场景 3D 多视角
5. 点击"开始生成视频" → H3 批量生成视频片段
6. 点击"生成最终视频" → FFmpeg 合并 + 字幕

## 📦 环境要求

| 项目 | 要求 |
|------|------|
| GPU | RTX 3060 12GB+（推荐 4080 16GB+） |
| 内存 | 32GB+ |
| 硬盘 | SSD 200GB+ |
| ComfyUI | ≥ 0.30.0（用户自启动） |
| Python | 3.10+ |
| FFmpeg | 已安装并加入 PATH |
| Claude API Key | 必需（剧本生成） |

## 🔧 已安装模型与节点

### 模型
```
models/diffusion_models/
├── qwen-image-2512/qwen_image_2512_fp8_e4m3fn.safetensors    # 图像生成
├── qwen-image/qwen_image_edit_2511_bf16.safetensors           # 图像编辑（多视角）
└── minimax-h3/minimax_h3_ref2va_pruned_int8_convrot.safetensors # 视频生成

models/loras/
├── Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors     # 2512 加速
├── Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors # 2511 加速
├── Qwen-Image-Edit-2511-Multiple-Angles-LoRA.safetensors       # 多角度生成
└── minimax_h3/minimax_h3_fl2v_lightx2v_turbo_4step_v0.1.safetensors # H3 加速

models/FlashVSR-v1.1/                                           # 超分辨率
```

### 自定义节点
```
custom_nodes/
├── Herrgotts-H3-Infinite-Continuation-Suite-main/  # H3 无缝续集拼接
├── ComfyUI-qwenmultiangle/                         # 3D 相机角度控制
├── ComfyUI-MiniMax-H3-Turbo/                       # H3 Turbo 加速
├── TE-Speed-MiniMaxH3 / TE-Speed-FlashVSR          # TE 加速
├── ComfyUI-Qwen-TTS/                               # 本地配音（可选）
└── ComfyUI-FlashVSR / ComfyUI-FlashVSR_Ultra_Fast  # 超分节点
```

## ⚡ 性能优化

| 优化项 | 配置 | 效果 |
|--------|------|------|
| H3 Turbo LoRA | 8步（默认20步） | 速度提升 2-3x |
| Qwen Lightning LoRA | 4步 | 图像生成加速 |
| TE-Speed-MiniMaxH3 | 已启用 | TE 编码加速 45% |
| SageAttention | 已启用 | 注意力计算加速 |
| MiniMaxLowVRAMAttention | head_chunks=4 | 低显存支持 |

## 🔗 相关资源

- [MiniMax H3 本地部署](https://platform.minimaxi.com/docs/guides/local-deploy-h3)
- [FlashVSR](https://github.com/OpenImagingLab/FlashVSR)
- [Qwen-Image-Edit-2511-Multiple-Angles-LoRA](https://huggingface.co/fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA)
- [ComfyUI-qwenmultiangle](https://github.com/jtydhr88/ComfyUI-qwenmultiangle)

---

**版本**: 2.0.0 | **日期**: 2026-09-09
