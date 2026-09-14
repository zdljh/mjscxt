"""
H3 视频生成测试 - 使用标准节点
"""

import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

COMFYUI_URL = "http://127.0.0.1:8188"
OUTPUT_DIR = r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output"

# 测试提示词
TEST_PROMPTS = [
    "云雾缭绕的仙山秘境，苍松翠柏间有一座破败的古道观。镜头缓慢推进，穿过残垣断壁，一个青衣少年正在擦拭一柄布满灰尘的长剑。国漫古风，电影级画面，cinematic push in, Chinese fantasy anime",
    "太初古剑突然迸发刺目的万丈光芒！金色光柱直冲云霄，整个秘境被金色剑气笼罩。古树剧烈摇晃，落叶纷飞。青衣少年被剑气冲击向后倒退，眼中倒映金光。国漫仙侠，史诗感，dramatic lighting, golden explosion",
    "光芒散去，一个玄金色的巨大剑影悬浮在空中，周围环绕太古剑意形成的气浪。少年单膝跪地，双手握拳叩首。剑影化作流光没入少年胸口，少年双眼闪过金色光芒后恢复清明。国漫仙侠，emotional climax, cinematic"
]

def req(method, path, data=None):
    url = COMFYUI_URL + path
    try:
        if data:
            r = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
        else:
            r = urllib.request.Request(url)
        with urllib.request.urlopen(r, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.reason}")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None

def wait_for_completion(prompt_id, timeout=1800):
    """等待任务完成"""
    print(f"  等待生成 (prompt_id={prompt_id})...")
    start = time.time()
    while time.time() - start < timeout:
        info = req("GET", f"/history/{prompt_id}")
        if info and prompt_id in info:
            print(f"  ✓ 生成完成 ({int(time.time() - start)}s)")
            return info[prompt_id]
        elapsed = int(time.time() - start)
        if elapsed % 60 == 0:
            print(f"  等待中... {elapsed//60}分钟")
        time.sleep(10)
    print(f"  ⚠ 超时 ({timeout//60}分钟)")
    return None

def main():
    print("╔══════════════════════════════════════════════════╗")
    print("║     H3 视频生成测试 - 3 段 × 10 秒 = 30 秒      ║")
    print("╚══════════════════════════════════════════════════╝")

    # 检查 ComfyUI
    status = req("GET", "/system_stats")
    if not status:
        print("✗ ComfyUI 未在线")
        return
    
    gpu = status.get("devices", [{}])[0]
    print(f"✓ ComfyUI 在线 | GPU: {gpu.get('name', 'unknown')}")
    print(f"  可用显存: {gpu.get('vram_free', 0)/1024/1024/1024:.1f}GB / {gpu.get('vram_total', 0)/1024/1024/1024:.1f}GB")

    # 创建工作流
    workflow = {
        "nodes": [
            # 模型加载
            {"id": 1, "type": "UNETLoader", "pos": [100, 100], "size": [300, 80],
             "widgets_values": ["minimax_h3_fl2va_pruned_int8_convrot.safetensors", "default"]},
            {"id": 2, "type": "CLIPLoader", "pos": [100, 200], "size": [300, 80],
             "widgets_values": ["qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "minimax", "default"]},
            {"id": 3, "type": "VAELoader", "pos": [100, 300], "size": [300, 50],
             "widgets_values": ["minimax_h3_video_vae_fp16.safetensors"]},
            {"id": 4, "type": "VAELoader", "pos": [100, 370], "size": [300, 50],
             "widgets_values": ["minimax_h3_audio_vae_fp32.safetensors"]},
            
            # Sigma Shift
            {"id": 5, "type": "MiniMaxH3SigmaShift", "pos": [100, 450], "size": [300, 50],
             "widgets_values": [12, 3]},
            
            # Clip 1 - Start
            {"id": 10, "type": "H3ContinuousStartV14", "pos": [500, 100], "size": [320, 150],
             "widgets_values": [TEST_PROMPTS[0], 736, 1280, 10.0, "match"],
             "inputs": [
                 {"name": "clip", "type": "CLIP", "link": 101},
                 {"name": "vae", "type": "VAE", "link": 102},
                 {"name": "prompt", "type": "STRING", "widget": {"name": "prompt"}, "link": None},
                 {"name": "width", "type": "INT", "widget": {"name": "width"}, "link": None},
                 {"name": "height", "type": "INT", "widget": {"name": "height"}, "link": None},
                 {"name": "duration", "type": "FLOAT", "widget": {"name": "duration"}, "link": None}
             ]},
             
            # Clip 2 - Continue
            {"id": 20, "type": "H3ContinuousContinueV14", "pos": [500, 300], "size": [320, 150],
             "widgets_values": [TEST_PROMPTS[1], 736, 1280, 10.0, "39"],
             "inputs": [
                 {"name": "clip", "type": "CLIP", "link": 101},
                 {"name": "vae", "type": "VAE", "link": 102},
                 {"name": "prompt", "type": "STRING", "widget": {"name": "prompt"}, "link": None},
                 {"name": "width", "type": "INT", "widget": {"name": "width"}, "link": None},
                 {"name": "height", "type": "INT", "widget": {"name": "height"}, "link": None},
                 {"name": "duration", "type": "FLOAT", "widget": {"name": "duration"}, "link": None}
             ]},
             
            # Clip 3 - Continue
            {"id": 30, "type": "H3ContinuousContinueV14", "pos": [500, 500], "size": [320, 150],
             "widgets_values": [TEST_PROMPTS[2], 736, 1280, 10.0, "39"],
             "inputs": [
                 {"name": "clip", "type": "CLIP", "link": 101},
                 {"name": "vae", "type": "VAE", "link": 102},
                 {"name": "prompt", "type": "STRING", "widget": {"name": "prompt"}, "link": None},
                 {"name": "width", "type": "INT", "widget": {"name": "width"}, "link": None},
                 {"name": "height", "type": "INT", "widget": {"name": "height"}, "link": None},
                 {"name": "duration", "type": "FLOAT", "widget": {"name": "duration"}, "link": None}
             ]},
             
            # SaveVideo
            {"id": 40, "type": "SaveVideo", "pos": [900, 300], "size": [300, 100],
             "widgets_values": ["comic_drama/h3_test", "auto", "auto"],
             "inputs": [
                 {"name": "video", "type": "VIDEO", "link": 200}
             ]}
        ],
        "links": [
            # Model connections
            [101, 1, 0, 10, 0],  # CLIP -> Start
            [101, 1, 0, 20, 0],  # CLIP -> Continue 1
            [101, 1, 0, 30, 0],  # CLIP -> Continue 2
            [102, 3, 0, 10, 1],  # VAE -> Start
            [102, 3, 0, 20, 1],  # VAE -> Continue 1
            [102, 3, 0, 30, 1],  # VAE -> Continue 2
            
            # H3 connections (simplified)
            [200, 30, 0, 40, 0]  # Last clip -> SaveVideo
        ]
    }

    print("\n提交工作流到 ComfyUI...")
    result = req("POST", "/prompt", {"prompt": workflow})
    
    if not result:
        print("✗ 提交失败")
        return
    
    prompt_id = result.get("prompt_id", "")
    print(f"✓ 已提交 (prompt_id={prompt_id})")
    print(f"  预计耗时: 约 10-20 分钟")
    
    # 等待完成
    hist = wait_for_completion(prompt_id)
    
    if hist:
        print("\n生成结果:")
        outputs = hist.get("outputs", {})
        for node_id, output in outputs.items():
            if "images" in output:
                for img in output["images"]:
                    print(f"  图片: {img['filename']}")
            if "videos" in output:
                for vid in output["videos"]:
                    print(f"  视频: {vid['filename']}")
        
        # 列出输出文件
        output_path = Path(OUTPUT_DIR) / "comic_drama" / "h3_test"
        if output_path.exists():
            print("\n生成的视频文件:")
            for f in sorted(output_path.glob("*.mp4")):
                size_mb = f.stat().st_size / 1024 / 1024
                print(f"  {f.name} ({size_mb:.1f} MB)")
        else:
            # 检查根目录
            for f in sorted(Path(OUTPUT_DIR).glob("*h3*.mp4")):
                print(f"  {f.name}")

if __name__ == "__main__":
    main()
