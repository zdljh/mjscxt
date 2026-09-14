"""
H3视频生成测试 - 使用正确的API格式
"""
import json
import urllib.request
import urllib.error
import time
from pathlib import Path

COMFYUI_URL = 'http://127.0.0.1:8188'
OUTPUT_DIR = r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output'
TEST_OUTPUT = 'h3_test_sword'

# 测试提示词
TEST_PROMPT = '云雾缭绕的仙山秘境，苍松翠柏间有一座破败的古道观。镜头缓慢推进，一个青衣少年正在擦拭一柄长剑。国漫古风，电影级画面，cinematic push in, Chinese fantasy anime, 8k, highly detailed'

def main():
    # 获取模型信息
    r = urllib.request.urlopen(f'{COMFYUI_URL}/object_info', timeout=5)
    obj_info = json.loads(r.read())

    # UNETLoader的unet_name选项
    unet_loader_info = obj_info.get('UNETLoader', {})
    unet_names = unet_loader_info.get('input', {}).get('required', {}).get('unet_name', [[]])[0]

    # 找到H3模型
    h3_model = None
    for m in unet_names:
        if 'minimax-h3' in m.lower():
            h3_model = m
            break
    print(f'使用H3模型: {h3_model}')

    # 获取VAE
    vae_loader_info = obj_info.get('VAELoader', {})
    vae_names = vae_loader_info.get('input', {}).get('required', {}).get('vae_name', [[]])[0]
    h3_vae = [v for v in vae_names if 'minimax' in v.lower()]
    vae_name = h3_vae[0] if h3_vae else vae_names[0]
    print(f'使用VAE: {vae_name}')

    # 获取CLIP
    clip_loader_info = obj_info.get('CLIPLoader', {})
    clip_names = clip_loader_info.get('input', {}).get('required', {}).get('clip_name', [[]])[0]
    clip_name = clip_names[0] if clip_names else 'minimax-h3\\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
    print(f'使用CLIP: {clip_name}')

    # 构建工作流节点 - 使用正确的格式
    nodes = {}

    # 1. UNETLoader
    nodes['1'] = {
        'class_type': 'UNETLoader',
        'inputs': {
            'unet_name': h3_model,
            'weight_dtype': 'default'
        }
    }

    # 2. CLIPLoader
    nodes['2'] = {
        'class_type': 'CLIPLoader',
        'inputs': {
            'clip_name': clip_name,
            'type': 'minimax',
            'device': 'default'
        }
    }

    # 3. VAELoader
    nodes['3'] = {
        'class_type': 'VAELoader',
        'inputs': {
            'vae_name': vae_name
        }
    }

    # 4. CLIPTextEncode - 正向提示词
    nodes['4'] = {
        'class_type': 'CLIPTextEncode',
        'inputs': {
            'text': TEST_PROMPT,
            'clip': ['2', 0]
        }
    }

    # 5. CLIPTextEncode - 负向提示词
    nodes['5'] = {
        'class_type': 'CLIPTextEncode',
        'inputs': {
            'text': 'low quality, blurry, distorted, watermark, text, worst quality',
            'clip': ['2', 0]
        }
    }

    # 6. EmptyMiniMaxH3LatentAV
    nodes['6'] = {
        'class_type': 'EmptyMiniMaxH3LatentAV',
        'inputs': {
            'width': 704,
            'height': 576,
            'length': 124,
            'batch_size': 1,
            'device': 'cuda'
        }
    }

    # 7. MiniMaxH3ReferenceToVideo - 核心生成节点
    nodes['7'] = {
        'class_type': 'MiniMaxH3ReferenceToVideo',
        'inputs': {
            'clip': ['2', 0],
            'vae': ['3', 0],
            'audio_vae': ['3', 0],
            'prompt': TEST_PROMPT,
            'width': 704,
            'height': 576,
            'length': 124,
            'ref_image_size': 'match',
            'seed': 42,
            'denoise': 0.88,
            'guidance_scale': 5.0,
            'turbo_lora_scale': 1.0,
            'ref_image_strength': 0.9,
            'model': ['1', 0],
            'ref_images': [],
            'conditioning': ['4', 0]
        }
    }

    # 8. VAEDecode
    nodes['8'] = {
        'class_type': 'VAEDecode',
        'inputs': {
            'samples': ['7', 0],
            'vae': ['3', 0]
        }
    }

    # 9. CreateVideo
    nodes['9'] = {
        'class_type': 'CreateVideo',
        'inputs': {
            'images': ['8', 0],
            'fps': 24
        }
    }

    print(f'\n工作流: {len(nodes)} 节点')

    # 正确的格式
    prompt_data = [
        0,  # extra_models_config (整数)
        '',  # prompt_id (空字符串，服务器会生成)
        nodes,
        {'create_time': int(time.time() * 1000)},
        list(nodes.keys())
    ]

    # 提交
    print('提交工作流...')
    try:
        r = urllib.request.Request(
            f'{COMFYUI_URL}/prompt',
            data=json.dumps(prompt_data).encode(),
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(r, timeout=30) as resp:
            result = json.loads(resp.read())
            prompt_id = result.get('prompt_id', '')
            print(f'成功! Prompt ID: {prompt_id}')
    except urllib.error.HTTPError as e:
        print(f'HTTP {e.code}: {e.read().decode()[:1000]}')
        return
    except Exception as e:
        print(f'错误: {e}')
        import traceback
        traceback.print_exc()
        return

    # 等待完成
    print('\n等待生成完成...')
    start_time = time.time()
    last_node = ''

    for i in range(300):
        time.sleep(10)
        elapsed = time.time() - start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        try:
            q_r = urllib.request.urlopen(f'{COMFYUI_URL}/queue', timeout=5)
            q_data = json.loads(q_r.read())
            running = q_data.get('queue_running', [])
            if running:
                node_type = running[0][2].get('class_type', 'unknown')
                if node_type != last_node:
                    print(f'  [{mins:02d}:{secs:02d}] {node_type}')
                    last_node = node_type
        except:
            pass

        r = urllib.request.urlopen(f'{COMFYUI_URL}/history', timeout=5)
        hist = json.loads(r.read())
        if prompt_id in hist:
            data = hist[prompt_id]
            outputs = data.get('outputs', {})
            print(f'\n完成! 状态: {data.get("status", {}).get("status_str")}')

            for nid, out in outputs.items():
                for key in ['images', 'videos']:
                    if key in out:
                        for item in out[key]:
                            filename = item.get('filename', '')
                            subfolder = item.get('subfolder', '')
                            print(f'  {key}: {subfolder}/{filename}')
            break
        elif i % 30 == 0:
            print(f'  [{mins:02d}:{secs:02d}] 生成中...')

    # 检查输出
    print('\n检查输出文件...')
    for pattern in ['*.mp4', '*.webm']:
        found = list(Path(OUTPUT_DIR).rglob(pattern))
        recent = sorted(found, key=lambda x: x.stat().st_mtime, reverse=True)[:5]
        for v in recent:
            if TEST_OUTPUT in str(v) or 'test_sword' in str(v):
                print(f'  OK {v.relative_to(OUTPUT_DIR)} ({v.stat().st_size/1024/1024:.1f}MB)')

if __name__ == '__main__':
    main()
