"""
H3 视频生成测试 - 直接使用标准节点构建工作流
"""
import json
import urllib.request
import urllib.error
import time
from pathlib import Path

COMFYUI_URL = 'http://127.0.0.1:8188'
OUTPUT_DIR = r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output'
TEST_OUTPUT = 'h3_test_sword_awakening'

# 测试提示词
TEST_PROMPTS = [
    '云雾缭绕的仙山秘境，苍松翠柏间有一座破败的古道观。镜头缓慢推进，穿过残垣断壁，一个青衣少年正在擦拭一柄布满灰尘的长剑。国漫古风，电影级画面，cinematic push in, Chinese fantasy anime',
    '太初古剑突然迸发刺目的万丈光芒！金色光柱直冲云霄，整个秘境被金色剑气笼罩。古树剧烈摇晃，落叶纷飞。青衣少年被剑气冲击向后倒退，眼中倒映金光。史诗感，dramatic lighting, golden explosion',
    '光芒散去，一个玄金色的巨大剑影悬浮在空中，周围环绕太古剑意形成的气浪。少年单膝跪地，双手握拳叩首。剑影化作流光没入少年胸口，少年双眼闪过金色光芒后恢复清明。国漫仙侠，emotional climax, cinematic'
]

def get_next_id(nodes):
    """获取下一个可用的节点ID"""
    return max(nodes.keys()) + 1 if nodes else 1

def main():
    # 获取现有工作流作为基础
    r = urllib.request.urlopen(f'{COMFYUI_URL}/prompt', timeout=5)
    current = json.loads(r.read())
    
    # 如果当前没有加载工作流，创建一个最小的H3工作流
    if not current:
        print('No workflow loaded, creating minimal H3 test workflow...')
    
    # 加载原始H3工作流
    wf = json.load(open(r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\user\default\workflows\H3信号10段测试001.json', encoding='utf-8'))
    
    # 将工作流转换为正确的API格式
    nodes = {}
    for n in wf['nodes']:
        nid = str(n['id'])
        node = {
            'class_type': n['type'],
            'inputs': {}
        }
        
        wv = n.get('widgets_values', [])
        
        # 处理不同节点类型的inputs
        if n['type'] == 'CLIPTextEncode':
            node['inputs']['text'] = wv[0] if wv else ''
        elif n['type'] == 'KSampler':
            # KSampler inputs
            node['inputs']['seed'] = wv[0] if len(wv) > 0 else 42
            node['inputs']['steps'] = wv[1] if len(wv) > 1 else 20
            node['inputs']['cfg'] = wv[2] if len(wv) > 2 else 2.0
            node['inputs']['sampler_name'] = wv[3] if len(wv) > 3 else 'euler'
            node['inputs']['scheduler'] = wv[4] if len(wv) > 4 else 'normal'
            node['inputs']['noise'] = '{}'
            node['inputs']['guider'] = '{}'
            node['inputs']['sampler_state'] = '{}'
            node['inputs']['denoise'] = wv[5] if len(wv) > 5 else 1.0
            node['inputs']['model'] = '{}'
            node['inputs']['positive'] = '{}'
            node['inputs']['negative'] = '{}'
            node['inputs']['latent_image'] = '{}'
        elif n['type'] == 'EmptySD3LatentImage':
            node['inputs']['width'] = wv[0] if len(wv) > 0 else 1344
            node['inputs']['height'] = wv[1] if len(wv) > 1 else 768
            node['inputs']['batch_size'] = wv[2] if len(wv) > 2 else 1
        elif n['type'] == 'SaveVideo':
            node['inputs']['filename_prefix'] = TEST_OUTPUT
            node['inputs']['ffmpeg_path'] = r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ffprobe\ffmpeg.exe'
        elif n['type'] == 'H3ContinuousSeamlessJoinV14':
            node['inputs']['next_output_mode'] = 'video'
            node['inputs']['next_head_context_frames'] = 2
            node['inputs']['video_crossfade_frames'] = 6
            node['inputs']['audio_crossfade_ms'] = 1000
            node['inputs']['luminance_match'] = True
            node['inputs']['luminance_fade_frames'] = 10
            node['inputs']['max_luminance_correction_percent'] = 15
            node['inputs']['max_safe_tail_bridge_frames'] = 6
        elif n['type'] == 'MiniMaxH3ReferenceToVideo':
            node['inputs']['clip'] = '{}'
            node['inputs']['vae'] = '{}'
            node['inputs']['audio_vae'] = '{}'
            node['inputs']['prompt'] = wv[0] if wv else ''
            node['inputs']['width'] = wv[1] if len(wv) > 1 else 1344
            node['inputs']['height'] = wv[2] if len(wv) > 2 else 768
            node['inputs']['length'] = wv[3] if len(wv) > 3 else 124
            node['inputs']['ref_image_size'] = wv[4] if len(wv) > 4 else 'match'
            node['inputs']['seed'] = wv[5] if len(wv) > 5 else 42
            node['inputs']['denoise'] = wv[6] if len(wv) > 6 else 0.88
            node['inputs']['guidance_scale'] = wv[7] if len(wv) > 7 else 5.0
            node['inputs']['turbo_lora_scale'] = wv[8] if len(wv) > 8 else 1.0
            node['inputs']['ref_image_strength'] = wv[9] if len(wv) > 9 else 0.9
            node['inputs']['model'] = '{}'
            node['inputs']['ref_images'] = '{}'
            node['inputs']['conditioning'] = '{}'
        elif 'Set_' in n.get('title', ''):
            # SetNode - 存储变量
            pass
        else:
            # 其他节点 - 尝试从widgets_values映射
            input_names = [inp['name'] for inp in n.get('inputs', []) 
                          if not inp.get('link') and inp.get('value') is not None]
            for i, name in enumerate(input_names):
                if i < len(wv):
                    node['inputs'][name] = wv[i]
        
        nodes[nid] = node
    
    # 更新提示词
    prompt_count = 0
    for nid, node in nodes.items():
        if node['class_type'] == 'MiniMaxH3ReferenceToVideo' and 'prompt' in node['inputs']:
            idx = int(nid) % 3  # 简单映射到3个提示词
            if idx < len(TEST_PROMPTS):
                node['inputs']['prompt'] = TEST_PROMPTS[idx]
                prompt_count += 1
    
    print(f'更新 {prompt_count} 个提示词')
    
    # 构建完整的prompt数据
    prompt_data = [
        {},  # extra_models_config_data (empty)
        '',  # prompt_id (will be filled by server)
        nodes
    ]
    
    # 提交
    print('提交H3视频生成任务...')
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
        print(f'HTTP {e.code}: {e.read().decode()[:500]}')
        return
    except Exception as e:
        print(f'错误: {e}')
        return
    
    # 等待完成
    print('等待生成完成...')
    start_time = time.time()
    last_node = ''
    
    for i in range(300):  # 最多50分钟
        time.sleep(10)
        elapsed = time.time() - start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        
        # 检查队列
        try:
            q_r = urllib.request.urlopen(f'{COMFYUI_URL}/queue', timeout=5)
            q_data = json.loads(q_r.read())
            running = q_data.get('queue_running', [])
            if running:
                node_type = running[0][2].get('class_type', 'unknown')
                if node_type != last_node:
                    print(f'  [{mins:02d}:{secs:02d}] 运行中: {node_type}')
                    last_node = node_type
        except:
            pass
        
        # 检查完成
        r = urllib.request.urlopen(f'{COMFYUI_URL}/history', timeout=5)
        hist = json.loads(r.read())
        if prompt_id in hist:
            data = hist[prompt_id]
            outputs = data.get('outputs', {})
            print(f'\n完成! 状态: {data.get("status", {}).get("status_str")}')
            
            # 显示输出
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
    
    # 检查输出文件
    output_path = Path(OUTPUT_DIR) / TEST_OUTPUT
    if output_path.exists():
        videos = list(output_path.glob('*.mp4'))
        if videos:
            print(f'\n输出文件:')
            for v in sorted(videos, key=lambda x: x.stat().st_mtime, reverse=True)[:3]:
                print(f'  {v.name} ({v.stat().st_size/1024/1024:.1f}MB)')
        else:
            print(f'\n未找到输出文件在 {output_path}')
    else:
        print(f'\n输出目录不存在: {output_path}')
        
        # 检查其他可能的输出位置
        for pattern in ['*.mp4', '*/*.mp4']:
            found = list(Path(OUTPUT_DIR).rglob(pattern))
            if found:
                print(f'在根目录找到视频:')
                for v in sorted(found, key=lambda x: x.stat().st_mtime, reverse=True)[:3]:
                    print(f'  {v.relative_to(OUTPUT_DIR)} ({v.stat().st_size/1024/1024:.1f}MB)')

if __name__ == '__main__':
    main()
