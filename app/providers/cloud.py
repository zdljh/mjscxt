# -*- coding: utf-8 -*-
"""云端 Provider 骨架（P1-4）

这些 Provider 目前只登记「能力声明 + 调用契约」，未接入真实云 API。
这样做的价值：
1. 需要切换引擎时，只需在此补实现，**不需要改动 app.py 与业务流程**；
2. 前端能提前看到可选引擎及其可用状态；
3. 避免「预留但没写」导致后来者无从下手。

接入任一云端引擎的步骤（以视频为例）：
  ① 在 config.py 增加该引擎的 base_url / api_key 环境变量（密钥走 secret_store）；
  ② 实现本文件对应类的 generate_sequence；
  ③ 把返回结果规整成 {"files": [本地已下载的 mp4 路径], "segments": [...]}，
     因为下游（质检 / 混音 / 合片 / 超分）都只认本地文件路径；
  ④ 设置 MJSCXT_PROVIDER_VIDEO=<该 provider 的 name> 即生效。
"""
from __future__ import annotations

import logging

from providers.base import ImageProvider, ProviderError, TTSProvider, VideoProvider

logger = logging.getLogger(__name__)

# 统一的「未接入」提示，避免调用方拿到空结果却不知原因
_NOT_IMPLEMENTED_HINT = (
    "该云端 Provider 尚未接入。请在 providers/cloud.py 中实现对应方法，"
    "并配置其 base_url / api_key（密钥走 secret_store 加密库）。"
)


class CloudProviderMixin:
    local = False

    def available(self) -> bool:
        """云端 Provider 默认视为不可用，直到其实现被补齐"""
        return False

    def unavailable_reason(self) -> str:
        return _NOT_IMPLEMENTED_HINT

    def _todo(self, capability: str):
        raise ProviderError(f"{self.label} 的{capability}能力尚未接入：{_NOT_IMPLEMENTED_HINT}")


class WanCloudVideoProvider(CloudProviderMixin, VideoProvider):
    """阿里通义万相（Wan）云端视频生成"""

    name = "wan-cloud"
    label = "通义万相（云端）"
    description = "云端 Wan 视频生成，适合关键镜次高质量重跑"

    def generate_sequence(self, segments, filename_prefix, seed=None,
                          timeout_per_segment: int = 900) -> dict:
        self._todo("视频生成")


class KlingCloudVideoProvider(CloudProviderMixin, VideoProvider):
    """快手可灵（Kling）云端视频生成"""

    name = "kling-cloud"
    label = "可灵 Kling（云端）"
    description = "云端 Kling 视频生成，动作自然度较好"

    def generate_sequence(self, segments, filename_prefix, seed=None,
                          timeout_per_segment: int = 900) -> dict:
        self._todo("视频生成")


class ViduCloudVideoProvider(CloudProviderMixin, VideoProvider):
    """生数 Vidu 云端视频生成"""

    name = "vidu-cloud"
    label = "Vidu（云端）"
    description = "云端 Vidu 视频生成"

    def generate_sequence(self, segments, filename_prefix, seed=None,
                          timeout_per_segment: int = 900) -> dict:
        self._todo("视频生成")


class VeoCloudVideoProvider(CloudProviderMixin, VideoProvider):
    """Google Veo 云端视频生成（支持首尾帧插值，适合关键帧驱动）"""

    name = "veo-cloud"
    label = "Google Veo（云端）"
    description = "云端 Veo，原生支持首帧+尾帧插值，适合关键帧驱动模式"

    def generate_sequence(self, segments, filename_prefix, seed=None,
                          timeout_per_segment: int = 900) -> dict:
        self._todo("视频生成")

    def generate_keyframe(self, start_frame: str, end_frame: str, prompt: str,
                          out_path: str, duration: float = 5.0, seed=None) -> dict:
        self._todo("关键帧驱动生成")


class DoubaoCloudImageProvider(CloudProviderMixin, ImageProvider):
    """火山豆包云端图片生成"""

    name = "doubao-cloud"
    label = "火山豆包（云端）"
    description = "云端豆包图片生成"

    def generate(self, prompt: str, out_path: str, reference_images=None,
                 params=None, workflow: str = "") -> dict:
        self._todo("图片生成")

    def generate_multiview(self, base_image: str, views, out_dir: str, params=None) -> dict:
        self._todo("多视角生成")


class CosyVoiceCloudTTSProvider(CloudProviderMixin, TTSProvider):
    """阿里 CosyVoice 云端配音"""

    name = "cosyvoice-cloud"
    label = "CosyVoice（云端）"
    description = "云端 CosyVoice 配音，音色更自然"

    def synthesize(self, lines, out_dir, params=None) -> dict:
        self._todo("语音合成")
