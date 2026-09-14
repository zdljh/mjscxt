"""
端到端测试：生成角色图 + H3视频
直接使用原始工作流格式（需要重新加载到ComfyUI）
"""
import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

COMFYUI_URL = "http://127.0.0.1:8188"
OUTPUT_DIR = r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output\comic_drama"

def req(method, path, data=None):
    url = COMFYUI_URL + path
    try:
        if data:
            r = urllib.request.Request(
                url, data=json.dumps(data).encode(),
                headers={"Content-Type": "application/json"}
            )
        else:
            r = urllib.request.Request(url)
        with urllib.request.urlopen(r, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:500]
        except:
            pass
        print(f"  HTTP {e.code}: {err_body}")
        return None
    except Exception as e:
        print(f"  Error: {e}")
        return None

def modify_workflow_for_character(wf, character_name="林风"):
    """修改工作流用于角色生成"""
    modified = json.loads(json.dumps(wf))  # Deep copy
    
    # 修改提示词
    for n in modified["nodes"]:
        if n["type"] == "CLIPTextEncode" and n.get("widgets_values"):
            if "古风" in n["widgets_values"][0] or "仙侠" in n["widgets_values"][0]:
                n["widgets_values"][0] = f"国风仙侠少年角色，17岁，身穿青色修士袍，黑发高马尾，眼神坚毅，全身正面照，纯白背景，高细节，anime style\n"
    
    # 修改输出目录
    for n in modified["nodes"]:
        if n["type"] == "SaveImage":
            n["widgets_values"][0] = f"comic_drama/characters/{character_name}"
    
    # 移除MarkdownNote
    modified["nodes"] = [n for n in modified["nodes"] if n["type"] != "MarkdownNote"]
    
    return modified

def submit_workflow(prompt_data):
    """提交工作流到ComfyUI"""
    result = req("POST", "/prompt", {"prompt": prompt_data})
    return result

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Step 1: 生成角色参考图 ──────────────────────────────────────────
print("=" * 60)
print("Step 1: 生成角色参考图 (Qwen 2512)")
print("=" * 60)

# 加载场景生成工作流
wf = json.load(open(r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\user\default\workflows\场景生成.json", encoding="utf-8"))
wf = modify_workflow_for_character(wf, "林风")

# 提交
result = submit_workflow(wf)
if not result:
    print("ERROR: 提交失败")
    exit(1)

prompt_id = result.get("prompt_id", "")
print(f"  提示ID: {prompt_id}")
print("  等待生成...")

# 等待完成
for i in range(120):
    time.sleep(5)
    info = req("GET", f"/history/{prompt_id}")
    if info and prompt_id in info:
        outputs = info[prompt_id].get("outputs", {})
        for nid, out in outputs.items():
            if "images" in out:
                for item in out["images"]:
                    print(f"  ✓ {item.get('filename')}")
        break
    if i % 12 == 0:
        print(f"  ... {(i+1)*5//60}分钟")

# 找生成的图片
char_folder = Path(OUTPUT_DIR) / "characters" / "林风"
ref_imgs = sorted(char_folder.glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True)

if not ref_imgs:
    ref_imgs = sorted(Path(OUTPUT_DIR).glob("*林风*.png"), key=lambda x: x.stat().st_mtime, reverse=True)
if not ref_imgs:
    ref_imgs = sorted(Path(OUTPUT_DIR).glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True)[:3]

if not ref_imgs:
    print("\nERROR: 没有生成任何图片")
    exit(1)

ref_img_path = str(ref_imgs[0])
print(f"\n  使用参考图: {ref_imgs[0].name} ({ref_imgs[0].stat().st_size/1024:.0f}KB)")

# ── Step 2: 生成 H3 视频 ────────────────────────────────────────────
print()
print("=" * 60)
print("Step 2: 生成 H3 视频 (3段 x 6秒)")
print("=" * 60)

test_prompts = [
    "云雾缭绕的仙山秘境，苍松翠柏间有一座破败的古道观。镜头缓慢推进，穿过残垣断壁，一个青衣少年正在擦拭一柄布满灰尘的长剑。国漫古风，电影级画面，cinematic push in",
    "太初古剑突然迸发刺目的万丈光芒！金色光柱直冲云霄，整个秘境被金色剑气笼罩。古树剧烈摇晃，落叶纷飞。青衣少年被剑气冲击向后倒退，眼中倒映金光。史诗感，dramatic lighting",
    "光芒散去，一个玄金色的巨大剑影悬浮在空中，周围环绕太古剑意形成的气浪。少年单膝跪地，双手握拳叩首。剑影化作流光没入少年胸口。国漫仙侠，emotional climax"
]

width, height = 768, 1344
duration_per_clip = 6

# 构建H3工作流（使用links格式）
h3_workflow = {
    "last_node_id": 10,
    "last_link_id": 10,
    "nodes": [
        {"id": 1, "type": "UNETLoader", "pos": [0, 0], "size": [315, 82], "flags": {}, "order": 0, "mode": 0,
         "inputs": [], "outputs": [{"name": "MODEL", "type": "MODEL", "links": [1]}],
         "title": "UNETLoader", "properties": {},
         "widgets_values": ["minimax-h3\\minimax_h3_ref2va_pruned_int8_convrot.safetensors", "default"]},
        {"id": 2, "type": "CLIPLoader", "pos": [0, 100], "size": [315, 82], "flags": {}, "order": 1, "mode": 0,
         "inputs": [], "outputs": [{"name": "CLIP", "type": "CLIP", "links": [2, 3]}],
         "title": "CLIPLoader", "properties": {},
         "widgets_values": ["minimax-h3\\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "minimax", "default"]},
        {"id": 3, "type": "VAELoader", "pos": [0, 200], "size": [315, 82], "flags": {}, "order": 2, "mode": 0,
         "inputs": [], "outputs": [{"name": "VAE", "type": "VAE", "links": [4]}],
         "title": "VAELoader", "properties": {},
         "widgets_values": ["minimax-h3\\minimax_h3_video_vae_fp16.safetensors"]},
        {"id": 4, "type": "VAELoader", "pos": [0, 300], "size": [315, 82], "flags": {}, "order": 3, "mode": 0,
         "inputs": [], "outputs": [{"name": "VAE", "type": "VAE", "links": [5]}],
         "title": "AudioVAELoader", "properties": {},
         "widgets_values": ["minimax-h3\\minimax_h3_audio_vae_fp32.safetensors"]},
    ],
    "links": [
        [1, 1, 0, 5, 0, "MODEL"],
        [2, 2, 0, 5, 0, "CLIP"],
        [3, 2, 0, 6, 0, "CLIP"],
        [4, 3, 0, 5, 1, "VAE"],
        [5, 4, 0, 6, 1, "VAE"],
    ]
}

# 添加H3节点
for i, prompt in enumerate(test_prompts):
    nid = 5 + i
    h3_workflow["nodes"].append({
        "id": nid, "type": "MiniMaxH3ReferenceToVideo", "pos": [400, i*100],
        "size": [400, 200], "flags": {}, "order": 4+i, "mode": 0,
        "inputs": [
            {"name": "clip", "type": "CLIP", "link": 2},
            {"name": "vae", "type": "VAE", "link": 4},
            {"name": "audio_vae", "type": "VAE", "link": 5},
            {"name": "prompt", "type": "STRING", "widget": {"name": "prompt"}, "link": None},
            {"name": "width", "type": "INT", "widget": {"name": "width"}, "link": None},
            {"name": "height", "type": "INT", "widget": {"name": "height"}, "link": None},
            {"name": "length", "type": "INT", "widget": {"name": "length"}, "link": None},
            {"name": "ref_image_size", "type": "COMBO", "widget": {"name": "ref_image_size"}, "link": None},
        ],
        "outputs": [{"name": "images", "type": "IMAGE", "links": [6+i, 7+i] if i < len(test_prompts)-1 else [6+i]}],
        "title": f"Clip_{i+1}", "properties": {},
        "widgets_values": [
            "minimax-h3\\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            "minimax", "default",
            prompt, width, height, duration_per_clip, [width, height]
        ]
    })
    # Update links for this node
    h3_workflow["links"].extend([
        [100+i, 2, 0, nid, 0, "CLIP"],
        [101+i, 4, 0, nid, 1, "VAE"],
        [102+i, 5, 0, nid, 2, "VAE"],
    ])

# 添加拼接节点
join_ids = []
for i in range(len(test_prompts) - 1):
    nid = 8 + i
    h3_workflow["nodes"].append({
        "id": nid, "type": "H3ContinuousSeamlessJoinV14", "pos": [850, i*100],
        "size": [400, 200], "flags": {}, "order": 7+i, "mode": 0,
        "inputs": [
            {"name": "previous_images", "type": "IMAGE", "link": 6+i},
            {"name": "next_images", "type": "IMAGE", "link": 7+i},
            {"name": "next_output_mode", "type": "STRING", "widget": {"name": "next_output_mode"}, "link": None},
            {"name": "next_head_context_frames", "type": "INT", "widget": {"name": "next_head_context_frames"}, "link": None},
            {"name": "video_crossfade_frames", "type": "INT", "widget": {"name": "video_crossfade_frames"}, "link": None},
            {"name": "audio_crossfade_ms", "type": "INT", "widget": {"name": "audio_crossfade_ms"}, "link": None},
            {"name": "luminance_match", "type": "BOOLEAN", "widget": {"name": "luminance_match"}, "link": None},
            {"name": "luminance_fade_frames", "type": "INT", "widget": {"name": "luminance_fade_frames"}, "link": None},
            {"name": "max_luminance_correction_percent", "type": "FLOAT", "widget": {"name": "max_luminance_correction_percent"}, "link": None},
            {"name": "max_safe_tail_bridge_frames", "type": "INT", "widget": {"name": "max_safe_tail_bridge_frames"}, "link": None},
        ],
        "outputs": [{"name": "images", "type": "IMAGE", "links": [8+i] if i < len(test_prompts)-2 else [8+i]}],
        "title": f"Join_{i+1}", "properties": {},
        "widgets_values": [0, 2, 6, 1000, True, 10, 15, 6]
    })
    join_ids.append(nid)

# Add SaveVideo
last_join = join_ids[-1]
h3_workflow["nodes"].append({
    "id": 10, "type": "SaveVideo", "pos": [1300, 100],
    "size": [400, 100], "flags": {}, "order": 9, "mode": 0,
    "inputs": [{"name": "images", "type": "IMAGE", "link": 8}],
    "outputs": [],
    "title": "SaveVideo", "properties": {},
    "widgets_values": ["h3_test_剑心初醒"]
})

# Update last_link_id
h3_workflow["last_link_id"] = max(link[0] for link in h3_workflow["links"])

print(f"  节点: {len(h3_workflow['nodes'])}")
print(f"  连接: {len(h3_workflow['links'])}")
print(f"  3个片段 x {duration_per_clip}s = {len(test_prompts) * duration_per_clip}秒")
print()

print("提交 H3 视频生成...")
result = req("POST", "/prompt", {"prompt": h3_workflow})
if not result:
    print("ERROR: 提交失败")
    exit(1)

prompt_id = result.get("prompt_id", "")
print(f"  提示ID: {prompt_id}")
print("  等待生成（预计10-20分钟）...")

# 监控
start = time.time()
last_node = ""
for i in range(240):
    time.sleep(10)
    elapsed = time.time() - start
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)

    # 检查队列
    try:
        q_r = urllib.request.urlopen("http://127.0.0.1:8188/queue", timeout=5)
        q_data = json.loads(q_r.read())
        running = q_data.get("queue_running", [])
        if running:
            node_type = running[0][2].get("class_type", "unknown")
            if node_type != last_node:
                print(f"  [{mins:02d}:{secs:02d}] {node_type}")
                last_node = node_type
    except:
        pass

    # 检查完成
    info = req("GET", f"/history/{prompt_id}")
    if info and prompt_id in info:
        outputs = info[prompt_id].get("outputs", {})
        print()
        print("=" * 60)
        print(f"✓ 完成！用时 {mins}分{secs}秒")
        print("=" * 60)
        for nid, out in outputs.items():
            for key in ["images", "videos"]:
                if key in out:
                    for item in out[key]:
                        print(f"  ✓ {key}: {item.get('subfolder', '')}/{item.get('filename')}")
        break
    elif i % 30 == 0:
        print(f"  [{mins:02d}:{secs:02d}] 生成中...")

# 列出输出
print()
print("输出文件:")
for pattern in ["**/*.mp4", "**/*h3*.mp4"]:
    for f in sorted(Path(OUTPUT_DIR).glob(pattern), key=lambda x: x.stat().st_mtime, reverse=True):
        sz = f.stat().st_size / 1024 / 1024
        print(f"  {f.relative_to(Path(OUTPUT_DIR).parent)} ({sz:.1f}MB)")
