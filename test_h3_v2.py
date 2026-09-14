"""
H3 视频生成端到端测试 - 3段 x 6秒 = 18秒
使用正确的 ComfyUI API 格式
"""
import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

COMFYUI_URL = "http://127.0.0.1:8188"
OUTPUT_DIR = r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output\comic_drama\h3_test"

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
        print(f"  HTTP {e.code}: {e.reason}")
        print(f"  Body: {err_body}")
        return None
    except Exception as e:
        print(f"  Error: {e}")
        return None

# ── 1. 检查 ComfyUI ──────────────────────────────────────────────────
print("=" * 50)
print("ComfyUI 状态检查")
print("=" * 50)
st = req("GET", "/system_stats")
if not st:
    print("ERROR: ComfyUI 不可用")
    exit(1)
for d in st.get("devices", []):
    free = d.get("vram_free", 0) / 1024 / 1024 / 1024
    total = d.get("vram_total", 0) / 1024 / 1024 / 1024
    print(f"  GPU: {d['name']} | {free:.1f}GB / {total:.1f}GB")

# ── 2. 找参考图 ──────────────────────────────────────────────────────
print()
print("查找角色参考图...")
output_root = Path(r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output")
char_imgs = sorted(
    output_root.glob("v6char_*_front.png"),
    key=lambda x: x.stat().st_mtime, reverse=True
)[:3]

if char_imgs:
    ref_img_path = str(char_imgs[0])
    print(f"  使用: {char_imgs[0].name} ({char_imgs[0].stat().st_size/1024:.0f}KB)")
else:
    # 尝试其他格式的参考图
    alt_imgs = sorted(output_root.glob("*front*.png"), key=lambda x: x.stat().st_mtime, reverse=True)[:3]
    if alt_imgs:
        ref_img_path = str(alt_imgs[0])
        print(f"  使用: {alt_imgs[0].name}")
    else:
        # 生成一个空白参考图
        print("  未找到参考图，将使用合成图")
        ref_img_path = ""

# ── 3. 构建 H3 工作流 ───────────────────────────────────────────────
print()
print("构建 H3 工作流...")

test_prompts = [
    "云雾缭绕的仙山秘境，苍松翠柏间有一座破败的古道观。镜头缓慢推进，穿过残垣断壁，一个青衣少年正在擦拭一柄布满灰尘的长剑。国漫古风，电影级画面，cinematic push in, Chinese fantasy anime, wuxia aesthetic",
    "太初古剑突然迸发刺目的万丈光芒！金色光柱直冲云霄，整个秘境被金色剑气笼罩。古树剧烈摇晃，落叶纷飞。青衣少年被剑气冲击向后倒退，眼中倒映金光。国漫仙侠，史诗感，dramatic lighting, golden explosion",
    "光芒散去，一个玄金色的巨大剑影悬浮在空中，周围环绕太古剑意形成的气浪。少年单膝跪地，双手握拳叩首。剑影化作流光没入少年胸口，少年双眼闪过金色光芒后恢复清明。国漫仙侠，emotional climax, cinematic"
]

width, height = 768, 1344
duration_per_clip = 6

os.makedirs(OUTPUT_DIR, exist_ok=True)

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

def make_link(from_node_id, from_idx, to_node_id, to_idx, type_str="STRING"):
    links.append([len(links), from_node_id, from_idx, to_node_id, to_idx, type_str])

# 1. UNETLoader
unet_id = add_node("UNETLoader", [], [
    "minimax-h3\\minimax_h3_ref2va_pruned_int8_convrot.safetensors", "default"
], "UNETLoader")

# 2. CLIPLoader
clip_id = add_node("CLIPLoader", [], [
    "minimax-h3\\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "minimax", "default"
], "CLIPLoader")

# 3. VAELoader (video)
vae_id = add_node("VAELoader", [], [
    "minimax-h3\\minimax_h3_video_vae_fp16.safetensors"
], "VAELoader")

# 4. VAELoader (audio)
audio_vae_id = add_node("VAELoader", [], [
    "minimax-h3\\minimax_h3_audio_vae_fp32.safetensors"
], "AudioVAELoader")

# 连接模型到各节点
# UNETLoader output -> model input of each clip
# CLIPLoader output -> clip input of each clip
# VAELoader (video) output -> vae input of each clip
# VAELoader (audio) output -> audio_vae input of each clip

# 创建 H3 生成节点
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

last_join = join_ids[-1]

# SaveVideo
save_id = add_node("SaveVideo", [
    {"name": "images", "link": last_join},
], ["h3_test"], "SaveVideo")

workflow = {"nodes": nodes, "links": links}

print(f"  节点: {len(nodes)}, 连接: {len(links)}")
print(f"  3个片段 x {duration_per_clip}s = {len(test_prompts) * duration_per_clip}秒总时长")
print()

# ── 4. 提交生成 ──────────────────────────────────────────────────────
print("=" * 50)
print("提交 H3 视频生成...")
print("=" * 50)

result = req("POST", "/prompt", {"prompt": workflow})
if not result:
    print("提交失败")
    exit(1)

prompt_id = result.get("prompt_id", "")
print(f"提示ID: {prompt_id}")
print()

# 监控进度
print("等待生成完成...")
start = time.time()
last_progress = ""

for i in range(240):  # 最多 40 分钟
    time.sleep(10)
    elapsed = time.time() - start
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)

    # 检查队列状态
    try:
        q_r = urllib.request.urlopen("http://127.0.0.1:8188/queue", timeout=5)
        q_data = json.loads(q_r.read())
        running = q_data.get("queue_running", [])
        queue_size = q_data.get("queue", {}).get("running", 0)

        if running:
            node_info = running[0][2]  # node info
            node_type = node_info.get("class_type", "unknown")
            progress_text = f"  [{mins:02d}:{secs:02d}] 运行中: {node_type}"
            if progress_text != last_progress:
                print(progress_text)
                last_progress = progress_text
        else:
            print(f"  [{mins:02d}:{secs:02d}] 等待中...")
    except:
        pass

    # 检查完成
    info = req("GET", f"/history/{prompt_id}")
    if info and prompt_id in info:
        outputs = info[prompt_id].get("outputs", {})
        print()
        print("=" * 50)
        print(f"✓ 生成完成！用时 {mins}分{secs}秒")
        print("=" * 50)
        for nid, out in outputs.items():
            for key in ["images", "videos"]:
                if key in out:
                    for item in out[key]:
                        fname = item.get("filename", "unknown")
                        subfolder = item.get("subfolder", "")
                        print(f"  ✓ {key}: {subfolder}/{fname}")
        break
    elif i % 30 == 0:  # 每 5 分钟
        print(f"  [{mins:02d}:{secs:02d}] 生成中...")

# ── 5. 列出输出文件 ─────────────────────────────────────────────────
print()
print("输出文件:")
out_path = Path(OUTPUT_DIR)
if out_path.exists():
    for f in sorted(out_path.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        sz = f.stat().st_size / 1024 / 1024
        print(f"  {f.name} ({sz:.1f}MB)")
else:
    print(f"  (目录不存在: {OUTPUT_DIR})")
