#!/usr/bin/env python3
import os
import json
import csv
import openpyxl
from pathlib import Path

BASE_DIR = Path("/Users/yanyutong/.gemini/antigravity/scratch/复刻视频文件")
JSON_DIR = BASE_DIR / "reference_points_output"
OUTPUT_CSV = BASE_DIR / "summary_reference_points.csv"
OUTPUT_JS = BASE_DIR / "viewer_data.js"
HTML_FILE = BASE_DIR / "viewer.html"

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

def get_assets(index):
    folder = BASE_DIR / index
    assets = {"video": None, "images": [], "objects": {}}
    if not folder.exists():
        return assets
    
    for item in sorted(folder.iterdir()):
        if item.is_file():
            suffix = item.suffix.lower()
            if suffix in [".mp4", ".mov", ".avi", ".webm"]:
                assets["video"] = f"{index}/{item.name}"
            elif item.stem.lower().startswith("image") and suffix in [".png", ".jpg", ".jpeg", ".webp", ".gif"]:
                assets["images"].append(f"{index}/{item.name}")
        elif item.is_dir() and item.name.lower().startswith("object_"):
            imgs = sorted([f"{index}/{item.name}/{f.name}" for f in item.iterdir() if f.is_file() and f.suffix.lower() in [".png", ".jpg", ".jpeg", ".webp", ".gif"]])
            if imgs:
                assets["objects"][item.name] = imgs
    return assets

def main():
    print("正在合并解析结果...")
    excel_data = read_original_excel()
    
    cases_data = []
    
    # 写入 CSV
    with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        headers = ["index", "中文prompt", "英文prompt", "running_params", 
                   "intent_summary", "inferred_task_type", "generation_instructions",
                   "traffic_code_analysis", "reference_points_json", "discard_elements_json"]
        writer.writerow(headers)
        
        # 遍历 JSON 目录
        for json_file in sorted(JSON_DIR.glob("*.json")):
            if "_error" in json_file.name:
                continue
            
            try:
                with open(json_file, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
            except Exception as e:
                print(f"解析 {json_file.name} 失败: {e}")
                continue
                
            index = data.get("index")
            if not index:
                continue
                
            res = data.get("result", {})
            
            # CSV 写入
            row = [
                index,
                data.get("zh_prompt", ""),
                excel_data.get(index, {}).get("en_prompt", ""),
                data.get("running_params", ""),
                res.get("intent_summary", ""),
                res.get("inferred_task_type", ""),
                res.get("generation_instructions", ""),
                json.dumps(res.get("traffic_code_analysis", {}), ensure_ascii=False),
                json.dumps(res.get("reference_points", {}), ensure_ascii=False),
                json.dumps(res.get("discard_elements", {}), ensure_ascii=False)
            ]
            writer.writerow(row)
            
            # 为前端组装数据
            cases_data.append({
                "id": index,
                "zh_prompt": data.get("zh_prompt", ""),
                "en_prompt": excel_data.get(index, {}).get("en_prompt", ""),
                "assets": get_assets(index),
                "result": res
            })
            
    # 生成 JS 数据文件
    js_content = f"const viewerData = {json.dumps(cases_data, ensure_ascii=False, indent=2)};"
    with open(OUTPUT_JS, "w", encoding="utf-8") as f:
        f.write(js_content)
        
    print(f"✅ 生成 CSV 成功: {OUTPUT_CSV.name}")
    print(f"✅ 生成 JS 数据成功: {OUTPUT_JS.name}")

if __name__ == "__main__":
    main()
