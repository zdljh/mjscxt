"""临时验证脚本：step_upscale 的 fail-open 行为与幂等性

重点验证「默认开启」不会把已跑通的成片拖成失败：
  1) 无成片            -> skipped
  2) 环境不可用        -> skipped（绝不 failed）
  3) 执行抛异常        -> skipped（绝不 failed）
  4) 正常产出          -> done 且归档到确定性路径，且 attach_audio=True
  5) 幂等重跑          -> skipped
  6) 交付物优先级      -> 超分产物优先于混音/成片
用法：python _verify_upscale_step.py
"""
import os
import shutil
import sys
import types

sys.path.insert(0, os.path.abspath("app"))

PROJECT = "_pyverify_tmp"
OUT = os.path.abspath("output")

# ---- 伪造宿主模块 app（pipeline 通过 _A() 延迟取用）----
fake = types.ModuleType("app")
fake.UPSCALE_DIR = os.path.join(OUT, "upscale")
fake.FINAL_DIR = os.path.join(OUT, "final")
fake.DUB_DIR = os.path.join(OUT, "dub")
fake.mix_out_dir = lambda p: os.path.join(OUT, "dub_mix", p)
fake.upscale_env_check = lambda: {
    "available": False, "comfy_online": False, "model_ready": False,
    "te_ready": False, "reasons": ["ComfyUI 未在线（模拟）"],
}
sys.modules["app"] = fake

import pipeline  # noqa: E402
import upscale_client  # noqa: E402

fails = []


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + str(extra)) if extra else ''}")
    if not cond:
        fails.append(label)


ctx = {
    "config": pipeline.normalize_config({}),
    "project_name": PROJECT,
    "episode_no": 1,
    "progress": lambda m, p=None, phase=None: None,
}

try:
    # ---------- 1) 无成片 ----------
    out = pipeline.step_upscale(ctx)
    check("1) 无成片 -> ok 且 skipped", out.get("ok") is True and out.get("skipped") is True,
          out.get("detail", {}).get("note"))

    # 造一个「混音成片」
    mix_p = pipeline.mix_output_path(ctx)
    os.makedirs(os.path.dirname(mix_p), exist_ok=True)
    with open(mix_p, "wb") as f:
        f.write(b"\x00" * 4096)

    # ---------- 2) 环境不可用 ----------
    out = pipeline.step_upscale(ctx)
    check("2) 环境不可用 -> ok 且 skipped（不阻断出片）",
          out.get("ok") is True and out.get("skipped") is True,
          out.get("detail", {}).get("note"))
    check("2b) 未误产出超分文件", not os.path.exists(pipeline.upscale_path(ctx)))

    # ---------- 3) 环境可用但执行抛错 ----------
    fake.upscale_env_check = lambda: {
        "available": True, "comfy_online": True, "model_ready": True,
        "te_ready": True, "reasons": [],
    }

    class Boom:
        def upscale(self, *a, **k):
            raise RuntimeError("模拟 ComfyUI 502")

    orig = upscale_client.VideoUpscaler
    upscale_client.VideoUpscaler = Boom
    out = pipeline.step_upscale(ctx)
    check("3) 执行抛异常 -> ok 且 skipped（不阻断出片）",
          out.get("ok") is True and out.get("skipped") is True,
          out.get("detail", {}).get("note"))

    # ---------- 4) 正常产出 ----------
    captured = {}

    class OK:
        def upscale(self, input_video, **k):
            captured.update({"input_video": input_video, **k})
            tmp = os.path.join(os.path.dirname(mix_p), "_tmp_upscaled.mp4")
            with open(tmp, "wb") as f:
                f.write(b"\x11" * 8192)
            return {"output_path": tmp, "engine": "te-speed-flashvsr", "scale": 2,
                    "elapsed_sec": 1.5, "before": {"width": 480}, "after": {"width": 960}}

    upscale_client.VideoUpscaler = OK
    out = pipeline.step_upscale(ctx)
    check("4) 正常产出 -> ok 且未 skipped",
          out.get("ok") is True and not out.get("skipped"), out.get("artifact"))
    check("4b) 归档到确定性路径", os.path.isfile(pipeline.upscale_path(ctx)))
    check("4c) attach_audio=True 已显式传参（防丢音轨）",
          captured.get("attach_audio") is True, f"实际={captured.get('attach_audio')}")
    check("4d) 输入源为混音成片", captured.get("input_video") == mix_p)
    check("4e) 倍率取自配置", captured.get("scale") == 2)

    # ---------- 5) 幂等重跑 ----------
    out = pipeline.step_upscale(ctx)
    check("5) 幂等重跑 -> skipped 且命中 probe",
          out.get("ok") is True and out.get("skipped") is True
          and bool((out.get("detail") or {}).get("probe")),
          (out.get("detail") or {}).get("probe"))
    upscale_client.VideoUpscaler = orig

    # ---------- 6) 交付物优先级 ----------
    steps = {"mix": {"artifact": mix_p}, "final": {"artifact": mix_p},
             "upscale": {"artifact": pipeline.upscale_path(ctx)}}
    check("6) 交付物优先取超分产物",
          pipeline._deliverable_of(ctx, steps) == pipeline.upscale_path(ctx))
    steps2 = {"mix": {"artifact": mix_p}, "final": {"artifact": mix_p},
              "upscale": {"artifact": "", "skipped": True}}
    check("6b) 超分跳过时回落混音成品",
          pipeline._deliverable_of(ctx, steps2) == mix_p)

    # ---------- 7) 开关可关闭 ----------
    ctx_off = dict(ctx, config=pipeline.normalize_config({"enable_upscale": False}))
    check("7) enable_upscale=False 时步骤不启用",
          pipeline.step_enabled("upscale", ctx_off) is False)
    check("7b) 默认启用", pipeline.step_enabled("upscale", ctx) is True)

finally:
    upscale_client.VideoUpscaler = getattr(upscale_client, "VideoUpscaler", None)
    for d in (os.path.join(fake.UPSCALE_DIR, PROJECT),
              os.path.join(OUT, "dub_mix", PROJECT)):
        shutil.rmtree(d, ignore_errors=True)
    tmpf = os.path.join(OUT, "dub_mix", PROJECT, "_tmp_upscaled.mp4")
    if os.path.exists(tmpf):
        os.remove(tmpf)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
