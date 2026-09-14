# -*- coding: utf-8 -*-
"""本地 Provider 实现：包装既有 ComfyUI 链路

设计原则：**零行为变更**。这里只做参数适配与结果规整，
真正的生成逻辑仍由 comfyui_client / tts_client 承担，保证引入抽象层不改变现有产出。
"""
from __future__ import annotations

import logging
import os

from providers.base import ImageProvider, ProviderError, TTSProvider, VideoProvider

logger = logging.getLogger(__name__)


def _comfy():
    """延迟导入 comfyui_client，避免 provider 包导入时触发重量级依赖"""
    import comfyui_client
    return comfyui_client


class ComfyUIImageProvider(ImageProvider):
    """本地 ComfyUI 图片生成（Qwen-Image 2512 / Qwen-Edit 2511）

    按 ComfyUI 客户端真实接口分派：
      - asset_type=character/item/scene → generate_*_base（Qwen 2512 文生图）
      - asset_type=storyboard           → generate_storyboard（Qwen Edit 2511 + 参考图）
    """

    name = "comfyui-local"
    label = "本地 ComfyUI（Qwen-Image）"
    local = True
    description = "本地 Qwen 2512 文生图 + Qwen Edit 2511 多视角/分镜编辑，零 API 成本"

    def available(self) -> bool:
        try:
            st = _comfy().ComfyUIClient().get_status()
            return st.get("status") == "online"
        except Exception:  # noqa: BLE001
            return False

    def unavailable_reason(self) -> str:
        return "ComfyUI 未运行或不可达（请先启动 ComfyUI）"

    def generate(self, prompt: str, out_path: str, reference_images=None,
                 params=None, workflow: str = "") -> dict:
        """通用生成入口：按 params['asset_type'] 分派到具体 ComfyUI 方法"""
        params = params or {}
        client = _comfy().ComfyUIClient()
        asset_type = (params.get("asset_type") or "character").strip().lower()
        seed = params.get("seed")
        timeout = params.get("timeout", 900)
        try:
            if asset_type == "storyboard":
                result = client.generate_storyboard(
                    prompt_zh=prompt,
                    ref_images=list(reference_images or []),
                    filename_prefix=params.get("filename_prefix") or "comic_drama_sb/shot",
                    seed=seed, timeout=timeout)
                files = (result or {}).get("files") or []
                prompt_id = (result or {}).get("prompt_id")
            else:
                fn = {
                    "character": client.generate_character_base,
                    "item": client.generate_item_base,
                    "scene": client.generate_scene_base,
                }.get(asset_type)
                if fn is None:
                    return {"ok": False, "provider": self.name,
                            "error": f"不支持的 asset_type：{asset_type}"
                                     f"（可选 character/item/scene/storyboard）"}
                files = fn(prompt, seed=seed) or []
                prompt_id = None
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.name, "error": str(e)}

        if not files:
            return {"ok": False, "provider": self.name, "error": "ComfyUI 未返回图片文件"}
        if out_path:
            try:
                import shutil
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                shutil.copy2(files[0], out_path)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "provider": self.name, "error": f"落盘失败：{e}"}
        return {"ok": True, "provider": self.name,
                "path": out_path or files[0], "raw": files[0],
                "prompt_id": prompt_id, "asset_type": asset_type}

    def generate_multiview(self, base_image: str, views, out_dir: str, params=None) -> dict:
        """多视角生成（委托 ComfyUI 的 generate_multiview）

        注意：views 的具体视角定义由 ComfyUI 侧 MULTIVIEW_CONFIG 决定，
        这里只负责调用与结果规整，保证与既有产物路径一致。
        """
        params = params or {}
        client = _comfy().ComfyUIClient()
        asset_type = (params.get("asset_type") or "character").strip().lower()
        asset_name = params.get("asset_name") or "asset"
        try:
            result = client.generate_multiview(
                base_image_path=base_image, asset_type=asset_type,
                asset_name=asset_name, base_prompt_zh=params.get("base_prompt_zh") or "",
                seed=params.get("seed"))
            return {"ok": bool(result), "provider": self.name, "views": result or {}}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.name, "error": str(e)}


class ComfyUIVideoProvider(VideoProvider):
    """本地 ComfyUI 视频生成（MiniMax H3 Ref2VA + Turbo LoRA + 无缝拼接）"""

    name = "comfyui-local"
    label = "本地 ComfyUI（MiniMax H3）"
    local = True
    description = "H3 Ref2VA 多段无缝拼接 + 原生音频剥离，支持逐镜/整集与关键帧驱动"

    def available(self) -> bool:
        try:
            return _comfy().ComfyUIClient().get_status().get("status") == "online"
        except Exception:  # noqa: BLE001
            return False

    def unavailable_reason(self) -> str:
        return "ComfyUI 未运行或不可达（请先启动 ComfyUI）"

    def generate_sequence(self, segments, filename_prefix, seed=None,
                          timeout_per_segment: int = 900) -> dict:
        result = _comfy().ComfyUIClient().generate_h3_sequence(
            segments=segments, filename_prefix=filename_prefix, seed=seed,
            timeout_per_segment=timeout_per_segment)
        if isinstance(result, dict):
            result.setdefault("provider", self.name)
            return result
        return {"files": [], "provider": self.name, "raw": result}

    def generate_keyframe(self, start_frame: str, end_frame: str, prompt: str,
                          out_path: str, duration: float = 5.0, seed=None) -> dict:
        """关键帧驱动：单段 H3 同时注入首帧与尾帧参考图

        H3 工作流的两个参考图槽位（qwen_reference_1 / qwen_reference_2）原本是
        「分镜图 + 主角锚点图」，这里改为「首帧 + 尾帧」，
        让模型在两端之间插值生成运动，构图可控性显著优于纯参考图模式。
        """
        client = _comfy().ComfyUIClient()
        seg = {
            "prompt": prompt,
            "duration": float(duration or 5.0),
            "reference_images": [start_frame, end_frame],
            "name": "keyframe",
        }
        result = client.generate_h3_sequence(
            segments=[seg], filename_prefix=filename_prefix_from(out_path), seed=seed,
            timeout_per_segment=900)
        files = (result or {}).get("files") or []
        if not files:
            return {"ok": False, "provider": self.name,
                    "error": "ComfyUI 未返回视频文件（关键帧驱动需 H3 节点支持双参考图）"}
        try:
            import shutil
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            shutil.copy2(files[0], out_path)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.name, "error": f"落盘失败：{e}"}
        return {"ok": True, "provider": self.name, "path": out_path,
                "prompt_id": (result or {}).get("prompt_id")}


def filename_prefix_from(out_path: str) -> str:
    """把输出路径转为 ComfyUI 的 filename_prefix（去扩展名、统一正斜杠）"""
    p = os.path.splitext(os.path.abspath(out_path or ""))[0]
    return p.replace("\\", "/")


class QwenTTSLocalProvider(TTSProvider):
    """本地 QwenTTS 配音"""

    name = "qwen-tts-local"
    label = "本地 QwenTTS"
    local = True
    description = "本地 QwenTTS 合成，音色按角色映射，支持逐句与批量"

    def available(self) -> bool:
        try:
            import tts_client
            return bool((tts_client.check_environment() or {}).get("available"))
        except Exception:  # noqa: BLE001
            return False

    def unavailable_reason(self) -> str:
        try:
            import tts_client
            reasons = (tts_client.check_environment() or {}).get("reasons") or []
            if reasons:
                return "；".join(str(r) for r in reasons[:3])
        except Exception:  # noqa: BLE001
            pass
        return "QwenTTS 环境未就绪（需 ComfyUI 已加载 Qwen-TTS 节点与模型权重）"

    def synthesize(self, lines, out_dir, params=None) -> dict:
        """批量合成（委托 tts_client.QwenTTSClient.synthesize_lines）

        lines 每项需含 text / voice，可选 out_path / out_name / line_id / shot_id。
        """
        import tts_client
        params = params or {}
        try:
            client = tts_client.QwenTTSClient(
                out_root=params.get("out_root"), params=params.get("tts_params"))
            results = client.synthesize_lines(lines, out_dir,
                                              timeout=params.get("timeout"))
            return {"ok": True, "provider": self.name, "results": results,
                    "count": len(results or [])}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.name, "error": str(e)}
