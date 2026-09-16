# -*- coding: utf-8 -*-
"""专业导出系统 - 支持 FCPXML、EDL、JSON 等格式

对标 openframe 的导出能力，支持接入 Premiere/达芬奇等专业后期软件
"""
import json
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any


class ExportManager:
    """导出管理器"""

    def __init__(self, project_key: str, base_dir: str):
        self.project_key = project_key
        self.base_dir = Path(base_dir) / "exports" / project_key
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def export_fcpml(self, timeline: Dict[str, Any]) -> str:
        """导出 FCPXML 格式 (Final Cut Pro XML)

        timeline 结构:
        {
            "project_name": "项目名称",
            "duration": 120.0,  # 总时长
            "resolution": {"width": 1920, "height": 1080},
            "fps": 30,
            "sequences": [
                {
                    "name": "主序列",
                    "clips": [
                        {
                            "name": "镜头1",
                            "start": 0.0,
                            "end": 5.0,
                            "asset_path": "/path/to/video.mp4",
                            "video_track": 1,
                            "audio_track": 1
                        }
                    ]
                }
            ]
        }
        """
        root = ET.Element("fcpxml")
        root.set("version", "1.9")

        # Project
        # 注意：这里绝不能用一个写死的中文占位串（原先是 "项目"）。调用方漏传
        # project_name 时，导出的 FCPXML 会带着 "项目" 这个名字进入剪映/达芬奇，
        # 让人误以为导出成功；退回本项目 key 至少能定位到是哪个项目。
        proj_name = timeline.get("project_name") or self.project_key
        project = ET.SubElement(root, "project")
        project.set("name", str(proj_name))

        # Sequence
        sequence = ET.SubElement(project, "sequence")
        sequence.set("duration", str(timeline.get("duration", 0)))
        sequence.set("frameRate", str(timeline.get("fps", 30)))

        # Spacetime
        spacetime = ET.SubElement(sequence, "spacetime")
        ET.SubElement(spacetime, "duration").text = str(timeline.get("duration", 0))

        # Resources
        resources = ET.SubElement(sequence, "resources")
        res_format = ET.SubElement(resources, "format")
        res_format.set("id", "f1")
        res_format.set("name", str(proj_name))
        res_format.set("frameDuration", f"1/{timeline.get('fps', 30)}")
        res_format.set("width", str(timeline.get("resolution", {}).get("width", 1920)))
        res_format.set("height", str(timeline.get("resolution", {}).get("height", 1080)))

        # Clips
        clips = ET.SubElement(sequence, "clips")
        for clip_info in timeline.get("sequences", [{}])[0].get("clips", []):
            clip = ET.SubElement(clips, "clip")
            clip.set("name", clip_info.get("name", "镜头"))
            clip.set("start", str(clip_info.get("start", 0)))
            clip.set("duration", str(clip_info.get("end", 0) - clip_info.get("start", 0)))

            # Video track position
            vid_pos = ET.SubElement(clip, "videospacetimerange")
            ET.SubElement(vid_pos, "time").text = f"0:{int(clip_info.get('start', 0) * timeline.get('fps', 30))}:0"
            ET.SubElement(vid_pos, "duration").text = f"0:{int((clip_info.get('end', 0) - clip_info.get('start', 0)) * timeline.get('fps', 30))}:0"

            # Asset reference
            asset = ET.SubElement(clip, "asset-ref")
            asset.set("ref", "r1")

        # Assets
        assets = ET.SubElement(sequence, "assets")
        for i, clip_info in enumerate(timeline.get("sequences", [{}])[0].get("clips", [])):
            asset = ET.SubElement(assets, "asset")
            asset.set("id", f"r{i+1}")
            asset.set("name", clip_info.get("name", f"镜头{i+1}"))

            format_ref = ET.SubElement(asset, "format-ref")
            format_ref.set("ref", "f1")

            # Source clip
            source_clip = ET.SubElement(asset, "originalsequence")
            reel = ET.SubElement(source_clip, "reel")
            ET.SubElement(reel, "name").text = "Reel 1"
            ET.SubElement(reel, "startIndex").text = "0"

            # Timeline range
            start = clip_info.get("start", 0) * timeline.get("fps", 30)
            duration = (clip_info.get("end", 0) - clip_info.get("start", 0)) * timeline.get("fps", 30)
            ET.SubElement(asset, "time").text = f"0:0:{int(start)}:0"
            ET.SubElement(asset, "duration").text = f"0:0:{int(duration)}:0"

        # Generate XML string
        xml_str = minidom.parseString(ET.tostring(root, encoding='unicode')).toprettyxml(indent="  ")

        # Save to file
        output_path = self.base_dir / f"{self.project_key}_fcpml.xml"
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(xml_str)

        return str(output_path)

    def export_edl(self, timeline: Dict[str, Any]) -> str:
        """导出 EDL (Edit Decision List) 格式

        EDL 格式:
        001  CUT    00:00:00 00:00:05 00:00:00 00:00:05 * CLIP NAME
        """
        lines = []
        lines.append("FILENAME: {}\n".format(self.project_key))
        lines.append("FROM CLIP NAME:\n")

        clip_num = 1
        for seq in timeline.get("sequences", []):
            for clip in seq.get("clips", []):
                start = clip.get("start", 0)
                end = clip.get("end", 0)
                duration = end - start

                # 转换为时间码
                start_tc = self._seconds_to_timecode(start, timeline.get("fps", 30))
                end_tc = self._seconds_to_timecode(end, timeline.get("fps", 30))
                dur_tc = self._seconds_to_timecode(duration, timeline.get("fps", 30))

                line = "{:03d}  CUT    {} {} {} {} * {}\n".format(
                    clip_num, start_tc, end_tc, start_tc, dur_tc,
                    clip.get("name", "CLIP").upper()
                )
                lines.append(line)
                clip_num += 1

        output_path = self.base_dir / f"{self.project_key}_edl.edl"
        with open(output_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)

        return str(output_path)

    def export_json(self, timeline: Dict[str, Any]) -> str:
        """导出 JSON 格式 (完整元数据)"""
        output_path = self.base_dir / f"{self.project_key}_timeline.json"
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(timeline, f, ensure_ascii=False, indent=2)
        return str(output_path)

    def export_all(self, timeline: Dict[str, Any]) -> Dict[str, str]:
        """导出所有格式"""
        exports = {}
        exports["fcpml"] = self.export_fcpml(timeline)
        exports["edl"] = self.export_edl(timeline)
        exports["json"] = self.export_json(timeline)
        return exports

    def _seconds_to_timecode(self, seconds: float, fps: int = 30) -> str:
        """秒数转时间码 HH:MM:SS:FF"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        frames = int((seconds % 1) * fps)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"
