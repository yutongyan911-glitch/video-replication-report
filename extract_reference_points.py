#!/usr/bin/env python3
"""
参考点提取脚本
将复刻视频文件夹中的素材与 prompt.xlsx 中的中文prompt，传入 Gemini 多模态模型，
逐 case 提取视频生成参考点，并输出结构化 JSON 文件到 reference_points_output/ 目录。

用法：
  python3 extract_reference_points.py               # 处理全部 case
  python3 extract_reference_points.py --index 效果广告爆款_001   # 只处理指定 case
  python3 extract_reference_points.py --limit 5     # 先测试前5个
"""

import os
import re
import json
import time
import argparse
import openpyxl
from pathlib import Path
from google import genai
from google.genai import types

# ===== 全局配置 =====
CREDENTIALS_PATH = "/Users/yanyutong/Downloads/llm-64897-gemini-79099acd0672.json"
BASE_DIR = Path("/Users/yanyutong/.gemini/antigravity/scratch/复刻视频文件")
OUTPUT_DIR = BASE_DIR / "reference_points_output"
MODEL = "gemini-2.5-pro"

# 支持的图片 MIME 类型
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# ===== 系统提示词（修改版） =====
SYSTEM_PROMPT = """# 角色
您是顶级的多模态意图解析与视频生成参考点提取专家。您精通视频生成与音频生成的底层解耦逻辑，擅长在用户非结构化、模糊甚至包含隐喻的自然语言指令中，精准捕捉其对多媒体素材（图/文/音/视频）的真实应用意图。

# 任务目标
接收多个素材（视频、图片、主体object图集）与用户的原始生成指令（中文prompt），通过交叉比对，精准提炼出用户要求模型在生成阶段**必须遵循的"参考点 (Reference Points)"**。

**素材对应关系（以文字说明为准）：**
- `the video` → 输入的视频素材
- `Image N` → 编号为N的单张图片素材
- `object_N` → 编号为N的主体图集（多张图片，展示同一主体的不同角度）

**【最高指令】：所有参考点的提炼，必须严格按照"视觉层 (Visual Layer)"和"音频层 (Audio Layer)"进行物理级解耦。**

# 核心分析逻辑与解耦维度

## 一、视觉层 (Visual Layer) 提取维度
1. **entity_subject**：保留的具体人物、动物或物体的外观特征（长相、衣着、材质等）
2. **visual_style**：继承的画风、色调、滤镜或特定渲染质感
3. **environment**：保留的特定物理背景、构图位置或核心道具
4. **subject_actions**：主体的动作（如，拿起xxx，喝水，踢腿等主体的动作）
5. **camera_movement**：镜头的运动，即运镜（如环绕拍摄、变速、画面的快速播放等）
6. **composition**：特殊画面构图（如：第一人称视角、强对称构图、特写镜头等）
7. **editing_techniques**：特殊剪辑方式（如：快切、卡点转场、淡入淡出、定格动画等）
8. **special_effects**：视觉特效（如：利用视觉特效手段实现人物快速变装、炫酷光影粒子等）
9. **presentation_format**：体裁（如：先展示商品的细节，再展示商品的使用方法）

## 二、音频层 (Audio Layer) 提取维度（严格对齐以下6项）
1. **dialogue_content**：用户要求完全复用或接续素材中的具体说话文本
2. **dialogue_style**：复用说话的语气、情感、播音腔、方言或特定人物音色
3. **music_exact**：强制要求使用与原素材完全同一首 BGM/歌曲
4. **music_genre**：要求生成与原素材风格相似但并非同一首的新音乐
5. **sound_effects**：要求保留素材中特定的动作触发音效
6. **ambient_sound**：要求保留素材中的背景底噪

# 解析规则与约束
1. **关于弃用元素的极度严格判定**：只有当用户在 prompt 中“显式、直接地要求修改或替换原素材某具体元素”时（例如明确要求换衣服，那么原素材的衣服才算被弃用），该具体被替换掉的元素才能放入 `discard_elements`。对于未被明确要求替换的部分（例如没有说要换人），它们属于隐式参考点（属于 P1），绝对不能随意判定为被弃用并放入 `discard_elements`！切忌过度主观推断。
2. **重点描述参考点内容**，不仅要说明“是什么”，还要详尽描述其特征。内容的颗粒度要丰富。例如：不要只写“戴墨镜的男子”，需写成“戴着黑色反光墨镜、身穿挺括黑色西装、留着短发的白种男子”。
3. **针对图片素材的特殊约束**：图片（Image）或图集（object）素材绝对不允许包含任何音频层信息，其 `audio_layer` 内部必须为空。
4. **动态字段与优先级评定原则（极端重要）**：
   - 不再要求保留所有字段，**没有被参考的维度禁止进入参考点，直接省略（不在 JSON 中输出该 key）**。
   - **优先级判定（P0 / P1）**：
     - **P0**：若该维度参考点为 user prompt 中“显式的明确表达”，或“属于构成显式明确表达的维度”，或者“是该参考素材中核心的元素参考点”，则判定为 P0。
     - **P1**：若该维度参考点“属于隐式表达”且“不属于该参考素材中核心的元素参考点”，则判定为 P1。
   - 在 `visual_layer` 和 `audio_layer` 内部的字段顺序，**必须按优先级（P0 在前，P1 在后）进行同层排序**！
   - **格式要求**：每一条具体的描述文字，开头必须带上优先级标签，如 `"[P0] 详尽的描述..."` 或 `"[P1] 详尽的描述..."`。
5. 格式约束：严禁输出任何解释性废话，必须严格按照下方结构输出 JSON。

# 输出格式 (JSON Format)

```json
{
  "intent_summary": "一句话概括用户的核心生成意图",
  "inferred_task_type": "任务类型（基于整体指令行为分析而非关键词）：若在the video基础上进行增/删/改/替换（如换人、换场景）或保持不变，判定为Edit；若将不同素材的多个维度进行拼接组合生成新视频，判定为Reference；若明确为延长the video，判定为Extend；否则为Hybrid",
  "reference_points": {
    "assets_mapping": [
      {
        "asset_id": "素材标识符（如：the video、Image 1、object_1）",
        "visual_layer": {
          "visual_style": ["[P0] 详尽的描述1", "[P1] 详尽的描述2"],
          "subject_actions": ["[P0] 详尽的描述"],
          "...": ["...以上仅为示例。请使用标准维度英文名作为 key，只保留原素材有的维度，按P0到P1顺序输出，并带上优先级前缀..."]
        },
        "audio_layer": {
          "music_exact": ["[P0] 详尽的描述"],
          "...": ["...以上仅为示例。请使用标准维度英文名作为 key，图片类均为空..."]
        }
      }
    ]
  },
  "discard_elements": {
    "visual_layer_discards": ["被明确废弃/替换的视觉元素"],
    "audio_layer_discards": ["被明确废弃/替换的音频元素"]
  },
  "generation_instructions": "融合所有参考点的精准生成指令（分离视听逻辑，脱水版执行prompt）",
  "traffic_code_analysis": {
    "visual_layer_hooks": ["仅针对the video视频本身画面内容（不考虑参考行为）提炼视觉层面的核心流量密码"],
    "audio_layer_hooks": ["仅针对the video视频本身音频内容（不考虑参考行为）提炼音频层面的核心流量密码"]
  }
}
```"""


# ===== 初始化客户端 =====
def init_client():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = CREDENTIALS_PATH
    client = genai.Client(vertexai=True, location="global", project="llm-64897-gemini")
    return client


# ===== 加载 case 列表 =====
def load_cases():
    """从 prompt.xlsx 加载所有 case"""
    wb = openpyxl.load_workbook(BASE_DIR / "prompt.xlsx")
    ws = wb.active
    cases = []
    for row in ws.iter_rows(values_only=True, min_row=2):
        if row[0]:
            cases.append({
                "index": str(row[0]).strip(),
                "zh_prompt": str(row[1]).strip() if row[1] else "",
                "en_prompt": str(row[2]).strip() if row[2] else "",
                "running_params": row[3],
            })
    return cases


# ===== 收集素材 =====
def get_case_assets(case_index: str) -> dict | None:
    """收集某个 case 文件夹中的所有素材"""
    folder = BASE_DIR / case_index
    if not folder.exists():
        return None

    assets = {
        "video": None,       # Path
        "images": {},        # {"Image 1": Path, "Image 2": Path, ...}
        "objects": {},       # {"object_1": [Path, ...], "object_2": [...]}
    }

    for item in sorted(folder.iterdir()):
        if item.is_file():
            suffix = item.suffix.lower()
            stem = item.stem  # e.g. "Image 1", "Image 2"
            if suffix in [".mp4", ".mov", ".avi", ".webm"]:
                assets["video"] = item
            elif stem.lower().startswith("image") and suffix in IMAGE_MIME:
                assets["images"][stem] = item
        elif item.is_dir() and item.name.lower().startswith("object_"):
            obj_images = sorted(
                [f for f in item.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_MIME],
                key=lambda x: x.name,
            )
            if obj_images:
                assets["objects"][item.name] = obj_images

    return assets


# ===== 读取视频为 inline bytes =====
def read_video_inline(video_path: Path) -> types.Part:
    """将视频以 inline bytes 形式传入（Vertex AI 不支持 Files API upload）"""
    size_mb = video_path.stat().st_size / 1024 / 1024
    print(f"    读取视频: {video_path.name} ({size_mb:.1f} MB)")
    if size_mb > 20:
        print(f"    ⚠️  视频较大（{size_mb:.1f} MB），建议 < 20 MB 以避免超时")
    video_bytes = video_path.read_bytes()
    # 根据扩展名判断 MIME
    suffix = video_path.suffix.lower()
    mime_map = {".mp4": "video/mp4", ".mov": "video/quicktime",
                ".avi": "video/avi", ".webm": "video/webm"}
    mime = mime_map.get(suffix, "video/mp4")
    return types.Part.from_bytes(data=video_bytes, mime_type=mime)


# ===== 构建多模态输入 =====
def build_contents(client: genai.Client, zh_prompt: str, assets: dict) -> list:
    """构建 Gemini 的多模态 parts 列表，并附带素材映射说明"""
    parts = []
    mapping_lines = []  # 素材与 prompt 占位符的对应说明

    # 1. 视频
    if assets["video"]:
        parts.append(read_video_inline(assets["video"]))
        mapping_lines.append("【上方视频】= the video（即prompt中的 'the video'）")

    # 2. 独立图片（Image 1, Image 2, ...）
    for img_name in sorted(assets["images"].keys()):
        img_path = assets["images"][img_name]
        mime = IMAGE_MIME.get(img_path.suffix.lower(), "image/png")
        parts.append(
            types.Part.from_bytes(data=img_path.read_bytes(), mime_type=mime)
        )
        mapping_lines.append(f"【上方图片】= {img_name}（即prompt中的 '{img_name}'）")

    # 3. Object 图集（object_1, object_2, ...）
    for obj_name in sorted(assets["objects"].keys()):
        obj_images = assets["objects"][obj_name]
        for img_path in obj_images:
            mime = IMAGE_MIME.get(img_path.suffix.lower(), "image/png")
            parts.append(
                types.Part.from_bytes(data=img_path.read_bytes(), mime_type=mime)
            )
        mapping_lines.append(
            f"【上方连续 {len(obj_images)} 张图片】= {obj_name}"
            f"（即prompt中的 '{obj_name}'，为同一主体的多角度图集）"
        )

    # 4. 文字说明 + 任务指令
    asset_desc = "\n".join(mapping_lines) if mapping_lines else "（无额外素材）"
    text_part = f"""## 素材映射说明（请严格按此对应关系解析prompt）
{asset_desc}

## 用户的原始生成指令（中文prompt）
{zh_prompt}

## 任务
请根据以上素材和指令，提取参考点，严格以 JSON 格式输出，不要输出任何其他内容。"""

    parts.append(types.Part.from_text(text=text_part))
    return parts


# ===== 解析 JSON 输出 =====
def parse_json_response(text: str) -> dict:
    """从模型输出中提取 JSON，支持代码块包裹格式"""
    text = text.strip()
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试提取 ```json ... ``` 代码块
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # 退回原始文本
    return {"raw_response": text, "parse_error": "JSON 解析失败"}


# ===== 处理单个 case =====
def process_case(client: genai.Client, case: dict, force: bool = False) -> bool:
    index = case["index"]
    output_file = OUTPUT_DIR / f"{index}.json"

    print(f"\n[{index}]")

    # 跳过已处理
    if output_file.exists() and not force:
        print(f"  已存在，跳过（使用 --force 可重新处理）")
        return True

    # 收集素材
    assets = get_case_assets(index)
    if assets is None:
        print(f"  ⚠️  文件夹不存在，跳过")
        return False

    # 报告素材情况
    video_info = assets["video"].name if assets["video"] else "无"
    img_info = list(assets["images"].keys())
    obj_info = {k: len(v) for k, v in assets["objects"].items()}
    print(f"  视频: {video_info} | 图片: {img_info} | Objects: {obj_info}")

    try:
        parts = build_contents(client, case["zh_prompt"], assets)

        response = client.models.generate_content(
            model=MODEL,
            contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.1,
            ),
        )

        result = parse_json_response(response.text)

        # 保存输出（含元数据）
        output = {
            "index": index,
            "zh_prompt": case["zh_prompt"],
            "running_params": case["running_params"],
            "result": result,
        }
        output_file.write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(f"  ✅ 已保存: {output_file.name}")
        return True

    except Exception as e:
        print(f"  ❌ 错误: {e}")
        error_output = {
            "index": index,
            "zh_prompt": case["zh_prompt"],
            "error": str(e),
        }
        error_file = OUTPUT_DIR / f"{index}_error.json"
        error_file.write_text(json.dumps(error_output, ensure_ascii=False, indent=2))
        return False


# ===== 主函数 =====
def main():
    parser = argparse.ArgumentParser(description="Gemini 多模态参考点提取脚本")
    parser.add_argument("--index", type=str, help="只处理指定 case（如：效果广告爆款_001）")
    parser.add_argument("--limit", type=int, help="只处理前N个 case（用于测试）")
    parser.add_argument("--force", action="store_true", help="强制重新处理已有输出文件")
    parser.add_argument("--delay", type=float, default=3.0, help="每个 case 之间的间隔秒数（默认3s）")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    client = init_client()
    cases = load_cases()
    print(f"共加载 {len(cases)} 个 case")

    # 筛选
    if args.index:
        cases = [c for c in cases if c["index"] == args.index]
        if not cases:
            print(f"❌ 未找到 index = {args.index}")
            return
    if args.limit:
        cases = cases[:args.limit]

    print(f"将处理 {len(cases)} 个 case，输出目录: {OUTPUT_DIR}")

    success, failed = 0, 0
    for i, case in enumerate(cases):
        ok = process_case(client, case, force=args.force)
        if ok:
            success += 1
        else:
            failed += 1
        # 非最后一个时等待，避免 rate limit
        if i < len(cases) - 1:
            time.sleep(args.delay)

    print(f"\n========== 完成 ==========")
    print(f"✅ 成功: {success}  ❌ 失败/跳过: {failed}")
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
