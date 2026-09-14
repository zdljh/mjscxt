"""
漫剧生成测试 - 国漫风格 1 分钟视频
步骤：
1. 用 Qwen 2512 生成角色/场景参考图
2. 加载 H3 工作流，替换前 3 个 clip 的提示词（各 6 秒 = 18 秒）
3. 提交 ComfyUI 执行，等待完成
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── 路径配置 ──────────────────────────────────────────
COMFYUI_URL = "http://127.0.0.1:8188"
COMFYUI_WORKFLOWS = r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\user\default\workflows"
OUTPUT_DIR = r"C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\output\comic_drama"
COMFYUI_OUTPUT = r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output"

# ── 测试内容 ──────────────────────────────────────────
TEST_SCRIPT = {
    "title": "剑心初醒",
    "style": "国漫古风",
    "characters": [
        {
            "name": "林风",
            "prompt": "少年剑修，17岁，黑发束起，身穿青色修士袍，眼神坚毅冷峻，国漫古风，精致面部特写，半身像，干净深色背景，cinematic lighting, anime style, detailed face, Chinese fantasy art"
        },
        {
            "name": "古剑之魂·渊",
            "prompt": "玄金色太古剑魂，万丈光芒中浮现的人形剑影，威严苍古，身披金色剑甲，身后悬浮太初古剑，国漫仙侠，史诗感，全身像，clean dark background, ethereal golden glow, Chinese mythology"
        }
    ],
    "scenes": [
        {
            "name": "仙山秘境",
            "prompt": "云雾缭绕的仙山秘境，苍松翠柏间有一座破败的古道观残垣断壁，远处瀑布飞流直下，国漫古风仙侠场景，广角镜头，电影级画面，cinematic landscape, Chinese fantasy"
        },
        {
            "name": "古剑觉醒",
            "prompt": "万丈金光冲天而起，古剑爆发耀眼剑芒，金色剑气席卷整个秘境，落叶纷飞，古树摇晃，国漫仙侠，史诗级特效，dramatic golden explosion, Chinese fantasy"
        },
        {
            "name": "剑魂入体",
            "prompt": "玄金色剑影化作流光没入少年胸口，少年睁眼，眸中闪过金芒，周身环绕太古剑意气浪，国漫仙侠，emotional climax, golden energy, Chinese fantasy"
        }
    ],
    "h3_clips": [
        {
            "num": 1,
            "duration": 6,
            "prompt": "云雾缭绕的仙山秘境，苍松翠柏间有一座破败的古道观。镜头缓慢推进，穿过残垣断壁，一个青衣少年正在擦拭一柄布满灰尘的长剑。国漫古风，电影级画面，cinematic push in, Chinese fantasy anime"
        },
        {
            "num": 2,
            "duration": 6,
            "prompt": "太初古剑突然迸发刺目的万丈光芒！金色光柱直冲云霄，整个秘境被金色剑气笼罩。古树剧烈摇晃，落叶纷飞。青衣少年被剑气冲击向后倒退，眼中倒映金光。国漫仙侠，史诗感，dramatic lighting, golden explosion"
        },
        {
            "num": 3,
            "duration": 6,
            "prompt": "光芒散去，一个玄金色的巨大剑影悬浮在空中，周围环绕太古剑意形成的气浪。少年单膝跪地，双手握拳叩首。剑影化作流光没入少年胸口，少年双眼闪过金色光芒后恢复清明。国漫仙侠，emotional climax, cinematic"
        }
    ]
}


# ── ComfyUI 工具函数 ──────────────────────────────────
def comfy_req(method, path, data=None, timeout=120):
    url = COMFYUI_URL + path
    try:
        if data is not None:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
        else:
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP Error {e.code}: {e.reason}")
        return None
    except Exception as e:
        print(f"  Error: {e}")
        return None


def comfy_status():
    d = comfy_req("GET", "/system_stats")
    if not d:
        return {"status": "offline"}
    devices = d.get("devices", [])
    gpu = devices[0] if devices else {}
    return {
        "status": "online",
        "gpu": gpu.get("name", "unknown"),
        "free_vram": gpu.get("vram_free", 0) / 1024 / 1024 / 1024,
        "total_vram": gpu.get("vram_total", 0) / 1024 / 1024 / 1024,
    }


def wait_for_idle(timeout=600):
    """等待 ComfyUI 队列空闲"""
    print("  等待 ComfyUI 空闲...")
    start = time.time()
    while time.time() - start < timeout:
        d = comfy_req("GET", "/prompt")
        if d and d.get("exec_info", {}).get("queue_remaining", 1) == 0:
            print("  ✓ ComfyUI 已空闲")
            return True
        time.sleep(5)
    print("  ⚠ 等待超时")
    return False


def wait_for_history(prompt_id, timeout=900):
    """等待指定 prompt 完成"""
    start = time.time()
    while time.time() - start < timeout:
        info = comfy_req("GET", f"/history/{prompt_id}")
        if info and prompt_id in info:
            return info[prompt_id]
        elapsed = int(time.time() - start)
        if elapsed % 30 == 0:
            st = comfy_status()
            print(f"  等待中... {elapsed//60}分{elapsed%60}秒 | GPU剩余: {st.get('free_vram', '?')}GB")
        time.sleep(5)
    return None


def find_node_by_type(nodes, type_name):
    """在 workflow nodes 中查找指定类型的节点"""
    results = []
    for n in nodes:
        if type_name in n["type"]:
            results.append(n)
    return results


def get_node_by_id(nodes, nid):
    """通过 ID 查找节点"""
    for n in nodes:
        if n["id"] == nid:
            return n
    return None


# ── 步骤 1: 生成参考图 ─────────────────────────────
def generate_ref_images():
    """用 QwenImageTextToImageApi 生成参考图"""
    print("\n" + "=" * 60)
    print("步骤 1: 生成参考图 (Qwen 2512)")
    print("=" * 60)

    os.makedirs(os.path.join(OUTPUT_DIR, "characters"), exist_ok=True)
    os.makedirs(os.path.join(OUTPUT_DIR, "scenes"), exist_ok=True)

    # 加载 Qwen 图像生成工作流
    wf_path = os.path.join(COMFYUI_WORKFLOWS, "角色生成.json")
    with open(wf_path, "r", encoding="utf-8") as f:
        wf = json.load(f)

    nodes = wf["nodes"]

    # 找到关键节点
    pos_node = neg_node = save_node = vae_decode = None
    for n in nodes:
        t = n["type"]
        if t == "CLIPTextEncode":
            wv = n.get("widgets_values", [])
            if wv and "正" in str(wv[0]):
                pos_node = n
            elif wv and "负" in str(wv[0]):
                neg_node = n
        elif t == "SaveImage":
            save_node = n
        elif t == "VAEDecode":
            vae_decode = n

    if not all([pos_node, neg_node, save_node]):
        print("  ⚠ 未找到全部必要节点，尝试自动定位...")
        for n in nodes:
            t = n["type"]
            wv = n.get("widgets_values", [])
            if t == "CLIPTextEncode" and wv:
                text = str(wv[0])
                if "清冷" in text or "positive" in text.lower():
                    pos_node = n
                elif "成熟" in text or "negative" in text.lower():
                    neg_node = n
            elif t == "SaveImage":
                save_node = n
            elif t == "VAEDecode":
                vae_decode = n

    if not all([pos_node, neg_node, save_node]):
        print("  ✗ 无法定位必要节点，跳过参考图生成")
        return {}

    generated = {}

    # 生成所有角色和场景图
    items = []
    for c in TEST_SCRIPT["characters"]:
        items.append((c["name"], c["prompt"], "characters"))
    for s in TEST_SCRIPT["scenes"]:
        items.append((s["name"], s["prompt"], "scenes"))

    for name, prompt, folder in items:
        print(f"  生成: {name} ...")

        # 更新提示词
        pos_node["widgets_values"][0] = prompt
        neg_node["widgets_values"][0] = (
            "成熟脸，尖锐五官，浓妆，艳红唇，油腻皮肤，金属质感过重，"
            "服装华丽过度，变形，五官错位，脸型崩坏，多余肢体，模糊，水印，文字，杂乱背景"
        )

        # 设置输出路径 (SaveImage widgets_values[0] = filename_prefix)
        out_name = f"{name}_{int(time.time() * 1000) % 10000}.png"
        save_node["widgets_values"][0] = f"comic_drama/{folder}/{out_name}"

        # 提交
        result = comfy_req("POST", "/prompt", {"prompt": wf})
        if not result:
            print(f"    ✗ 提交失败")
            continue

        prompt_id = result.get("prompt_id", "")
        print(f"    ✓ 已提交 (prompt_id={prompt_id})")

        # 等待完成
        hist = wait_for_history(prompt_id, timeout=300)
        if hist:
            outputs = hist.get("outputs", {})
            for out_id, out_data in outputs.items():
                files = out_data.get("images", [])
                for img in files:
                    filepath = os.path.join(COMFYUI_OUTPUT, img.get("filename", ""))
                    if name in img.get("filename", ""):
                        generated[name] = filepath
                        print(f"    ✓ {name}: {img['filename']}")
                        break

        # 清除临时路径
        save_node["inputs"]["filename_prefix"] = "comic_drama"
        time.sleep(2)  # 避免过快提交

    print(f"\n  共生成 {len(generated)} 张参考图")
    return generated


# ── 步骤 2: H3 视频生成 ─────────────────────────────
def generate_h3_video(ref_images=None):
    """加载 H3 工作流，替换提示词，生成视频"""
    print("\n" + "=" * 60)
    print("步骤 2: H3 视频生成")
    print("=" * 60)

    wf_path = os.path.join(COMFYUI_WORKFLOWS, "H3信号10段测试001.json")
    with open(wf_path, "r", encoding="utf-8") as f:
        wf = json.load(f)

    nodes = wf["nodes"]

    # 找到所有 H3 生成节点（widgets 含 'Prompt — Clip'）
    h3_nodes = {}
    for n in nodes:
        wv = n.get("widgets_values", [])
        if wv and len(wv) >= 4 and "Clip" in str(wv[-1]):
            clip_num = int(str(wv[-1]).split("Clip ")[-1])
            h3_nodes[clip_num] = n

    print(f"  找到 {len(h3_nodes)} 个 H3 生成节点")

    # 替换前 N 个 clip 的提示词
    num_clips = min(len(TEST_SCRIPT["h3_clips"]), len(h3_nodes))
    for clip_info in TEST_SCRIPT["h3_clips"][:num_clips]:
        clip_num = clip_info["num"]
        node = h3_nodes.get(clip_num)
        if node:
            old_wv = node["widgets_values"]
            node["widgets_values"] = [
                old_wv[0],  # width
                old_wv[1],  # height
                float(clip_info["duration"]),  # duration
                clip_info["prompt"],  # prompt
            ]
            print(f"  Clip {clip_num}: 已更新提示词 ({clip_info['duration']}s)")

    # 修改 SaveVideo 输出目录 (widgets[0] = filename_prefix)
    for n in nodes:
        if n["type"] == "SaveVideo":
            n["widgets_values"][0] = "comic_drama/h3_test"
            print(f"  输出目录: comic_drama/h3_test")

    # 提交
    print("\n  提交 H3 工作流到 ComfyUI...")
    result = comfy_req("POST", "/prompt", {"prompt": wf})
    if not result:
        print("  ✗ 提交失败")
        return []

    prompt_id = result.get("prompt_id", "")
    print(f"  ✓ 已提交 (prompt_id={prompt_id})")

    # 等待完成（H3 10段可能需要较长时间）
    print(f"\n  等待生成完成（预计 3-8 分钟）...")
    hist = wait_for_history(prompt_id, timeout=900)

    if not hist:
        print("  ⚠ 生成超时")
        return []

    # 查找生成的视频
    output_videos = []
    output_path = Path(COMFYUI_OUTPUT) / "comic_drama" / "h3_test"
    if output_path.exists():
        for f in sorted(output_path.glob("*.mp4")):
            size_mb = f.stat().st_size / 1024 / 1024
            print(f"  ✓ 视频: {f.name} ({size_mb:.1f} MB)")
            output_videos.append(str(f))

    # 也检查根 output 目录
    for f in sorted(Path(COMFYUI_OUTPUT).glob("*h3*.mp4")):
        if str(f) not in output_videos:
            size_mb = f.stat().st_size / 1024 / 1024
            print(f"  ✓ 视频: {f.name} ({size_mb:.1f} MB)")
            output_videos.append(str(f))

    for f in sorted(Path(COMFYUI_OUTPUT).glob("*Herrgotts*.mp4")):
        if str(f) not in output_videos:
            size_mb = f.stat().st_size / 1024 / 1024
            print(f"  ✓ 视频: {f.name} ({size_mb:.1f} MB)")
            output_videos.append(str(f))

    return output_videos


# ── 主流程 ──────────────────────────────────────────
def main():
    print("╔══════════════════════════════════════════════════╗")
    print("║     漫剧生成测试 - 国漫风格 18秒视频             ║")
    print("╚══════════════════════════════════════════════════╝")

    # 检查 ComfyUI
    status = comfy_status()
    if status["status"] != "online":
        print("✗ ComfyUI 未在线，请先启动 ComfyUI")
        sys.exit(1)
    print(f"✓ ComfyUI 在线 | GPU: {status['gpu']} | 可用显存: {status['free_vram']:.1f}GB")

    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 步骤 1: 生成参考图
    ref_images = generate_ref_images()

    # 步骤 2: H3 视频生成
    videos = generate_h3_video(ref_images)

    # 汇总
    print("\n" + "=" * 60)
    print("测试完成!")
    print(f"参考图: {len(ref_images)} 张")
    print(f"视频: {len(videos)} 个")
    print(f"输出目录: {OUTPUT_DIR}")
    print("=" * 60)

    if videos:
        print("\n生成的视频文件:")
        for v in videos:
            print(f"  {v}")


if __name__ == "__main__":
    main()
