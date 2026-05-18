import os
import re
import json
import time
import argparse
import openpyxl
from pathlib import Path
from google import genai
from google.genai import types
from tqdm import tqdm

# ===== 全局配置 =====
CREDENTIALS_PATH = "/Users/yanyutong/Downloads/llm-64897-gemini-79099acd0672.json"
BASE_DIR = Path("/Users/yanyutong/.gemini/antigravity/scratch/复刻视频文件/0518-爆款复刻分析")
REF_POINTS_DIR = BASE_DIR / "reference_points_output"
EVAL_DIR_9455 = BASE_DIR / "evaluation_results_9455"
EVAL_DIR_9477 = BASE_DIR / "evaluation_results_9477"
MODEL = "gemini-3.1-pro-preview"

IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# ===== 参考点提取提示词 =====
SYSTEM_PROMPT_EXTRACT = """# 角色
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
3. **针对图片素材的特殊约束**：图片（Image） or 图集（object）素材绝对不允许包含任何音频层信息，其 `audio_layer` 内部必须为空。
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

# ===== AI Judge 评估提示词 =====
SYSTEM_PROMPT_EVAL = """# 角色
您是专业的 AI 视频生成质量评估专家。您的任务是通过比对“原始参考素材”、“已提取的参考点分析 (P0/P1)”以及“模型生成的视频文件”，评估生成结果是否精准命中了预设的参考点。

# 任务目标与评估逻辑
1. **核对 P0 命中率**：检查生成的视频是否精准复现了 [P0] 级别（显式核心）的参考点。P0 丢失属于严重不及格。
2. **核对 P1 命中率**：检查生成的视频是否合理延续了 [P1] 级别（隐式参考）的参考点。
3. **音频与台词校验 (关键)**：你必须仔细对比“生成的视频”与“原始参考视频”的音频。如果任务要求复用原视频台词（台词点通常在 P0/P1 中），而生成视频的台词内容、词汇表达与原视频不一致，必须判定该项为 ❌。
4. **合格性判定**：
   - **合格 (Pass)**：P0 必须全部为 ✅ 或总分 >= 60）
   - **不合格 (Fail)**：若 P0 中存在任何 ❌，则判定强制为不合格 (is_qualified: false)；关键 P0 丢失，台词严重偏离，或画面出现严重违和。

# 输入说明
- **Reference Assets**：提供给模型的原始输入素材（视频/图）。
- **Reference Points JSON**：之前步骤提取出的、要求模型必须遵循的清单。
- **Generated Video**：我们需要评估的对象。

# 输出格式 (JSON Only)
{
  "p0_evaluation": "针对 P0 点的逐项分析。请使用【✅ 达成内容】或【❌ 未达成/有缺失：简述错误】的格式。每一项占一行。",
  "p1_evaluation": "针对 P1 点的逐项分析。请使用【✅ 达成内容】或【❌ 未达成/有缺失：简述错误】的格式。每一项占一行。",
  "visual_score": 0-100,
  "audio_score": 0-100,
  "total_score": 0-100,
  "is_qualified": true/false,
  "failure_reasons": "如果不合格，请列出具体原因；如果合格，填 null",
  "scoring_logic": "视觉权重 70%，音频权重 30%。总分 = visual_score * 0.7 + audio_score * 0.3。合格判定条件：P0 必须全部为 ✅ 或总分合理（或总分 >= 60）。若 P0 中存在任何 ❌，则判定强制为不合格 (is_qualified: false)。"
}"""

# ===== 客户端初始化 =====
def init_client():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = CREDENTIALS_PATH
    return genai.Client(vertexai=True, location="global", project="llm-64897-gemini")

# ===== ffmpeg 视频极致压缩优化 =====
def get_optimized_video_bytes(video_path: Path) -> tuple[bytes, str]:
    """
    如果视频文件超过 15MB，利用 ffmpeg 在本地压缩至 720p 24fps 1500kbps，
    在保留足够的动作/画面细节的同时降低传输体积。
    """
    suffix = video_path.suffix.lower()
    mime_map = {".mp4": "video/mp4", ".mov": "video/quicktime",
                ".avi": "video/avi", ".webm": "video/webm"}
    mime = mime_map.get(suffix, "video/mp4")
    
    size_mb = video_path.stat().st_size / 1024 / 1024
    if size_mb <= 15.0:
        return video_path.read_bytes(), mime
        
    temp_path = video_path.parent / f"_temp_compressed_{video_path.name}"
    # 720p, 24fps, 1500kbps 视频码率, 128kbps 音频码率
    cmd = f'ffmpeg -y -i "{video_path}" -vf "scale=-2:720" -r 24 -b:v 1500k -c:a aac -b:a 128k "{temp_path}" >/dev/null 2>&1'
    os.system(cmd)
    
    if temp_path.exists():
        comp_size_mb = temp_path.stat().st_size / 1024 / 1024
        print(f"\n    ⚡️ ffmpeg 压缩: {video_path.name} ({size_mb:.1f} MB -> {comp_size_mb:.1f} MB)")
        data = temp_path.read_bytes()
        try:
            temp_path.unlink()
        except:
            pass
        return data, "video/mp4"
    else:
        return video_path.read_bytes(), mime


def read_video_inline(video_path: Path) -> types.Part:
    data, mime = get_optimized_video_bytes(video_path)
    return types.Part.from_bytes(data=data, mime_type=mime)

# ===== 辅助素材搜集 =====
def get_case_assets(case_index: str) -> dict | None:
    folder = BASE_DIR / case_index
    if not folder.exists():
        return None

    assets = {
        "video": None,
        "images": {},
        "objects": {},
    }

    for item in sorted(folder.iterdir()):
        if item.is_file():
            suffix = item.suffix.lower()
            stem = item.stem
            if suffix in [".mp4", ".mov", ".avi", ".webm"]:
                if not item.name.startswith("gen_") and item.name != "gen.mp4":
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

def build_contents_extract(zh_prompt: str, assets: dict) -> list:
    parts = []
    mapping_lines = []

    if assets["video"]:
        parts.append(read_video_inline(assets["video"]))
        mapping_lines.append("【上方视频】= the video")

    for img_name in sorted(assets["images"].keys()):
        img_path = assets["images"][img_name]
        mime = IMAGE_MIME.get(img_path.suffix.lower(), "image/png")
        parts.append(types.Part.from_bytes(data=img_path.read_bytes(), mime_type=mime))
        mapping_lines.append(f"【上方图片】= {img_name}")

    for obj_name in sorted(assets["objects"].keys()):
        obj_images = assets["objects"][obj_name]
        for img_path in obj_images:
            mime = IMAGE_MIME.get(img_path.suffix.lower(), "image/png")
            parts.append(types.Part.from_bytes(data=img_path.read_bytes(), mime_type=mime))
        mapping_lines.append(f"【上方连续 {len(obj_images)} 张图片】= {obj_name}")

    asset_desc = "\n".join(mapping_lines) if mapping_lines else "（无额外素材）"
    text_part = f"""## 素材映射说明
{asset_desc}

## 用户的原始生成指令（中文prompt）
{zh_prompt}

## 任务
请根据以上素材和指令，提取参考点，严格以 JSON 格式输出，不要输出任何其他内容。"""
    parts.append(types.Part.from_text(text=text_part))
    return parts

def parse_json_response(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return {"raw_response": text, "parse_error": "JSON 解析失败"}

# ===== 核心流程：1. 参考点提取 =====
def run_reference_extraction(client, cases, force=False, delay=1.0):
    REF_POINTS_DIR.mkdir(parents=True, exist_ok=True)
    print("\n==================== 步骤 1: 提取参考点 (Reference Points Extraction) ====================")
    
    success, failed = 0, 0
    for case in tqdm(cases, desc="提取参考点"):
        index = case["index"]
        output_file = REF_POINTS_DIR / f"{index}.json"
        
        if output_file.exists() and not force:
            success += 1
            continue

        assets = get_case_assets(index)
        if not assets:
            print(f"\n  ⚠️  [{index}] 目录不存在，跳过。")
            failed += 1
            continue

        try:
            parts = build_contents_extract(case["zh_prompt"], assets)
            response = client.models.generate_content(
                model=MODEL,
                contents=parts,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT_EXTRACT,
                    temperature=0.1,
                ),
            )
            result = parse_json_response(response.text)
            
            output = {
                "index": index,
                "zh_prompt": case["zh_prompt"],
                "running_params": case["running_params"],
                "result": result,
            }
            output_file.write_text(json.dumps(output, ensure_ascii=False, indent=2))
            success += 1
            time.sleep(delay)
        except Exception as e:
            print(f"\n  ❌ [{index}] 提取出错: {e}")
            failed += 1
            
    print(f"🎉 步骤 1 完成！成功: {success} | 失败/跳过: {failed}")

# ===== 核心流程：2. 视频生成评测 =====
def run_evaluation_for_batch(client, cases, batch_id, eval_dir, force=False, delay=1.0):
    eval_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n==================== 步骤 2: 评测视频生成质量 (Batch: {batch_id}) ====================")
    
    success, failed = 0, 0
    for case in tqdm(cases, desc=f"评测 Batch {batch_id}"):
        index = case["index"]
        output_file = eval_dir / f"{index}_eval.json"
        ref_json_file = REF_POINTS_DIR / f"{index}.json"
        
        if output_file.exists() and not force:
            success += 1
            continue

        if not ref_json_file.exists():
            print(f"\n  ⚠️  [{index}] 缺失参考点 JSON，跳过评测。")
            failed += 1
            continue

        case_dir = BASE_DIR / index
        gen_video = case_dir / f"gen_{batch_id}.mp4"
        if not gen_video.exists():
            gen_video = case_dir / f"gen{batch_id}.mp4"
            
        if not gen_video.exists():
            found = list(case_dir.glob(f"*{batch_id}*.mp4"))
            if found:
                gen_video = found[0]
                
        if not gen_video.exists():
            print(f"\n  ❌ [{index}] 找不到生成视频文件 (gen_{batch_id}.mp4)")
            failed += 1
            continue

        assets = get_case_assets(index)
        
        try:
            with open(ref_json_file, "r", encoding="utf-8") as f:
                ref_data = json.load(f)
            ref_points_str = json.dumps(ref_data["result"].get("reference_points", {}), ensure_ascii=False, indent=2)
            target_instructions = ref_data["result"].get("generation_instructions", "")

            parts = []
            
            if assets and assets["video"]:
                parts.append(types.Part.from_text(text="=== 【原始参考视频 (Referenced the video)】 ==="))
                parts.append(read_video_inline(assets["video"]))
                
            if assets:
                for img_name, img_path in sorted(assets["images"].items()):
                    parts.append(types.Part.from_text(text=f"=== 【辅助参考图片 ({img_name})】 ==="))
                    parts.append(types.Part.from_bytes(data=img_path.read_bytes(), mime_type=IMAGE_MIME.get(img_path.suffix.lower(), "image/png")))

            parts.append(types.Part.from_text(text=f"=== 【待评估的生成视频 (Batch {batch_id} Result)】 ==="))
            parts.append(read_video_inline(gen_video))

            parts.append(types.Part.from_text(text=f"""
## 系统提取的参考点清单 (P0/P1 List):
{ref_points_str}

## 本次生成的最终指令:
{target_instructions}

请针对上述“生成的视频”进行打分评估。检查其是否在视觉和音频上命中了 P0/P1 点。
"""))

            response = client.models.generate_content(
                model=MODEL,
                contents=parts,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT_EVAL,
                    temperature=0.1,
                ),
            )

            eval_result = parse_json_response(response.text)
            
            output = {
                "index": index,
                "evaluation": eval_result,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            output_file.write_text(json.dumps(output, ensure_ascii=False, indent=2))
            success += 1
            time.sleep(delay)
        except Exception as e:
            print(f"\n  ❌ [{index}] 评测出错: {e}")
            failed += 1
            
    print(f"🎉 Batch {batch_id} 评测完成！成功: {success} | 失败/跳过: {failed}")

# ===== 主控入口 =====
def main():
    parser = argparse.ArgumentParser(description="0518 爆款复刻一键端到端评测控制台")
    parser.add_argument("--force", action="store_true", help="强制覆盖已分析的记录")
    parser.add_argument("--delay", type=float, default=1.0, help="API 请求间隔延迟秒数")
    args = parser.parse_args()

    client = init_client()

    prompt_file = BASE_DIR / "prompt.xlsx"
    if not prompt_file.exists():
        print(f"❌ 错误: 找不到 {prompt_file}")
        return

    wb = openpyxl.load_workbook(prompt_file)
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
    
    print(f"🎬 共加载了 {len(cases)} 个 Case 开始多阶段分析流程。")

    # 1. 运行参考点提取
    run_reference_extraction(client, cases, force=args.force, delay=args.delay)

    # 2. 评测 gen_9455
    run_evaluation_for_batch(client, cases, "9455", EVAL_DIR_9455, force=args.force, delay=args.delay)

    # 3. 评测 gen_9477
    run_evaluation_for_batch(client, cases, "9477", EVAL_DIR_9477, force=args.force, delay=args.delay)

    print("\n🎉 所有的分析和评估任务已全部顺利完成！")

if __name__ == "__main__":
    main()
