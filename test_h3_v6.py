"""
H3视频生成测试 - 使用示例工作流模板
"""
import json
import urllib.request
import urllib.error
import time
from pathlib import Path

COMFYUI_URL = 'http://127.0.0.1:8188'
OUTPUT_DIR = r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output'
TEST_OUTPUT = 'h3_test_simple'

# 测试提示词
TEST_PROMPT = '云雾缭绕的仙山秘境，苍松翠柏间有一座破败的古道观。镜头缓慢推进，一个青衣少年正在擦拭一柄长剑。国漫古风，电影级画面，cinematic push in, Chinese fantasy anime, 8k, highly detailed'

def main():
    # 加载示例工作流
    wf_path = r'D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\custom_nodes\Herrgotts-H3-Infinite-Continuation-Suite-main\examples\Herrgotts_H3_Infinite_v1.4_01_Start.json'
    with open(wf_path, 'r', encoding='utf-8') as f:
        wf = json.load(f)
    
    print(f'原始工作流: {len(wf["nodes"])} 节点, {len(wf.get("links", []))} 链接')
    
    # 转换为API格式
    nodes = {}
    for n in wf['nodes']:
        nid = str(n['id'])
        node_type = n['type']
        wv = n.get('widgets_values', [])
        
        node = {'class_type': node_type, 'inputs': {}}
        
        # 根据节点类型处理widgets_values
        if node_type == 'UNETLoader':
            node['inputs']['unet_name'] = wv[0] if len(wv) > 0 else ''
            node['inputs']['weight_dtype'] = wv[1] if len(wv) > 1 else 'default'
        elif node_type == 'CLIPLoader':
            node['inputs']['clip_name'] = wv[0] if len(wv) > 0 else ''
            node['inputs']['type'] = wv[1] if len(wv) > 1 else 'minimax'
            node['inputs']['device'] = wv[2] if len(wv) > 2 else 'default'
        elif node_type == 'VAELoader':
            node['inputs']['vae_name'] = wv[0] if len(wv) > 0 else ''
        elif node_type == 'LoadImage':
            node['inputs']['image'] = wv[0] if len(wv) > 0 else ''
            node['inputs']['upload'] = wv[1] if len(wv) > 1 else 'image'
        elif node_type == 'CLIPTextEncode':
            # 找到提示词节点并更新
            title = n.get('title', '')
            if '提示词' in title or 'prompt' in title.lower():
                node['inputs']['text'] = TEST_PROMPT
                node['inputs']['clip'] = ['2', 0]  # 连接到CLIP
            else:
                node['inputs']['text'] = wv[0] if wv else ''
                node['inputs']['clip'] = ['2', 0]
        elif node_type == 'MiniMaxH3ReferenceToVideo':
            # 直接设置参数
            node['inputs']['clip'] = ['2', 0]
            node['inputs']['vae'] = ['3', 0]  # 使用第一个VAE
            node['inputs']['audio_vae'] = ['4', 0]  # 使用第二个VAE
            node['inputs']['prompt'] = TEST_PROMPT
            node['inputs']['width'] = 704  # 降低分辨率以节省显存
            node['inputs']['height'] = 576
            node['inputs']['length'] = 124  # 5秒 at 24fps
            node['inputs']['ref_image_size'] = 'match'
            node['inputs']['seed'] = 42
            node['inputs']['denoise'] = 0.88
            node['inputs']['guidance_scale'] = 5.0
            node['inputs']['turbo_lora_scale'] = 1.0
            node['inputs']['ref_image_strength'] = 0.9
            node['inputs']['model'] = ['1', 0]
            node['inputs']['ref_images'] = []
            node['inputs']['conditioning'] = ['6', 0]  # 正向conditioning
        elif node_type == 'H3ContinuousStartV14':
            node['inputs']['model'] = ['1', 0]
            node['inputs']['clip'] = ['2', 0]
            node['inputs']['vae'] = ['3', 0]
            node['inputs']['audio_vae'] = ['4', 0]
            node['inputs']['positive'] = ['6', 0]
            node['inputs']['negative'] = ['7', 0]
            node['inputs']['latent'] = ['8', 0]
            node['inputs']['width'] = 704
            node['inputs']['height'] = 576
            node['inputs']['length'] = 124
            node['inputs']['seed'] = 42
            node['inputs']['steps'] = 20
            node['inputs']['cfg'] = 5.0
            node['inputs']['sampler_name'] = 'euler'
            node['inputs']['scheduler'] = 'normal'
            node['inputs']['denoise'] = 1.0
            node['inputs']['ref_image_size'] = 'match'
            node['inputs']['ref_image_strength'] = 0.9
        elif node_type == 'VAEDecode':
            node['inputs']['samples'] = ['9', 0]  # 连接到H3输出
            node['inputs']['vae'] = ['3', 0]
        elif node_type == 'CreateVideo':
            node['inputs']['images'] = ['10', 0]  # 连接到VAEDecode
            node['inputs']['fps'] = 24
            node['inputs']['dirname'] = TEST_OUTPUT
            node['inputs'] = {**node['inputs'], 'filename_prefix': 'test_sword'}
        elif node_type == 'SaveVideo':
            node['inputs']['video'] = ['9', 0]  # 连接到VAEDecode或CreateVideo
            node['inputs']['filename_prefix'] = TEST_OUTPUT
        else:
            # 其他节点尝试从widgets_values映射
            input_names = [inp['name'] for inp in n.get('inputs', []) 
                          if not inp.get('link') and inp.get('value') is not None]
            for i, name in enumerate(input_names):
                if i < len(wv):
                    node['inputs'][name] = wv[i]
        
        nodes[nid] = node
    
    print(f'转换后: {len(nodes)} 节点')
    
    # 转换links
    links = []
    for link in wf.get('links', []):
        link_id, from_node_id, from_idx, to_node_id, to_idx, type_str = link
        links.append([link_id, str(from_node_id), from_idx, str(to_node_id), to_idx, type_str])
    
    # 提交
    prompt_data = [0, '', nodes, {'create_time': int(time.time() * 1000)}, list(nodes.keys())]
    prompt_with_links = [0, '', nodes, {'create_time': int(time.time() * 1000)}, list(nodes.keys()), links]
    
    print('提交工作流...')
    try:
        r = urllib.request.Request(
            f'{COMFYUI_URL}/prompt',
            data=json.dumps(prompt_with_links).encode(),
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
