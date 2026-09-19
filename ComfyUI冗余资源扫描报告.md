# ComfyUI 冗余资源扫描报告

**扫描时间**：2026-09-19 10:40
**执行时间**：2026-09-19 10:42
**ComfyUI 根目录**：`D:/ComfyUI_portable_TE_v260619`
**项目**：`C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统`

> ✅ 已执行（用户确认后）。**采用「移动到隔离目录」而非删除** —— 同盘移动瞬时完成、可整批移回。
> 隔离目录：`D:/ComfyUI_portable_TE_v260619/_cleanup_quarantine_20260919/`
> 还原清单：该目录下 `_manifest.json`
> **实际可释放 44.41 GB / 95 项**。验证 ComfyUI 正常后删除隔离目录即可回收空间。

---

## 一、本项目实际用到的工作流（6 个）

来源：`app/config.py` 的 `WORKFLOW_TEMPLATE` + 超分工作流名。

| 用途 | 工作流文件 |
|---|---|
| H3 视频生成（整集/逐镜） | `H3信号10段测试001.json` |
| 角色 / 物品 / 场景基础图 | `角色生成.json` / `物品生成.json` / `场景生成.json` |
| 分镜图 + 多视角 | `分镜生成.json` |
| 视频超分 | `TE-Speed-flashVSR 视频超分放大加速工作流.json` |

工作流目录：`D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\user\default\workflows`

---

## 二、✅ 已隔离（逐项验证）—— 实际可释放 44.41 GB

| # | 隔离路径 | 大小 | 验证依据 |
|---|---|---|---|
| 1 | `models/diffusion_models/FlashVSR-v1.1/` | **6.63 GB** | 5 个文件 md5 与 `models/FlashVSR-v1.1/` 逐字节相同；插件 `ComfyUI-FlashVSR_Ultra_Fast/nodes.py` 用 `os.path.join(folder_paths.models_dir,"FlashVSR-v1.1")` 加载 → 走 `models/FlashVSR-v1.1/`；无工作流按目录引用 |
| 2 | `models/loras/` 下 **94 个**文件 | **37.94 GB** | 本项目 6 个工作流只引用 4 个 lora；其余未被引用（含 KJ 那份与 `minimax_h3/` 逐字节相同的重复 lora） |

**保留校验（执行后复核，全部在原件）**：
- 4 个必需的 lora：`minimax_h3/minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy`(1866 MB)、
  `Qwen-Image-2512-Lightning-4steps-V1.0-fp32`(1620 MB)、
  `Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16`(810 MB)、`新建文件夹/zi-base-gm_male_20`(225 MB)
- FlashVSR 正本：`models/FlashVSR-v1.1/` 的 5 个文件全在
- 核心模型：`diffusion_models/minimax-h3/minimax_h3_ref2va_pruned_int8_convrot`、
  `text_encoders/minimax-h3/qwen3vl_32b_minimax_h3_nvfp4_awq`、`models/qwen-tts/` 均完好

### ⚠️ 一处修正：`vae/MiniMax` 不是「多余副本」，是**硬链接**

原先我把它列为可清理 5.5 GB —— **这是错的**。用 `os.stat().st_nlink` 查证：

```
models/vae/minimax-h3/…video_vae_fp16.safetensors   st_nlink=2  inode=1407374884027428
_cleanup_quarantine/vae/MiniMax/…video_vae_fp16     st_nlink=2  inode=1407374884027428  ← 同一 inode
```

两者是**硬链接，共享同一份物理数据**，删任何一方都不释放空间
（对比：loras 里的文件 `st_nlink=1`，是真副本）。
所以**已把它移回原位**，隔离目录不含它 —— 免得让你误以为删隔离目录能省 49.8 GB。

> 📌 教训：判断「重复文件」不能只看 **大小**，也不能只看 **md5** ——
> 硬链接的 md5 必然相同。**必须查 `st_nlink`/inode**，否则会把「零成本别名」当成可回收空间。

---

## 三、未处理（按你的决定保留）

### 3.1 其它大目录（本项目代码不引用，但被别的插件/服务使用）

| 目录 | 体积 | 用途 |
|---|---|---|
| `models/LLM` | 26.8 GB | .gguf 大模型，TE 的「LLM 反推」类工作流 |
| `models/qwen-tts` | 18.4 GB | QwenTTS 语音合成（`tts_client.py` 链路依赖） |
| `models/stt` | 2.9 GB | 语音转文字（Whisper 插件） |
| `models/sams` | 358 MB | SAM 分割（SAM3DBody 插件） |

### 3.2 插件：78 个 —— **最终结论：不要删**（但权威清单如下）

#### 为什么之前判不出来
TE 系列插件（`TE_MAN`/`TE-Speed-*`）是**编译过的 `.pyd`**（`TE_MAN/api_client.pyd`、
`Gemini_Imagen_Generator.pyd` …），节点注册在二进制里，任何源码分析都看不到；
翻译类插件（`ComfyUI-DD-Translation`）含大量节点名字符串，字符串 grep 会严重误报
（实测把 78 个里 60 个判成"需要"）。

#### 权威办法（ComfyUI 启动后查 `/object_info`）
每个节点都带 `python_module`，这就是「节点 → 插件」的权威映射。
本次 ComfyUI 启动后（4201 个节点全部带该字段）已跑出结果。

#### ✅ 本项目**必须保留**的 13 个插件

| # | 插件 | 提供的本项目节点 |
|---|---|---|
| 1 | `Herrgotts-H3-Infinite-Continuation-Suite-main` | `H3Continuous*` × 6（整集无缝衔接，**核心**） |
| 2 | `TE-Speed-MiniMaxH3` | `TESpeedMiniMaxH3`（H3 加速） |
| 3 | `MiniMax-Optimization-Stubs` | `Label (rgthree)` / `MiniMaxChunkFeedForward` / `MiniMaxLowVRAMAttention` |
| 4 | `TE-Speed-FlashVSR-backup` | `TEFlashVSR{ModelLoader,Restore,Tuning}` / `TESpeedVideoCombine`（超分） |
| 5 | `ComfyUI-FlashVSR_Ultra_Fast` | `FlashVSRInitPipe` / `FlashVSRNodeAdv`（超分） |
| 6 | `Comfyui-XH_H3MemorySaver` | `XHImagePrecision`（显存/精度） |
| 7 | `ComfyUI-KJNodes` | `PathchSageAttentionKJ` |
| 8 | `ComfyUI-SolAttn_triton` | `SolAttnPatch` |
| 9 | `ComfyUI-VideoHelperSuite` | `VHS_*` × 4 |
| 10 | `was-node-suite-comfyui` | `Text Multiline` |
| 11 | `audio-separation-nodes-comfyui` | `AudioSeparation` / `AudioCombine`（**音效分离**，`sfx_isolate.py` 用） |
| 12 | **`ComfyUI-Qwen-TTS`** | `FB_Qwen3TTS*` × 9（**配音链路的真实实现**，`tts_client.py` 用） |
| 13 | `ComfyUI-GGUF` | （不是给本项目用，但**被 `ComfyUI-KJNodes` 引用**，删了会连带出错） |

#### ⚠️ 这个清单本身也**不能**当作删除依据 —— 有两处已证实的盲点

我最初只用「6 个模板工作流」推节点，结果漏掉了两类**代码里动态构建**的关键节点：

1. `sfx_isolate.py` 硬编码 `LoadAudio → AudioSeparation → AudioCombine → SaveAudio`
   （音效分离，靠 `"class_type"` 正则才捞到）；
2. `tts_client.py` 的**配音**走的是 ComfyUI 的 `FB_Qwen3TTS*` 节点 ——
   这个靠正则**没捞到**，是我读它的 docstring 才发现的。

也就是说：「**本项目没用这个插件**」这个判断本身就不可靠，更别说推导
「你可以删」。再加上插件总体积仅约 **4 GB**（多数 < 100 MB），
**收益远小于风险**（删错一个可能导致 ComfyUI 启动时整片插件 IMPORT FAILED）。
所以：**插件一个都不动**。上面的 13 个清单留作参考，方便你日后排查。

---

## 四、后续

1. **建议启动 ComfyUI 跑一遍本项目生产**，确认模型/插件都还在（尤其是 H3 与超分链路）；
2. 确认无误后，删除 `D:/ComfyUI_portable_TE_v260619/_cleanup_quarantine_20260919/`
   即可回收 **44.41 GB**；
3. 若发现问题，按 `_manifest.json` 整批移回即可完全还原；
4. 插件要清理的话，先启动 ComfyUI，我用 `/object_info` 出权威清单。

---

## 五、扫描方法（可复现）

- 工作流模型引用：解析 6 个 + 全部 25 个工作流 JSON 的**所有字符串**，
  过滤模型扩展名（`.safetensors/.ckpt/.pt/.pth/.bin/.onnx/.gguf/.sft`），
  **排除 HuggingFace 下载链接**（MarkdownNote 里的说明文字，不是真实路径）
- 重复判定：`md5sum` 逐字节比对 + **`os.stat().st_nlink` 查硬链接**
- 引用核实：在全部工作流 JSON 里 grep 具体目录路径
- 移动执行：分批 10 个/批，每批前后比对字节数；全过程记录到 `_manifest.json`
- 中间产物：`.workbuddy/test/_out/comfyui-usage.json`、`comfyui-plugin-ast.json`

