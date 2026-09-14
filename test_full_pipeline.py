"""
快速测试：直接用场景生成工作流生成参考图，然后跑H3视频生成
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

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Step 1: 用场景生成工作流生成参考图 ──────────────────────────────
print("=" * 60)
print("Step 1: 生成角色参考图 (使用现有工作流)")
print("=" * 60)

# 加载场景生成工作流
wf = json.load(open(r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\user\default\workflows\场景生成.json", encoding="utf-8"))

# 修改提示词为角色描述
for n in wf["nodes"]:
    if n["type"] == "CLIPTextEncode" and n.get("widgets_values"):
        # 第一个是正提示词
        n["widgets_values"][0] = """年轻中国修仙少年，17岁，身穿青色修士袍，黑发束起，眼神坚毅，站在破败古道观前，云雾缭绕的仙山背景，国漫古风，全身正面照，白色背景，高细节，anime style
"""
        print(f"  正提示词: {n['widgets_values'][0][:50]}...")
    elif n["type"] == "CLIPTextEncode" and n.get("widgets_values"):
        # 第二个是负提示词
        n["widgets_values"][0] = """人物，角色，文字，水印，3D，写实，照片，过曝，模糊，脏污色块
"""

# 修改输出目录
for n in wf["nodes"]:
    if n["type"] == "SaveImage":
        n["widgets_values"][0] = "comic_drama/characters/林风"
        print(f"  输出: {n['widgets_values'][0]}")

# 提交
result = req("POST", "/prompt", {"prompt": wf})
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
    # 尝试其他位置
    ref_imgs = sorted(Path(OUTPUT_DIR).glob("*林风*.png"), key=lambda x: x.stat().st_mtime, reverse=True)
if not ref_imgs:
    ref_imgs = sorted(Path(OUTPUT_DIR).glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True)[:3]

if not ref_imgs:
    print("\nERROR: 没有生成任何图片")
    print("列出所有PNG:")
    for f in sorted(Path(OUTPUT_DIR).rglob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
        print(f"  {f.relative_to(Path(OUTPUT_DIR).parent)}")
    exit(1)

ref_img_path = str(ref_imgs[0])
print(f"\n  使用参考图: {ref_imgs[0].name} ({ref_imgs[0].stat().st_size/1024:.0f}KB)")

# ── Step 2: 生成 H3 视频 ────────────────────────────────────────────
print()
print("=" * 60)
print("Step 2: 生成 H3 视频 (3段 x 6秒)")
print("=" * 60)

test_prompts = [
    "云雾缭绕的仙山秘境，苍松翠柏间有一座破败的古道观。镜头缓慢推进，穿过残垣断壁，一个青衣少年正在擦拭一柄布满灰尘的长剑。国漫古风，电影级画面，cinematic push in, Chinese fantasy anime",
    "太初古剑突然迸发刺目的万丈光芒！金色光柱直冲云霄，整个秘境被金色剑气笼罩。古树剧烈摇晃，落叶纷飞。青衣少年被剑气冲击向后倒退，眼中倒映金光。国漫仙侠，史诗感，dramatic lighting",
    "光芒散去，一个玄金色的巨大剑影悬浮在空中，周围环绕太古剑意形成的气浪。少年单膝跪地，双手握拳叩首。剑影化作流光没入少年胸口，少年双眼闪过金色光芒后恢复清明。国漫仙侠，emotional climax"
]

width, height = 768, 1344
duration_per_clip = 6

# 构建H3工作流（3个片段+拼接）
nodes = []
links = []
next_order = 0

def add_node(node_type, inputs, widgets_values=None, title=""):
    global next_order
    nid = len(nodes) + 1
    n = {
        "id": nid, "type": node_type,
        "pos": [0, 0], "size": [400, 100],
        "flags": {}, "order": next_order, "mode": 0,
        "inputs": inputs, "outputs": [],
        "properties": {},
        "widgets_values": widgets_values or []
    }
    if title:
        n["title"] = title
    nodes.append(n)
    next_order += 1
    return nid

def make_link(from_node_id, from_idx, to_node_id, to_idx):
    links.append([len(links), from_node_id, from_idx, to_node_id, to_idx, "STRING"])

# 模型加载器
unet_id = add_node("UNETLoader", [], ["minimax-h3\\minimax_h3_ref2va_pruned_int8_convrot.safetensors", "default"], "UNETLoader")
clip_id = add_node("CLIPLoader", [], ["minimax-h3\\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "minimax", "default"], "CLIPLoader")
vae_id = add_node("VAELoader", [], ["minimax-h3\\minimax_h3_video_vae_fp16.safetensors"], "VAELoader")
audio_vae_id = add_node("VAELoader", [], ["minimax-h3\\minimax_h3_audio_vae_fp32.safetensors"], "AudioVAELoader")

# 加载参考图
ref_id = add_node("LoadImage", [{"name": "image", "value": ref_img_path}], [ref_img_path, "image"], "LoadImage")

# 3个H3生成节点
clip_nodes = []
for i, prompt in enumerate(test_prompts):
    cn_id = add_node("MiniMaxH3ReferenceToVideo", [
        {"name": "clip", "link": clip_id},
        {"name": "vae", "link": vae_id},
        {"name": "audio_vae", "link": audio_vae_id},
        {"name": "prompt", "value": prompt},
        {"name": "width", "value": width},
        {"name": "height", "value": height},
        {"name": "length", "value": duration_per_clip},
        {"name": "ref_image_size", "value": [width, height]},
    ], [
        "minimax-h3\\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "minimax", "default",
        prompt, width, height, duration_per_clip, [width, height]
    ], f"Clip_{i+1}")
    clip_nodes.append(cn_id)

# 串联拼接
join_ids = []
for i in range(len(clip_nodes) - 1):
    j_id = add_node("H3ContinuousSeamlessJoinV14", [
        {"name": "previous_images", "link": clip_nodes[i]},
        {"name": "next_images", "link": clip_nodes[i + 1]},
        {"name": "next_output_mode", "value": "video"},
        {"name": "next_head_context_frames", "value": 2},
        {"name": "video_crossfade_frames", "value": 6},
        {"name": "audio_crossfade_ms", "value": 1000},
        {"name": "luminance_match", "value": True},
        {"name": "luminance_fade_frames", "value": 10},
        {"name": "max_luminance_correction_percent", "value": 15},
        {"name": "max_safe_tail_bridge_frames", "value": 6},
    ], [0, 2, 6, 1000, True, 10, 15, 6], f"Join_{i+1}")
    join_ids.append(j_id)

# SaveVideo
save_id = add_node("SaveVideo", [{"name": "images", "link": join_ids[-1]}], ["h3_test_剑心初醒"], "SaveVideo")

workflow = {"nodes": nodes, "links": links}

print(f"  节点: {len(nodes)}, 连接: {len(links)}")
print(f"  3个片段 x {duration_per_clip}s = {len(test_prompts) * duration_per_clip}秒")
print()

# 提交
print("提交 H3 视频生成...")
result = req("POST", "/prompt", {"prompt": workflow})
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
