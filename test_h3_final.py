"""
H3视频生成测试 - 替换UUID节点为已知节点
"""
import json
import urllib.request
import urllib.error
import time
from pathlib import Path

COMFYUI_URL = 'http://127.0.0.1:8188'
OUTPUT_DIR = r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output'
TEST_OUTPUT = 'h3_test_final'

# 测试提示词
TEST_PROMPTS = [
    '云雾缭绕的仙山秘境，苍松翠柏间有一座破败的古道观。镜头缓慢推进，一个青衣少年正在擦拭一柄长剑。国漫古风，电影级画面，cinematic push in, Chinese fantasy anime',
    '太初古剑突然迸发刺目的万丈光芒！金色光柱直冲云霄，整个秘境被金色剑气笼罩。古装仙侠，史诗感，dramatic lighting',
    '光芒散去，一个玄金色的巨大剑影悬浮在空中，周围环绕太古剑意形成的气浪。古装仙侠，emotional climax, cinematic'
]

def main():
    # 加载H3工作流
    wf = json.load(open(r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\user\default\workflows\H3信号10段测试001.json', encoding='utf-8'))
    print(f'原始工作流: {len(wf["nodes"])} 节点')

    # 查找UUID节点并替换
    uuid_nodes = [n for n in wf['nodes'] if len(n['type']) == 36 and '-' in n['type']]
    print(f'找到 {len(uuid_nodes)} 个UUID节点，正在替换...')

    for i, n in enumerate(uuid_nodes):
        nid = str(n['id'])
        wv = n.get('widgets_values', [])

        # 获取clip编号
        clip_num = wv[3].split('Clip ')[-1] if len(wv) > 3 and 'Clip' in wv[3] else '1'

        # 创建新的MiniMaxH3ReferenceToVideo节点
        new_node = {
            'class_type': 'MiniMaxH3ReferenceToVideo',
            'inputs': {
                'clip': ['1', 0],  # 连接到CLIP
                'vae': ['2', 0],   # 连接到VAE
                'audio_vae': ['2', 0],
                'prompt': TEST_PROMPTS[int(clip_num) - 1] if int(clip_num) <= len(TEST_PROMPTS) else TEST_PROMPTS[0],
                'width': wv[0] if len(wv) > 0 else 704,
                'height': wv[1] if len(wv) > 1 else 576,
                'length': wv[2] if len(wv) > 2 else 124,
                'ref_image_size': 'match',
                'seed': 42,
                'denoise': 0.88,
                'guidance_scale': 5.0,
                'turbo_lora_scale': 1.0,
                'ref_image_strength': 0.9,
                'model': ['3', 0],  # 连接到UNET
                'ref_images': [],
                'conditioning': ['4', 0]  # 连接到正向conditioning
            }
        }
        wf['nodes'][i] = new_node
        print(f'  替换节点 {nid}')

    # 查找并更新SaveVideo节点
    for n in wf['nodes']:
        if n.get('type') == 'SaveVideo':
            n['widgets_values'][0] = TEST_OUTPUT
            print(f'更新输出路径: {TEST_OUTPUT}')

    # 转换为API格式
    nodes = {}
    for n in wf['nodes']:
        nid = str(n.get('id', ''))
        if not nid:
            continue
        node_type = n.get('type', '')
        wv = n.get('widgets_values', [])

        node = {'class_type': node_type, 'inputs': {}}

        # 处理不同节点类型
        if node_type == 'UNETLoader':
            node['inputs']['unet_name'] = wv[0] if wv else 'minimax-h3\\minimax_h3_ref2va_pruned_int8_convrot.safetensors'
            node['inputs']['weight_dtype'] = wv[1] if len(wv) > 1 else 'default'
        elif node_type == 'VAELoader':
            node['inputs']['vae_name'] = wv[0] if wv else 'MiniMax\\minimax_h3_audio_vae_fp32.safetensors'
        elif node_type == 'CLIPLoader':
            node['inputs']['clip_name'] = wv[0] if wv else 'minimax-h3\\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
            node['inputs']['type'] = wv[1] if len(wv) > 1 else 'minimax'
        elif node_type == 'CLIPTextEncode':
            node['inputs']['text'] = wv[0] if wv else ''
            node['inputs']['clip'] = ['1', 0]  # 连接到CLIP
        elif node_type == 'EmptyMiniMaxH3LatentAV':
            node['inputs']['width'] = wv[0] if len(wv) > 0 else 704
            node['inputs']['height'] = wv[1] if len(wv) > 1 else 576
            node['inputs']['length'] = wv[2] if len(wv) > 2 else 124
            node['inputs']['batch_size'] = wv[3] if len(wv) > 3 else 1
            node['inputs']['device'] = 'cuda'
        elif node_type == 'CreateVideo':
            node['inputs']['images'] = ['5', 0]  # 连接到VAEDecode
            node['inputs']['fps'] = 24
        elif node_type == 'SaveVideo':
            node['inputs']['video'] = ['5', 0]
            node['inputs']['filename_prefix'] = TEST_OUTPUT
        elif node_type == 'VAEDecode':
            node['inputs']['samples'] = ['6', 0]  # 连接到H3节点
            node['inputs']['vae'] = ['2', 0]
        else:
            # 其他节点尝试从inputs映射
            for inp in n.get('inputs', []):
                if inp.get('value') is not None and not inp.get('link'):
                    node['inputs'][inp['name']] = inp['value']

        nodes[nid] = node

    print(f'\\n转换后: {len(nodes)} 节点')

    # 提交
    prompt_data = [0, '', nodes, {'create_time': int(time.time() * 1000)}, list(nodes.keys())]

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
            if TEST_OUTPUT in str(v):
                print(f'  OK {v.relative_to(OUTPUT_DIR)} ({v.stat().st_size/1024/1024:.1f}MB)')

if __name__ == '__main__':
    main()
