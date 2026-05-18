#!/usr/bin/env python3
import os
import json
import csv
import openpyxl
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_JS = BASE_DIR / "viewer_data.js"

def read_original_excel():
    wb = openpyxl.load_workbook(BASE_DIR / "prompt.xlsx")
    ws = wb.active
    data = {}
    for row in ws.iter_rows(values_only=True, min_row=2):
        if row[0]:
            data[str(row[0]).strip()] = {
                "zh_prompt": str(row[1]).strip() if row[1] else "",
                "en_prompt": str(row[2]).strip() if row[2] else "",
                "running_params": row[3],
            }
    return data

def get_assets(index, batch_suffix=None):
    folder = BASE_DIR / index
    assets = {"ref_video": None, "gen_video": None, "images": [], "objects": {}}
    if not folder.exists():
        return assets
    
    items = sorted(folder.iterdir())
    
    for item in items:
        if item.is_file():
            suffix = item.suffix.lower()
            if suffix in [".mp4", ".mov", ".avi", ".webm"]:
                fname = item.name.lower()
                rel_path = f"0518-爆款复刻分析/{index}/{item.name}"
                if fname == "video.mp4" or fname == "video1.mp4" or fname == "video 1.mp4":
                    assets["ref_video"] = rel_path
                elif not assets["ref_video"] and "ref" in fname:
                    assets["ref_video"] = rel_path
            elif item.stem.lower().startswith("image") and suffix in [".png", ".jpg", ".jpeg", ".webp", ".gif"]:
                assets["images"].append(f"0518-爆款复刻分析/{index}/{item.name}")
        elif item.is_dir() and item.name.lower().startswith("object_"):
            imgs = sorted([f"0518-爆款复刻分析/{index}/{item.name}/{f.name}" for f in item.iterdir() if f.is_file() and f.suffix.lower() in [".png", ".jpg", ".jpeg", ".webp", ".gif"]])
            if imgs:
                assets["objects"][item.name] = imgs
                
    # Now find the generated video
    if batch_suffix:
        suf_lower = batch_suffix.lower()
        for item in items:
            if item.is_file() and item.suffix.lower() in [".mp4", ".mov", ".avi", ".webm"]:
                fname = item.name.lower()
                if suf_lower in fname and ("gen" in fname or "output" in fname):
                    assets["gen_video"] = f"0518-爆款复刻分析/{index}/{item.name}"
                    break
                    
    if not assets["gen_video"]:
        for item in items:
            if item.is_file() and item.suffix.lower() in [".mp4", ".mov", ".avi", ".webm"]:
                fname = item.name.lower()
                rel_path = f"0518-爆款复刻分析/{index}/{item.name}"
                if fname in ["gen.mp4", "generated.mp4", "output.mp4"]:
                    assets["gen_video"] = rel_path
                    break
                elif "generated" in fname or "output" in fname:
                    assets["gen_video"] = rel_path
                    break
                    
    if not assets["ref_video"]:
        for item in items:
            if item.is_file() and item.suffix.lower() in [".mp4", ".mov", ".avi", ".webm"]:
                rel_path = f"0518-爆款复刻分析/{index}/{item.name}"
                if rel_path != assets["gen_video"]:
                    assets["ref_video"] = rel_path
                    break
                    
    return assets

def main():
    print("正在合并 0518 爆款复刻解析结果...")
    excel_data = read_original_excel()
    
    batches = {} # batch_name -> (json_dir, eval_dir, output_csv_path, suffix)
    
    # 扫描所有 evaluation_results 目录来动态发现批次！
    for item in BASE_DIR.iterdir():
        if item.is_dir() and item.name.startswith("evaluation_results_"):
            suffix = item.name[len("evaluation_results_"):]
            if not suffix:
                continue
            # 格式化名称，例如 9455 变为 9455 批次
            batch_name = f"{suffix.upper()} 批次"
            
            # 查找参考点：优先找专属的 reference_points_output_<suffix>，找不到则用默认的 reference_points_output
            json_dir = BASE_DIR / f"reference_points_output_{suffix}"
            if not json_dir.exists():
                json_dir = BASE_DIR / "reference_points_output"
                
            eval_dir = item
            csv_path = BASE_DIR / f"summary_reference_points_{suffix}.csv"
            batches[batch_name] = (json_dir, eval_dir, csv_path, suffix)
            
    print(f"检测到评估批次: {list(batches.keys())}")
    
    viewer_batches = {}
    
    for batch_name, (json_dir, eval_dir, csv_path, suffix) in batches.items():
        print(f"正在编译批次: {batch_name} ...")
        cases_data = []
        
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            headers = ["index", "中文prompt", "英文prompt", "running_params", 
                       "intent_summary", "inferred_task_type", "generation_instructions",
                       "traffic_code_analysis", "score", "is_qualified",
                       "evaluation_detail", "reference_points_json", "discard_elements_json"]
            writer.writerow(headers)
            
            for json_file in sorted(json_dir.glob("*.json")):
                if "_error" in json_file.name:
                    continue
                
                try:
                    with open(json_file, "r", encoding="utf-8") as jf:
                        data = json.load(jf)
                except Exception as e:
                    print(f"  解析 {json_file.name} 失败: {e}")
                    continue
                    
                index = data.get("index")
                if not index:
                    continue
                    
                res = data.get("result", {})
                
                eval_file = eval_dir / f"{index}_eval.json"
                eval_res = {}
                if eval_file.exists():
                    try:
                        with open(eval_file, "r", encoding="utf-8") as ef:
                            eval_data = json.load(ef)
                            eval_res = eval_data.get("evaluation", {})
                    except Exception as e:
                        print(f"  解析评估文件 {eval_file.name} 失败: {e}")
                
                # 写入 CSV 行
                writer.writerow([
                    index,
                    data.get("zh_prompt", ""),
                    excel_data.get(index, {}).get("en_prompt", ""),
                    data.get("running_params", ""),
                    res.get("intent_summary", ""),
                    res.get("inferred_task_type", ""),
                    res.get("generation_instructions", ""),
                    json.dumps(res.get("traffic_code_analysis", {}), ensure_ascii=False),
                    eval_res.get("total_score", 0),
                    eval_res.get("is_qualified", False),
                    eval_res.get("failure_reasons", ""),
                    json.dumps(res.get("reference_points", {}), ensure_ascii=False),
                    json.dumps(res.get("discard_elements", {}), ensure_ascii=False)
                ])
                
                cases_data.append({
                    "id": index,
                    "zh_prompt": data.get("zh_prompt", ""),
                    "en_prompt": excel_data.get(index, {}).get("en_prompt", ""),
                    "assets": get_assets(index, batch_suffix=suffix),
                    "result": res,
                    "evaluation": eval_res
                })
                
        # 按照 ID 升序对案例排序，保证侧边栏一致性
        cases_data.sort(key=lambda x: x["id"])
        viewer_batches[batch_name] = cases_data
        print(f"  批次 {batch_name} 编译完成，共 {len(cases_data)} 个案例。")
        
    # 保存结果到 viewer_data.js
    js_content = f"const viewerBatches = {json.dumps(viewer_batches, ensure_ascii=False, indent=2)};"
    OUTPUT_JS.write_text(js_content, encoding="utf-8")
    print(f"✅ 完成！已导出 {OUTPUT_JS}")

if __name__ == "__main__":
    main()
