# -*- coding: utf-8 -*-
"""模型 Provider 抽象层（P1-4）

为什么需要
----------
改造前业务代码直接调用 `comfyui_client.xxx`，等于把「本地 ComfyUI + 固定模型」
写死在流程里。开源项目（LumenX / waoowaoo）普遍用 Provider 网关按环节选最优模型，
本模块把这层抽象补上，做到**不改业务代码、只改配置即可换引擎**。

三类能力
--------
- ImageProvider：文生图 / 图生图（角色基础图、多视角、分镜图）
- VideoProvider：图/文生视频（H3 序列生成）
- TTSProvider：语音合成

切换方式
--------
环境变量（默认全部本地）：
  MJSCXT_PROVIDER_IMAGE = comfyui-local | wan-cloud | ...
  MJSCXT_PROVIDER_VIDEO = comfyui-local | wan-cloud | kling-cloud | vidu-cloud | veo-cloud
  MJSCXT_PROVIDER_TTS   = qwen-tts-local | ...

未实现的云端 Provider 会注册但调用时给出明确提示（而非静默失败），
这样接入新引擎只需实现一个类，不需要改动 app.py。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Provider 调用失败（面向用户的友好错误）"""


class BaseProvider:
    """Provider 基类：定义统一契约与自描述元信息"""

    kind = "base"          # image / video / tts
    name = "base"
    label = "基础 Provider"
    local = True
    description = ""

    def info(self, available: bool = None) -> dict:
        """自描述信息（供前端展示可选项）

        available 显式传入时不重复探测 —— 可用性探测可能真连网络（如 ComfyUI / TTS），
        catalog() 会统一走带 TTL 的缓存后把结果传进来，避免列表接口被探测拖慢。
        """
        return {"kind": self.kind, "name": self.name, "label": self.label,
                "local": self.local, "description": self.description,
                "available": self.available() if available is None else bool(available),
                "unavailable_reason": "" if (available is None or available)
                                      else self.unavailable_reason()}

    def available(self) -> bool:
        """是否可用（依赖是否就绪）"""
        return True

    def unavailable_reason(self) -> str:
        return ""


class ImageProvider(BaseProvider):
    """图片生成能力"""

    kind = "image"

    def generate(self, prompt: str, out_path: str, reference_images: List[str] = None,
                 params: Dict[str, Any] = None, workflow: str = "") -> dict:
        """生成单张图并落盘到 out_path

        返回 {"ok": bool, "path": str, "provider": str, "error": str}
        """
        raise NotImplementedError

    def generate_multiview(self, base_image: str, views: List[dict], out_dir: str,
                           params: Dict[str, Any] = None) -> dict:
        """基于基础图生成多视角图；views 为 [{"key","label","azimuth",...}]"""
        raise NotImplementedError


class VideoProvider(BaseProvider):
    """视频生成能力"""

    kind = "video"

    def generate_sequence(self, segments: List[dict], filename_prefix: str,
                          seed: Optional[int] = None,
                          timeout_per_segment: int = 900) -> dict:
        """按段生成视频并（如支持）无缝拼接

        segments: [{"prompt", "duration", "reference_images", "name"}]
        返回 {"files": [路径], "prompt_id": str, "segments": [...], "provider": str}
        """
        raise NotImplementedError

    def generate_keyframe(self, start_frame: str, end_frame: str, prompt: str,
                          out_path: str, duration: float = 5.0,
                          seed: Optional[int] = None) -> dict:
        """关键帧驱动生成（首尾帧插值）。不支持的 Provider 应抛 ProviderError。"""
        raise ProviderError(f"{self.label} 暂不支持关键帧驱动模式")


class TTSProvider(BaseProvider):
    """语音合成能力"""

    kind = "tts"

    def synthesize(self, lines: List[dict], out_dir: str,
                   params: Dict[str, Any] = None) -> dict:
        """批量合成台词音频

        lines: [{"line_id", "speaker", "text", "out_path", ...}]
        """
        raise NotImplementedError
