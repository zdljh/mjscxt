# 提示词生成规范（分镜图 · Qwen-Image-2.1）

> 目标：**从源头减少质检重跑**。分镜图是重跑重灾区（教训库 68/73 条），其中「景别」
> 占 45 条。根因是扁平长提示词里硬约束被画面内容稀释。
>
> **2026-09-25 改版**：图片链路切到 QwenImage2.1 后，提示词协议从中文分节（【取景】…）
> 换成官方 Prompt Rewriter 的 **`<imageN>` 英文协议**。本规范由
> `comfyui_client.build_storyboard_prompt` 单一实现，守卫 `verify_storyboard_prompt_spec.py`
> 与质检层 `prompt_qc._check_storyboard` 共同锁定。

## 依据（Qwen-Image-2.1 官方规范）

1. **`<image1>…<imageN>` 显式编号是硬要求**。官方明确禁用「第一张图 / 图 A / 左边那张」
   这类自然语言指代 —— 会产生歧义。编号即输入顺序（`images.image_N` 槽位序号）。
2. **`<image1>` 是 major canvas / edit target**。官方 ComfyUI Image Edit workflow 里
   `image_1` 就是主编辑目标，输出画布也跟随它 → 项目里 `image1` 固定放**主角色身份锚点**。
3. **属性解耦（Attribute Disentanglement）**：官方结构是
   `IDENTITY → CHANGE → SOURCE → PRESERVE`，把「谁是画布 / 谁提供身份 / 这次改什么 /
   什么必须不变」拆成各自独立的句子。
4. ⚠️ **身份一律指向参考图，禁止用文字重述五官**。官方的关键结论：一旦重新描述
   脸型/眼睛/鼻子，任务语义就从「保持这个人」变成「重新画一张符合这些描述的人」，
   反而**降低 likeness**。
5. **Preservation Clause 用 blanket 写法**：`Keep all untargeted content unchanged.`
   不逐项罗列（每重述一次都可能重新触发生成）。
6. **官方容量上限 10 张，但「容量不是目标」**。每张参考图解决一个明确问题；
   多余槽位必须留空，塞重复/无关图会让模型分不清优先级。项目取 **9 槽**。
7. **画幅不写进提示词正文**。官方 Rewriter 单独返回 `wh_ratio` / `ratio_follow` ——
   它属于 generation parameter，由工作流尺寸节点与参考图画幅落实。

## 分节顺序（固定，不可调换）

| 顺序 | 段名 | 内容 | 是否可选 | 硬约束等级 |
|------|------|------|---------|-----------|
| ① | `TASK:` | 景别（FRAMING）+ 机位（CAMERA ANGLE） | 景别可「未指定」 | **最高**（最靠前） |
| ② | `PRIMARY CANVAS:` | 哪张图作主要画布 / 编辑目标 | 有参考图必填 | 高 |
| ③ | `IDENTITY:` | 身份锚点（指向 `<imageN>`，不复述五官） | 有角色参考图时必填 | **高** |
| ④ | `REFERENCE ROLES:` | 每张参考图**唯一**职责（`Use <imageN> only for …`） | 有参考图必填 | 高 |
| ⑤ | `SCENE AND ACTION:` | 场景 / 动作与画面内容 / 说话状态 / 情绪 | 至少含动作 | 中 |
| ⑥ | `LIGHTING:` | 光影氛围（光源引导） | 可选（幂等去重） | 低 |
| ⑦ | `STYLE:` | 画面风格声明（英文，`style_kit.style_suffix_en`） | 必填（无风格给兜底） | 高 |
| ⑧ | `PRESERVE:` | blanket 保留子句 + 无文字硬禁令 | 必填（最后兜底） | 高 |

## 参考图槽位分工（9 槽，与 `<imageN>` 编号一一对应）

| 槽位 | 编号 | 职责 | 来源 |
|------|------|------|------|
| 1 | `<image1>` | 主角色身份锚点（兼画布/构图基线） | `characters_in_shot[0]` |
| 2 | `<image2>` | 次角色身份（独立，禁止特征串味） | `characters_in_shot[1]` |
| 3 | `<image3>` | 第三角色身份 | `characters_in_shot[2]` |
| 4+ | `<image4>`+ | 场景环境与氛围，随后是道具（形状/材质/配色，最多 2 张） | `location` → 场景资产；`items_in_shot` |

> ⚠️ **编号必须按位置重新编号**，不能照抄 label 里的数字：label 生成时会带「预留槽位号」，
> 但某镜若只有 1 个角色，场景实际落在第 2 槽而非第 4 槽。照抄会让提示词引用一个
> **根本没连图**的槽位 → 身份/场景约束静默失效。见 `build_storyboard_prompt` 的
> `for pos, raw in enumerate(...)`。
>
> ⚠️ 未用到的尾部槽位在生成端**留空**（`generate_storyboard` 的 `slot_cleared`），
> 不复用锚点图 —— 官方「容量不是目标」。（紧凑模板 ≤4 槽才沿用复用行为。）

## 景别对档：角色参考图按镜头景别选「半身档 / 全身档」（2026-09-25）

### 根因

`<image1>` 是主画布，其**画幅会牵引输出取景**。原角色资产是 736×736 近方形**全身**立绘，
cover 到 9:16 竖屏（544×960）要**左右各裁一半** → 模型为保住立绘里「完整的全身」
只能把人物缩小 → **系统性偏全景/远景**；而近景/特写要求「只拍局部」，与立绘
「保全身」方向相反 → 被反向拉回，**画不出来**。

实测（`.workbuddy/tools/diag_framing_control.py`，项目「逆天系统」）：

| 镜头 | camera | 实际出图 | 质检结论 |
|------|--------|---------|---------|
| shot_01 | 远景 | 远景 | 通过（与立绘同向） |
| shot_06 | 中景 | 偏全景 | 通过（容差放行） |
| shot_24 | **近景** | **近全身** | **通过**（容差放行） |

两个放大器：① 提示词里的景别约束是**否定式**（「严禁退为全景」），遵从度低于正向描述；
② 质检容差「差一档不算缺陷」把系统性偏差掩盖成偶发。

### 对策

角色设定图由「三张全身横排」→ **「上排 3 全身 + 下排 1 半身胸像」两层版式**，
新增 `half` 档；分镜端按镜头景别取对应档作 `<image1>`：

| 景别 | 取哪档 | 理由 |
|------|--------|------|
| 特写 / 近景 / 中景 | `half.png`（正面半身胸像） | 画幅与景别**同向**，画幅对抗消失 |
| 全景 / 远景 | `front.png`（全身） | 与「拍全身」同向 |
| **未指定**（camera 只有机位/运镜） | `front.png` | 不猜档位；全身档是画幅对抗最小的默认 |

实现见 `app._framing_wants_half` / `app._pick_char_view` / `app._allocate_storyboard_refs`。

> ⚠️ **`_framing_wants_half` 必须先挡空值**：`comfyui_client.camera_key('')` 会返回
> `'中景'`（那是给生成端用的默认档，**不是**「本镜是中景」）。直接复用会把
> 「景别未指定」误判成中景 → 静默换档。空值必须提前 return False。

> ⚠️ **`_pick_char_view` 必须优雅降级**：`half.png` 是 2026-09-25 起才有的新档，
> 存量项目只有 `front.png`/`base.png`。找不到目标档时**逐级回退**
> （要半身 → half → front → base；要全身 → front → base → half），
> **绝不返回空** —— 返回空会让该镜头判成「无可用参考图」而 400，
> 把「档位缺失」升级成「不能出图」。

### 切分几何（`app/sheet_split.py`）

半身档必须放**独立的下排横带**，不能塞进真正的 2×2 网格：2×2 里半身的 y 区间与
上排全身**重叠** → 行投影把上下排粘成一段（合成图与真实图都实测过）→ 切分不可靠。
下排独占横带后，行投影稳定得到 2 段、行内列投影得到各排格数，两向都不碰内容。
行投影失败时用 `CHARACTER_SHEET_HALF_BAND`（0.42）做**确定性兜底**（硬切在 58% 高度）。

口径常量见 `config.CHARACTER_SHEET_LAYOUT_ZH / VIEWS / GRID / CELLS / HALF_BAND /
CHARACTER_HALF_VIEWS / ASSET_VIEW_STEMS` —— 三处（config / 提示词 / 切分）**必须同序**，
`verify_sheet_split_views.py` 的 D 段与 `verify_character_sheet_ratio.py` 的 G3/G4 段锁定。

## 视频层提示词（H3：生成段切分 · 4 秒可信窗）

> 分镜图是「一镜一张图」，视频层是「一镜一段片」。2026-09-25 按短剧行业实践引入
> **4 秒可信窗**约束：AI 视频单次生成**超过约 4 秒后画面开始崩坏**（结构/身份/物理
> 一致性迅速劣化），行业通行做法是**单次生成 ≤ 4 秒，长镜头切成多段再拼**。

### 切分规则（`h3_prompt_kit.segment_durations`）

- 单镜时长 ≤ `H3_SEGMENT_MAX_SEC`（4.0s）→ **原样返回**（不做任何取整，比特不变）。
- 超过 → `n = ceil(dur / 4.0)`，各段等长取 `dur / n`。
- ⚠️ **总时长严格守恒**（`sum(各段) == 原时长`）。所以**配音时间轴 / 成片时长完全不受影响**，
  下游 `dub_mix.shot_timeline`、`probe_video`、`_SHOT_RE` 的既有契约一律不动。
- 若等分后某段短于 `H3_SEGMENT_MIN_SEC`（1.5s）→ **减少段数** `n -= 1`，避免出现
  「1.5 秒都不到」的残缺尾段（n 降到 1 为止，此时宁可不切）。

### 单段产物不变（关键设计）

N 个子段提交给 `comfyui_client.generate_h3_sequence(segments=[...])`，由 H3
**原生无缝拼接**成**一个**连续视频文件 → 磁盘上仍是**单个 `shot_XX.mp4`**。
整个文件名契约（`probe_video` / `dub_mix.shot_timeline` / `video_postprocess._SHOT_RE`）
**零改动**。这是刻意选择：否则要动整条文件命名链路。

### 每段必须重建提示词（`segment_shot`）

- **按子段时长**重算节拍（否则 4 秒的段拿到 12 秒的节拍 → 大量空镜死时间）。
- ⭐ **台词只留最后一段**：H3 每段独立生成（都带音轨），若每段都带台词，
  同一句话会被说 N 遍。非末段的 `dialogue` / `dialogue_text` / `narration` **清空**，
  `description` 降级为过场子句（「动作自上一刻继续推进…」），`visual_detail` 清空。

### 运镜注入（`_camera_move_en` / `movement_hint`）

历史缺陷：**景别注入成功、运镜却被丢弃** —— 分镜图提示词只有「景别」没有「怎么动」。
对策：`_CAMERA_MOVE_EN` 词典把「跟拍/推入/升降/环绕/摇拍/拉远/急推/固定/手持/俯拍/仰拍…」
映射成英文运镜子句。

- ⚠️ **最长词优先**匹配（`升降` 必须先于 `升` 命中，否则 `升` 会吃掉 `升降` 的词头）。
- ⭐ **运镜只写一次**，注入 `[Shot 1]` 抬头：
  `[Shot 1] A medium shot, the camera tracks the subject: …`
  绝不在句尾重复（重复会让模型理解成「运镜两次」）。
- ⚠️ **运镜打头的复合词要单独进 `_CAMERA_EN`**：`手持跟拍` / `定格特写` 这类
  没有景别词 → 走通用分支会返回 `""` → **景别整条丢失**。
- **尾帧（`keyframe.movement_hint`）**：`build_end_frame_prompt` 生成尾帧图时同样要
  体现「机位已位移」，否则 H3 在首尾帧之间插值不出运动 → 视频是「静帧微动」。
  `_MOVEMENT_BY_CAMERA` 表与 `_CAMERA_MOVE_EN` 同口径（同样最长词优先）。

## 质检层新增：跨镜连续性 + 剧本叙事节奏（2026-09-25）

### 跨镜连续性（图片质检第 7 项）

原图片质检**逐镜独立**判，看不见「上一镜」→ 换镜后背景结构 / 光向 / 服装
**悄悄漂移也没人管**（多镜连播时最刺眼）。

- `qc_client.CONTINUITY_NOTE`：判背景结构 / 光向 / 服装一致性；**换场景豁免**。
- ⚠️ **防幻觉子句**：未提供上一镜时**不得**判连续性（否则模型会凭空编一个「上一镜」）。
- ⚠️ **只在同场景内传上一镜**：`app._qc_prev_shot_desc` 比对
  location / scene_id / scene / scene_name，**跨场景一律不传** —— 换场景本来就该不一样。
- `app._qc_prev_shot_ref` 传上一镜图路径（存在才传），让判官能**目视比对**而非只看文字。

### 剧本质检新增 5 项（`DEFAULT_SCRIPT_PROMPT` 第 18–22 项）

补上「叙事层」盲区（原来只判时长/台词/可拍性，不判「好不好看」）：

| # | 判据 | 短剧行业口径 |
|---|------|-------------|
| 18 | 前三秒钩子 | 开场 3 秒内必须有冲突 / 悬念 / 反常信息，否则划走 |
| 19 | 话轮轮换密度 | 话轮过于集中在单角色 → 观感是「独白」而非「戏」 |
| 20 | 冲突强度 | 单集需有明确的对抗点（人 vs 人 / 人 vs 环境 / 人 vs 自我） |
| 21 | 集尾钩子 | 结尾必须留悬念，否则没有看下一集的动力 |
| 22 | 景别节奏 | 长段同景别 → 观感呆板；需有景别变化 |

> ⚠️ **剧本层时长 ≠ 生成层时长**。`SHOT_DURATION_MAX_OK`（**12.0s**）是**剧本**判据
> （剧本写 12 秒是合理的，成片可以切段）；`H3_SEGMENT_MAX_SEC`（**4.0s**）只作用于
> **生成**层。两者混用会把**完全正常的剧本误判为不可拍**（历史回归）。

## 硬规则

1. **景别/机位必须最靠前、独立成行**。这是「景别 45 次重跑」的直接对策 —— 取景级约束
   一旦被动作描述稀释，模型注意力会优先画动作而跑偏景别。
2. **景别未指定时绝不编一个**（camera 只有机位/运镜时写 `FRAMING (not specified …)`，
   交还画面描述），否则生成端注入「中景」与画面描述的脚部俯拍互斥，质检端再按中景判
   不符 → 该镜永远过不了。（历史：《蛊真人》ep02 shot_13 脚部俯拍被猜成中景，白烧 6 次 GPU。）
3. **身份不复述五官**：只写 `Preserve the exact identity from <imageN>`，
   绝不写脸型/眼睛/鼻子。质检层对此设了反向探针。
4. **台词严禁写进画面**：只给「说话状态 + 口型开合」，写进台词会被模型当字幕画出来。
5. **光影幂等**：`_extract_light_hint` 命中但已被 visual_detail/storyboard_prompt_zh
   承载时返回空串，不重复追加。
6. **风格不硬编码**：无 style 时给「follow the visual style of the reference images」
   兜底 + warning 日志，让上游补风格。
7. **无文字禁令必须独立兜底**，措辞最强硬（含「尤其不得在右下角出现 AI generated 标识」）。
8. **画幅不进正文**：只由 work flow 尺寸节点 / 参考图画幅决定。

## 质检层对应判据（`prompt_qc._check_storyboard`）

生成端与质检端**共用同一份标准**（历史坑：两端标准不同曾把 43% 的镜头误判为不合格）。

**协议分流**：`_new_protocol()` 以 `TASK:` / `PRESERVE:` 判协议版本 —— 新协议按新骨架判、
旧中文分节按旧骨架判。存量项目不会整批假红。

新协议下的判定项：

- 缺 `TASK:` / `FRAMING`（且非「未指定」态）/ `SCENE AND ACTION:` / `PRESERVE:` → issue
- `PRESERVE:` 缺 blanket 保留子句 → issue
- 有参考图但缺 `PRIMARY CANVAS:` → issue
- 有参考图但提示词里**没有任何 `<imageN>` 编号** → issue
- 引用了 `N > ref_count` 的编号（越界） → issue（模型找不到图，约束静默失效）
- 有 `IDENTITY:` 但不含 `Preserve the exact identity from` → issue（likeness 下降）
- `IDENTITY:` 里出现「脸型/五官/鼻子/眼睛/…」等五官词 → issue（官方禁项）
- 出现「第一张图 / 图 A / the first image」等自然语言指代 → issue
- 缺无文字约束 / 台词泄漏 / 质量类空词 → issue（新旧协议共同判定）

## 改动约定

- 改 `build_storyboard_prompt` 后必须跑 `verify_storyboard_prompt_spec.py`
  （断言分节顺序、景别前置、`<imageN>` 编号一致性、身份锚点句式、blanket 保留子句、
  无文字禁令 + 边界镜头：未指定景别 / 无参考图 / 无风格 / 多角色 / 越界编号）。
- 改工作流槽位数后必须跑 `verify_storyboard_ref_canvas.py` + `verify_qwen21_migration.py`。
- 改景别/机位标准 `SHOT_CAMERA_SPECS`/`_CAMERA_ANGLE_SPECS` 前跑 `verify_camera_framing.py`。
- **改视频段切分 / 运镜注入 / 跨镜连续性 / 剧本叙事判据后**必须跑
  `verify_shot_segment_motion.py`（A 运镜注入 · B 切分守恒与无残尾 · C 尾帧运镜 ·
  D app.py 接线含单镜重跑与「不得 `segments=[seg]`」· E 质检新增项 · F 连续性接线）。
  ⚠️ 断言口径：`_camera_move_en` 最长词优先、运镜在提示词里**恰好出现一次**、
  非末段**必须**无台词。
- **改角色设定图版式或景别对档后**必须跑 `verify_sheet_split_views.py`（切分正确性 +
  cells/views 同序 + 兜底开关 + 景别对档取图与降级）与 `verify_character_sheet_ratio.py`
  （画幅不变 + 版式提示词约束齐全 + 幂等标记）。
  ⚠️ 改 `config.CHARACTER_SHEET_LAYOUT_ZH` **必须同步改**
  `comfyui_client._ensure_fullbody_prompt` 的散文串，否则切出来的视角与格位错位。
  ⚠️ `_ensure_fullbody_prompt` 的**幂等 marker 必须是 suffix 的字面子串**
  （曾写作 `上下两排分档设定图` 而 suffix 里是 `…分档版式` → 幂等失效、提示词被无限追加）。
- 工作流模板由 `.workbuddy/tools/build_qwen21_workflows.py` 生成（`EDIT_REF_SLOTS = 9`），
  **不要手改** `分镜生成_Qwen21.json`。
