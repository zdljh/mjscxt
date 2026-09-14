"""
H3视频生成端到端测试
使用正确的ComfyUI API格式
"""
import json
import urllib.request
import urllib.error
import time
from pathlib import Path

COMFYUI_URL = 'http://127.0.0.1:8188'
OUTPUT_DIR = Path(r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output')

def req(method, path, data=None):
    url = f'{COMFYUI_URL}{path}'
    try:
        if data:
            r = urllib.request.Request(url, data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
        else:
            r = urllib.request.Request(url)
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        print(f'HTTP {e.code}: {body}')
        return None
    except Exception as e:
        print(f'Error: {e}')
        return None

def get_status():
    """检查ComfyUI状态"""
    r = req('GET', '/system_stats')
    if r:
        for dev in r.get('devices', []):
            free = dev.get('vram_free', 0) / 1024 / 1024 / 1024
            total = dev.get('vram_total', 0) / 1024 / 1024 / 1024
            print(f'GPU: {dev["name"]} | 空闲: {free:.1f}GB / 总计: {total:.1f}GB')
        return r
    return None

def wait_for_completion(prompt_id, timeout=1800):
    """等待工作流完成"""
    start = time.time()
    last_node = ''
    for i in range(timeout // 10):
        time.sleep(10)
        elapsed = time.time() - start
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        # 检查队列
        q = req('GET', '/queue')
        running = q.get('queue_running', []) if q else []
        if running:
            node_type = running[0][2].get('class_type', 'unknown')
            if node_type != last_node:
                print(f'  [{mins:02d}:{secs:02d}] {node_type}')
                last_node = node_type

        # 检查完成
        hist = req('GET', f'/history/{prompt_id}')
        if hist and prompt_id in hist:
            data = hist[prompt_id]
            status = data.get('status', {}).get('status_str', 'unknown')
            outputs = data.get('outputs', {})
            print(f'\n完成! 状态: {status}')
            for nid, out in outputs.items():
                for key in ['images', 'videos']:
                    if key in out:
                        for item in out[key]:
                            subfolder = item.get('subfolder', '')
                            filename = item.get('filename', '')
                            print(f'  {key}: {subfolder}/{filename}')
                            # 返回完整路径
                            if subfolder:
                                return (OUTPUT_DIR / subfolder / filename).absolute()
                            else:
                                return (OUTPUT_DIR / filename).absolute()
            return None
        elif i % 30 == 0:
            print(f'  [{mins:02d}:{secs:02d}] 生成中...')

    print('超时!')
    return None

def main():
    print('=' * 60)
    print('漫剧生成系统 - H3视频测试')
    print('=' * 60)

    # 1. 检查状态
    print('\n[1/4] 检查 ComfyUI 状态...')
    r = req('GET', '/system_stats')
    if not r:
        print('ERROR: ComfyUI 未在线')
        return
    for dev in r.get('devices', []):
        free = dev.get('vram_free', 0) / 1024 / 1024 / 1024
        total = dev.get('vram_total', 0) / 1024 / 1024 / 1024
        print(f'GPU: {dev["name"]} | 空闲: {free:.1f}GB / 总计: {total:.1f}GB')
    print('ComfyUI 在线')

    # 2. 构建H3工作流
    print('\n[2/4] 构建H3工作流...')

    # 测试提示词
    prompts = [
        'A young Chinese martial artist in blue robes stands in a misty ancient temple, holding a glowing sword. Epic fantasy anime style, cinematic lighting, detailed, 8k',
        'A golden sword spirit emerges from the sword with brilliant light, surrounding the young warrior. Chinese fantasy epic, dramatic lighting, cinematic',
        'The golden sword spirit merges into the warrior, his eyes glow gold. Emotional climax, cinematic, Chinese fantasy anime style'
    ]

    # 使用示例工作流作为基础
    example_wf = json.load(open(
        r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\custom_nodes\Herrgotts-H3-Infinite-Continuation-Suite-main\examples\Herrgotts_H3_Infinite_v1.4_01_Start.json',
        encoding='utf-8'
    ))

    # 转换为API格式
    nodes = {}
    for n in example_wf['nodes']:
        if n['type'] == 'MarkdownNote':
            continue  # 跳过MarkdownNote

        nid = str(n['id'])
        node_type = n['type']
        wv = n.get('widgets_values', [])

        node = {'class_type': node_type, 'inputs': {}}

        # 根据节点类型映射inputs
        if node_type == 'UNETLoader':
            node['inputs']['unet_name'] = wv[0] if wv else 'minimax-h3/MiniMax_H3_Ref2VA_Int8.safetensors'
            node['inputs']['weight_dtype'] = wv[1] if len(wv) > 1 else 'default'
        elif node_type == 'VAELoader':
            node['inputs']['vae_name'] = wv[0] if wv else 'minimax-h3/MiniMax_H3_VAE_bf16.safetensors'
        elif node_type == 'CLIPLoader':
            node['inputs']['clip_name'] = wv[0] if wv else 'minimax-h3/mt5-xxl.fp16.safetensors'
        elif node_type == 'KSamplerSelect':
            node['inputs']['sampler_name'] = wv[0] if wv else 'euler'
        elif node_type == 'BasicScheduler':
            node['inputs']['scheduler_name'] = wv[0] if wv else 'normal'
        elif node_type == 'RandomNoise':
            node['inputs']['noise_seed'] = wv[0] if wv else 42
        elif node_type == 'SaveVideo':
            node['inputs']['filename_prefix'] = wv[0] if wv else 'h3_test_final'
        elif node_type == 'CreateVideo':
            pass  # 不需要额外inputs
        elif node_type == 'VAEDecode':
            pass
        elif node_type == 'VAEDecodeAudio':
            pass
        elif node_type == 'MiniMaxH3SigmaShift':
            pass
        elif node_type == 'PathchSageAttentionKJ':
            pass
        elif node_type == 'H3ContinuousSaveLatent':
            pass
        elif node_type == 'H3ContinuousStitchOutputV14':
            pass
        elif node_type == 'H3ContinuousAnalyzeHandoverV14':
            pass
        elif node_type in ['MiniMaxH3ReferenceToVideo', 'H3ContinuousStartV14']:
            # 这些是主要的H3生成节点
            # 从widgets获取参数
            if len(wv) >= 1:
                node['inputs']['prompt'] = wv[0]
            if len(wv) >= 2:
                node['inputs']['width'] = wv[1]
            if len(wv) >= 3:
                node['inputs']['height'] = wv[2]
            if len(wv) >= 4:
                node['inputs']['length'] = wv[3]
            if len(wv) >= 5:
                node['inputs']['ref_image_size'] = wv[4]
            if len(wv) >= 6:
                node['inputs']['seed'] = wv[5]
            if len(wv) >= 7:
                node['inputs']['denoise'] = wv[6]
        elif node_type == 'BasicGuider':
            pass  # 从其他节点连接
        elif node_type == 'SamplerCustomAdvanced':
            pass  # 从其他节点连接
        else:
            # 尝试从inputs映射
            for inp in n.get('inputs', []):
                if inp.get('value') is not None:
                    node['inputs'][inp['name']] = inp['value']

        nodes[nid] = node

    print(f'转换后: {len(nodes)} 节点')

    # 更新提示词
    updated = 0
    for nid, node in nodes.items():
        if node['class_type'] == 'MiniMaxH3ReferenceToVideo':
            if 'inputs' in node and 'prompt' in node['inputs']:
                old_prompt = node['inputs']['prompt']
                # 只更新前3个提示词
                if updated < len(prompts):
                    node['inputs']['prompt'] = prompts[updated]
                    print(f'  更新提示词 {updated+1}: {prompts[updated][:50]}...')
                    updated += 1

    # 更新输出路径
    for nid, node in nodes.items():
        if node['class_type'] == 'SaveVideo':
            node['inputs']['filename_prefix'] = 'h3_sword_awakening_test'
            print(f'  输出: h3_sword_awakening_test')

    # 3. 提交工作流
    print('\n[3/4] 提交到 ComfyUI...')
    prompt_data = [0, '', nodes, {'create_time': int(time.time() * 1000)}, list(nodes.keys())]

    result = req('POST', '/prompt', prompt_data)
    if not result:
        print('提交失败')
        return

    prompt_id = result.get('prompt_id', '')
    print(f'成功! Prompt ID: {prompt_id}')

    # 4. 等待完成
    print('\n[4/4] 等待生成完成...')
    output_path = wait_for_completion(prompt_id)

    if output_path:
        print(f'\n{"="*60}')
        print(f'视频已生成: {output_path}')
        print(f'{"="*60}')
    else:
        print('\n未找到输出文件')

if __name__ == '__main__':
    main()
