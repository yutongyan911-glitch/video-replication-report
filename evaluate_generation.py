#!/usr/bin/env python3
"""
视频生成合格性评估脚本 (AI Judge)
将“原始参考素材”、“参考点分析 (P0/P1)”与“模型生成的视频”进行三方比对，
评估生成任务的命中率与质量，并给出合格性判定。

用法：
  python3 evaluate_generation.py --index 效果广告爆款_001
"""

import os
import re
import json
import time
import argparse
from pathlib import Path
from google import genai
from google.genai import types

# ===== 全局配置 =====
CREDENTIALS_PATH = "/Users/yanyutong/Downloads/llm-64897-gemini-79099acd0672.json"
BASE_DIR = Path("/Users/yanyutong/.gemini/antigravity/scratch/复刻视频文件")
REF_POINTS_DIR = BASE_DIR / "reference_points_output"
OUTPUT_DIR = BASE_DIR / "evaluation_results"
MODEL = "gemini-3.1-pro-preview"

IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# ===== 系统提示词 (AI Judge) =====
SYSTEM_PROMPT = """# 角色
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
}
"""

def init_client():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = CREDENTIALS_PATH
    client = genai.Client(vertexai=True, location="global", project="llm-64897-gemini")
    return client

def read_video_part(video_path: Path) -> types.Part:
    mime_map = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
    mime = mime_map.get(video_path.suffix.lower(), "video/mp4")
    return types.Part.from_bytes(data=video_path.read_bytes(), mime_type=mime)

def get_case_files(index: str):
    folder = BASE_DIR / index
    if not folder.exists():
        return None
    
    files = {"ref_video": None, "ref_images": [], "gen_video": None}
    
    for item in folder.iterdir():
        if item.is_file():
            # 识别原始参考视频 (通常叫 video.mp4)
            if item.name == "video.mp4":
                files["ref_video"] = item
            # 识别生成的视频 (此处逻辑：假设以 generated.mp4 或 output.mp4 命名，或者包含 "generated" 关键字)
            elif "generated" in item.name.lower() or "output" in item.name.lower() or item.name == "gen.mp4":
                files["gen_video"] = item
            elif item.stem.lower().startswith("image") and item.suffix.lower() in IMAGE_MIME:
                files["ref_images"].append(item)
    
    return files

def process_eval(client, index, force=False):
    output_file = OUTPUT_DIR / f"{index}_eval.json"
    ref_json_file = REF_POINTS_DIR / f"{index}.json"
    
    print(f"\n[评估阶段: {index}]")
    
    if not ref_json_file.exists():
        print(f"  ⚠️  找不到参考点分析文件 {ref_json_file.name}，请先运行提取脚本。")
        return False
    
    if output_file.exists() and not force:
        print(f"  已生成评估，跳过。")
        return True

    files = get_case_files(index)
    if not files or not files["gen_video"]:
        print(f"  ❌ 找不到生成的视频文件 (请确保文件名包含 'generated' 或为 'gen.mp4')。")
        return False

    try:
        # 读取参考点 JSON
        with open(ref_json_file, "r", encoding="utf-8") as f:
            ref_data = json.load(f)
        ref_points_str = json.dumps(ref_data["result"].get("reference_points", {}), ensure_ascii=False, indent=2)
        target_instructions = ref_data["result"].get("generation_instructions", "")

        parts = []
        # 1. 原始文件说明
        if files["ref_video"]:
            parts.append(types.Part.from_text(text="=== 【原始参考视频 (Referenced the video)】 ==="))
            parts.append(read_video_part(files["ref_video"]))
        
        for idx, img in enumerate(files["ref_images"]):
            parts.append(types.Part.from_text(text=f"=== 【辅助参考图片 ({img.name})】 ==="))
            parts.append(types.Part.from_bytes(data=img.read_bytes(), mime_type=IMAGE_MIME.get(img.suffix.lower())))

        # 2. 生成结果
        parts.append(types.Part.from_text(text="=== 【待评估的生成视频 (Generated Result)】 ==="))
        parts.append(read_video_part(files["gen_video"]))

        # 3. 任务数据
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
                system_instruction=SYSTEM_PROMPT,
                temperature=0.1,
            ),
        )

        res_text = response.text
        # 简单的提取 JSON 逻辑
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", res_text)
        if match:
            eval_result = json.loads(match.group(1))
        else:
            eval_result = json.loads(res_text.strip())

        # 保存结果
        output = {
            "index": index,
            "evaluation": eval_result,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        output_file.write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(f"  ✅ 评估完成: {output_file.name}")
        return True

    except Exception as e:
        print(f"  ❌ 评估出错: {e}")
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=str)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    client = init_client()

    if args.index:
        process_eval(client, args.index, args.force)
    else:
        # 遍历所有已有参考点分析的 case
        for ref_file in sorted(REF_POINTS_DIR.glob("*.json")):
            if "_error" in ref_file.name: continue
            index = ref_file.stem
            process_eval(client, index, args.force)

if __name__ == "__main__":
    main()
