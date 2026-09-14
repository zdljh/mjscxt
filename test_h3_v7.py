# -*- coding: utf-8 -*-
"""
H3 视频生成测试脚本
使用 ComfyUI API 直接构建和提交工作流
"""
import json
import urllib.request
import urllib.error
import time
from pathlib import Path

COMFYUI_URL = 'http://127.0.0.1:8188'
OUTPUT_DIR = Path(r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output')
RESULTS_DIR = Path(r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\output')

def api_request(path, data=None, timeout=60):
    """发送API请求"""
    url = f'{COMFYUI_URL}{path}'
    try:
        if data is not None:
            req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                       headers={'Content-Type': 'application/json'})
        else:
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:1000]
        raise Exception(f'HTTP {e.code}: {body}')
    except Exception as e:
        raise Exception(f'Request failed: {e}')

def check_comfyui():
    """检查ComfyUI状态"""
    try:
        data = api_request('/system_stats')
        devices = data.get('devices', [])
        if devices:
            gpu = devices[0]
            print(f"ComfyUI 在线 - GPU: {gpu['name']}")
            print(f"VRAM: {gpu.get('vram_free', 0)/1024/1024/1024:.1f}GB / {gpu.get('vram_total', 0)/1024/1024/1024:.1f}GB")
            return True
    except Exception as e:
        print(f"ComfyUI 连接失败: {e}")
    return False

def get_node_info():
    """获取所有节点信息"""
    return api_request('/object_info')

def build_h3_workflow(prompts, ref_image_path=None):
    """构建H3视频生成工作流"""
    nodes = {}
    node_id = 1

    # 1. UNETLoader
    nodes[str(node_id)] = {
        'class_type': 'UNETLoader',
        'inputs': {
            'unet_name': 'minimax-h3/minimax_h3_ref2va_pruned_int8_convrot.safetensors',
            'weight_dtype': 'fp8_e4m3fn'
        }
    }
    unet_id = str(node_id)
    node_id += 1

    # 2. VAELoader
    nodes[str(node_id)] = {
        'class_type': 'VAELoader',
        'inputs': {
            'vae_name': 'minimax-h3/minimax_h3_video_vae_fp16.safetensors'
        }
    }
    vae_id = str(node_id)
    node_id += 1

    # 3. CLIPLoader (type='minimax')
    nodes[str(node_id)] = {
        'class_type': 'CLIPLoader',
        'inputs': {
            'clip_name': 'minimax-h3/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
            'type': 'minimax'
        }
    }
    clip_id = str(node_id)
    node_id += 1

    # 4. RandomNoise
    nodes[str(node_id)] = {
        'class_type': 'RandomNoise',
        'inputs': {
            'noise_seed': 42
        }
    }
    noise_id = str(node_id)
    node_id += 1

    # 5. H3ContinuousStartV14 - 第一个视频片段
    nodes[str(node_id)] = {
        'class_type': 'H3ContinuousStartV14',
        'inputs': {
            'clip': [clip_id, 0],
            'vae': [vae_id, 0],
            'prompt': prompts[0] if prompts else '',
            'width': 832,  # 降低分辨率以适应2.6GB VRAM
            'height': 480,
            'duration': 5.0,  # 5秒
            'ref_image_size': 'match'
        }
    }
    h3_1_id = str(node_id)
    node_id += 1

    # 6. H3ContinuousContinueV14 - 第二个片段（如果有）
    if len(prompts) > 1:
        nodes[str(node_id)] = {
            'class_type': 'H3ContinuousContinueV14',
            'inputs': {
                'clip': [clip_id, 0],
                'vae': [vae_id, 0],
                'prompt': prompts[1],
                'width': 832,
                'height': 480,
                'duration': 5.0,
                'ref_image_size': 'match',
                'prev_latent': [h3_1_id, 1]  # 连接前一个片段的latent
            }
        }
        h3_2_id = str(node_id)
        node_id += 1
    else:
        h3_2_id = h3_1_id

    # 7. H3ContinuousSeamlessJoinV14 - 合并片段
    if len(prompts) > 1:
        nodes[str(node_id)] = {
            'class_type': 'H3ContinuousSeamlessJoinV14',
            'inputs': {
                'prev_images': [h3_1_id, 0],
                'next_images': [h3_2_id, 0]
            }
        }
        join_id = str(node_id)
        node_id += 1
    else:
        join_id = h3_1_id

    # 8. VAEDecode - 解码latent到图像
    nodes[str(node_id)] = {
        'class_type': 'VAEDecode',
        'inputs': {
            'samples': [join_id, 1],  # LATENT输出
            'vae': [vae_id, 0]
        }
    }
    decode_id = str(node_id)
    node_id += 1

    # 9. CreateVideo - 创建视频
    nodes[str(node_id)] = {
        'class_type': 'CreateVideo',
        'inputs': {
            'images': [decode_id, 0],
            'fps': 24.0
        }
    }
    video_id = str(node_id)
    node_id += 1

    # 10. SaveVideo - 保存视频
    output_path = 'h3_test_sword'
    nodes[str(node_id)] = {
        'class_type': 'SaveVideo',
        'inputs': {
            'video': [video_id, 0],
            'filename_prefix': output_path,
            'format': 'auto'
        }
    }

    return nodes

def submit_workflow(nodes):
    """提交工作流到ComfyUI"""
    # 构建prompt数据
    prompt_data = [
        0,  # execution_count
        '',  # prompt_id
        nodes,
        {'create_time': int(time.time() * 1000)},
        list(nodes.keys())  # execution_list
    ]

    print(f"提交工作流: {len(nodes)} 个节点")
    result = api_request('/prompt', prompt_data, timeout=30)
    prompt_id = result.get('prompt_id', '')
    print(f"成功! Prompt ID: {prompt_id}")
    return prompt_id

def wait_for_completion(prompt_id, timeout_minutes=30):
    """等待工作流完成"""
    print(f"等待生成完成 (最长{timeout_minutes}分钟)...")
    start = time.time()
    last_node = ''

    for i in range(timeout_minutes * 6):  # 每10秒检查一次
        time.sleep(10)
        elapsed = time.time() - start
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        # 检查队列
        try:
            queue_data = api_request('/queue')
            running = queue_data.get('queue_running', [])
            if running:
                node_type = running[0][2].get('class_type', 'unknown')
                if node_type != last_node:
                    print(f"  [{mins:02d}:{secs:02d}] 正在执行: {node_type}")
                    last_node = node_type
        except:
            pass

        # 检查完成状态
        try:
            hist = api_request(f'/history/{prompt_id}')
            if prompt_id in hist:
                data = hist[prompt_id]
                status = data.get('status', {}).get('status_str', 'unknown')
                outputs = data.get('outputs', {})
                print(f"\n完成! 状态: {status}")

                results = []
                for nid, out in outputs.items():
                    for key in ['images', 'videos']:
                        if key in out:
                            for item in out[key]:
                                subfolder = item.get('subfolder', '')
                                filename = item.get('filename', '')
                                print(f"  {key}: {subfolder}/{filename}")
                                full_path = OUTPUT_DIR / subfolder / filename if subfolder else OUTPUT_DIR / filename
                                results.append(full_path)
                                print(f"  完整路径: {full_path}")

                return results
        except:
            pass

        if i % 6 == 0:  # 每60秒打印一次
            print(f"  [{mins:02d}:{secs:02d}] 生成中...")

    print("超时!")
    return []

def main():
    print("=" * 50)
    print("H3 视频生成测试")
    print("=" * 50)

    # 1. 检查ComfyUI状态
    print("\n[1] 检查ComfyUI状态...")
    if not check_comfyui():
        print("ERROR: ComfyUI 未在线")
        return

    # 2. 获取节点信息
    print("\n[2] 获取节点信息...")
    node_info = get_node_info()
    print(f"可用节点类型: {len(node_info)}")

    # 3. 构建H3工作流
    print("\n[3] 构建H3视频生成工作流...")
    prompts = [
        'A young Chinese martial artist in blue robes stands in a misty ancient temple, holding a glowing sword. Epic fantasy anime style, cinematic lighting, detailed, 8k, Chinese xianxia',
        'A golden sword spirit emerges from the sword with brilliant light, surrounding the young warrior. Chinese fantasy epic, dramatic lighting, cinematic, fire and light particles',
        'The golden sword spirit merges into the warrior, his eyes glow gold. Emotional climax, cinematic, Chinese fantasy anime style, dramatic transformation'
    ]

    nodes = build_h3_workflow(prompts)
    print(f"工作流包含 {len(nodes)} 个节点")

    # 4. 提交工作流
    print("\n[4] 提交工作流...")
    prompt_id = submit_workflow(nodes)

    # 5. 等待完成
    print("\n[5] 等待生成完成...")
    results = wait_for_completion(prompt_id, timeout_minutes=20)

    # 6. 检查结果
    print("\n" + "=" * 50)
    if results:
        print(f"成功! 生成 {len(results)} 个文件:")
        for r in results:
            print(f"  {r}")
    else:
        print("未生成任何文件")
    print("=" * 50)

if __name__ == '__main__':
    main()
