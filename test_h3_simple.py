"""
H3 视频生成测试 - 使用标准节点构建简化工作流
"""

import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

COMFYUI_URL = "http://127.0.0.1:8188"
OUTPUT_DIR = r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output"

# 测试提示词（国漫古风）
TEST_PROMPTS = [
    "云雾缭绕的仙山秘境，苍松翠柏间有一座破败的古道观。镜头缓慢推进，穿过残垣断壁，一个青衣少年正在擦拭一柄布满灰尘的长剑。国漫古风，电影级画面，cinematic push in, Chinese fantasy anime",
    "太初古剑突然迸发刺目的万丈光芒！金色光柱直冲云霄，整个秘境被金色剑气笼罩。古树剧烈摇晃，落叶纷飞。青衣少年被剑气冲击向后倒退，眼中倒映金光。国漫仙侠，史诗感，dramatic lighting, golden explosion",
    "光芒散去，一个玄金色的巨大剑影悬浮在空中，周围环绕太古剑意形成的气浪。少年单膝跪地，双手握拳叩首。剑影化作流光没入少年胸口，少年双眼闪过金色光芒后恢复清明。国漫仙侠，emotional climax, cinematic"
]

def req(method, path, data=None):
    """发送HTTP请求"""
    url = COMFYUI_URL + path
    try:
        if data:
            r = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
        else:
            r = urllib.request.Request(url)
        with urllib.request.urlopen(r, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP Error {e.code}: {e.reason}")
        try:
            body = e.read().decode()
            print(f"  Error details: {body[:500]}")
        except:
            pass
        return None
    except Exception as e:
        print(f"  Error: {e}")
        return None


def build_workflow():
    """构建简化版H3工作流"""
    # 基础配置
    width = 736
    height = 1280  # 竖屏比例
    duration = 10.0  # 每段10秒
    
    # 工作流节点
    nodes = [
        # 模型加载
        {
            "id": 1,
            "type": "UNETLoader",
            "pos": [100, 100],
            "size": [320, 80],
            "widgets_values": ["minimax_h3_fl2va_pruned_int8_convrot.safetensors", "default"]
        },
        {
            "id": 2,
            "type": "CLIPLoader",
            "pos": [100, 200],
            "size": [320, 80],
            "widgets_values": ["qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "minimax", "default"]
        },
        {
            "id": 3,
            "type": "VAELoader",
            "pos": [100, 300],
            "size": [320, 50],
            "widgets_values": ["minimax_h3_video_vae_fp16.safetensors"]
        },
        {
            "id": 4,
            "type": "VAELoader",
            "pos": [100, 370],
            "size": [320, 50],
            "widgets_values": ["minimax_h3_audio_vae_fp32.safetensors"]
        },
        
        # Sigma Shift
        {
            "id": 5,
            "type": "MiniMaxH3SigmaShift",
            "pos": [100, 450],
            "size": [320, 50],
            "widgets_values": [12, 3]
        },
        
        # Clip 1 - Start
        {
            "id": 10,
            "type": "H3ContinuousStartV14",
            "pos": [500, 100],
            "size": [350, 150],
            "widgets_values": [TEST_PROMPTS[0], width, height, duration, "match"]
        },
        
        # Clip 2 - Continue
        {
            "id": 20,
            "type": "H3ContinuousContinueV14",
            "pos": [500, 300],
            "size": [350, 150],
            "widgets_values": [TEST_PROMPTS[1], width, height, duration, "39"]
        },
        
        # Clip 3 - Continue
        {
            "id": 30,
            "type": "H3ContinuousContinueV14",
            "pos": [500, 500],
            "size": [350, 150],
            "widgets_values": [TEST_PROMPTS[2], width, height, duration, "39"]
        },
        
        # SaveVideo
        {
            "id": 40,
            "type": "SaveVideo",
            "pos": [950, 300],
            "size": [320, 100],
            "widgets_values": ["comic_drama/h3_test", "auto", "auto"]
        }
    ]
    
    # 连接关系
    links = [
        # CLIP连接 (node 2 outputs -> all H3 nodes)
        [101, 2, 0, 10, 0],  # CLIP -> Clip 1
        [102, 2, 0, 20, 0],  # CLIP -> Clip 2
        [103, 2, 0, 30, 0],  # CLIP -> Clip 3
        
        # VAE连接 (node 3 outputs -> all H3 nodes)
        [104, 3, 0, 10, 1],  # Video VAE -> Clip 1
        [105, 3, 0, 20, 1],  # Video VAE -> Clip 2
        [106, 3, 0, 30, 1],  # Video VAE -> Clip 3
        
        # Audio VAE连接 (node 4 outputs -> all H3 nodes)
        [107, 4, 0, 10, 2],  # Audio VAE -> Clip 1
        [108, 4, 0, 20, 2],  # Audio VAE -> Clip 2
        [109, 4, 0, 30, 2],  # Audio VAE -> Clip 3
        
        # Model连接 (node 1 outputs -> SigmaShift -> H3 nodes)
        [110, 1, 0, 5, 0],   # Model -> SigmaShift
        [111, 5, 0, 10, 3],  # SigmaShift -> Clip 1
        [112, 5, 0, 20, 3],  # SigmaShift -> Clip 2
        [113, 5, 0, 30, 3],  # SigmaShift -> Clip 3
        
        # H3连接 (Clip 1 -> Clip 2 -> Clip 3 -> SaveVideo)
        [200, 10, 0, 20, 4],  # Clip 1 output -> Clip 2 previous_latent
        [201, 20, 0, 30, 4],  # Clip 2 output -> Clip 3 previous_latent
        [202, 30, 0, 40, 0],  # Clip 3 output -> SaveVideo
    ]
    
    return {
        "nodes": nodes,
        "links": links,
        "version": 1.1
    }


def wait_for_completion(prompt_id, timeout=1800):
    """等待任务完成"""
    print(f"\n  等待生成完成 (prompt_id={prompt_id})...")
    print(f"  预计耗时: 10-20分钟（3段×10秒 H3视频）")
    
    start = time.time()
    while time.time() - start < timeout:
        info = req("GET", f"/history/{prompt_id}")
        if info and prompt_id in info:
            elapsed = int(time.time() - start)
            print(f"  ✓ 生成完成 ({elapsed//60}分钟{elapsed%60}秒)")
            return info[prompt_id]
        
        elapsed = int(time.time() - start)
        if elapsed % 60 == 0:
            status = req("GET", "/queue")
            if status:
                running = len(status.get("queue_running", []))
                print(f"  等待中... {elapsed//60}分钟 | 队列: {running}个任务")
        
        time.sleep(10)
    
    print(f"  ⚠ 超时 ({timeout//60}分钟)")
    return None


def main():
    print("╔══════════════════════════════════════════════════╗")
    print("║     H3 视频生成测试 - 国漫风格 30秒视频         ║")
    print("║     (3段 × 10秒 = 30秒)                         ║")
    print("╚══════════════════════════════════════════════════╝")
    
    # 检查 ComfyUI
    status = req("GET", "/system_stats")
    if not status:
        print("✗ ComfyUI 未在线，请先启动 ComfyUI")
        return
    
    gpu = status.get("devices", [{}])[0]
    print(f"✓ ComfyUI 在线 | GPU: {gpu.get('name', 'unknown')}")
    print(f"  可用显存: {gpu.get('vram_free', 0)/1024/1024/1024:.1f}GB / {gpu.get('vram_total', 0)/1024/1024/1024:.1f}GB")
    
    # 创建工作流
    print("\n构建工作流...")
    workflow = build_workflow()
    print(f"  节点数: {len(workflow['nodes'])}")
    print(f"  连接数: {len(workflow['links'])}")
    
    # 创建输出目录
    os.makedirs(os.path.join(OUTPUT_DIR, "comic_drama", "h3_test"), exist_ok=True)
    
    # 提交
    print("\n提交工作流到 ComfyUI...")
    result = req("POST", "/prompt", {"prompt": workflow})
    
    if not result:
        print("✗ 提交失败")
        return
    
    prompt_id = result.get("prompt_id", "")
    print(f"✓ 已提交 (prompt_id={prompt_id})")
    
    # 等待完成
    hist = wait_for_completion(prompt_id)
    
    if hist:
        print("\n生成结果:")
        outputs = hist.get("outputs", {})
        for nid, out in outputs.items():
            for key in ["images", "videos"]:
                if key in out:
                    for item in out[key]:
                        print(f"  {key}: {item['filename']}")
        
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
    else:
        print("\n⚠ 未获取到生成结果")


if __name__ == "__main__":
    main()
