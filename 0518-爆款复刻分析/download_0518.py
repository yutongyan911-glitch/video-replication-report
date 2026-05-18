import json
import re
import sys
from pathlib import Path
import pandas as pd
import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from tqdm import tqdm

_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound", "Not Found"})

class BlobStoreClient:
    """
    S3 兼容的对象存储下载客户端
    """
    def __init__(
        self,
        bucket: str = "mmu-model-eval",
        endpoint_url: str = "http://bs3-hb1.corp.kuaishou.com",
        use_ssl: bool = False,
        *,
        use_unsigned_signature: bool = True,
    ):
        config_kwargs: dict = {"s3": {"addressing_style": "path"}}
        if use_unsigned_signature:
            config_kwargs["signature_version"] = UNSIGNED
        self.blob_client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            use_ssl=use_ssl,
            config=Config(**config_kwargs),
        )
        self.bucket = bucket

    def _bucket(self, bucket: str) -> str:
        return bucket if bucket else self.bucket

    def download_file(self, file_path: str, key: str, bucket: str = "") -> bool:
        bkt = self._bucket(bucket)
        try:
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            response = self.blob_client.get_object(Bucket=bkt, Key=key)
            data_bytes = response["Body"].read()
            with open(file_path, "wb") as f:
                f.write(data_bytes)
            return True
        except Exception as e:
            print(f"Download failed: {e}, bucket: {bkt}, file_path: {file_path}, key: {key}")
            return False

def parse_running_params_cell(raw) -> dict | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, dict):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    try:
        d = json.loads(s)
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None

def parse_subject_list(rp: dict | None) -> list:
    if not rp:
        return []
    entry = rp.get("subject_list")
    if entry is None or (isinstance(entry, float) and pd.isna(entry)):
        return []
    if isinstance(entry, str):
        s = entry.strip()
        if not s:
            return []
        try:
            entry = json.loads(s)
        except json.JSONDecodeError:
            return []
    return entry if isinstance(entry, list) else []

def split_blob_keys(raw) -> list[str]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    s = str(raw).strip()
    if not s:
        return []
    parts = re.split(r"[,，;；\n|]+", s)
    return [p.strip() for p in parts if p.strip()]

_SUBJECT_BLOB_PREFIX = "mmu:model-eval:"

def subject_blob_key(ref: str) -> str:
    s = str(ref).strip()
    if s.startswith(_SUBJECT_BLOB_PREFIX):
        return s[len(_SUBJECT_BLOB_PREFIX):]
    return s

def suffix_from_ref(ref: str) -> str:
    tail = str(ref).strip().rsplit("/", maxsplit=1)[-1]
    suf = Path(tail).suffix.lower()
    return suf if suf else ".bin"

def _subject_url_list(subject: dict, key: str) -> list[str]:
    arr = subject.get(key)
    if not isinstance(arr, list):
        return []
    return [subject_blob_key(str(x).strip()) for x in arr if str(x).strip()]

def video_dest_names(count: int) -> list[str]:
    if count == 1:
        return ["video"]
    return [f"video {i}" for i in range(1, count + 1)]

def running_params_without_subject_list(rp: dict | None) -> str | None:
    if rp is None:
        return None
    rest = {k: v for k, v in rp.items() if k != "subject_list"}
    if "prefer_multi_shots" not in rest:
        rest["prefer_multi_shots"] = False
    return json.dumps(rest, ensure_ascii=False)

def main():
    base_dir = Path("/Users/yanyutong/.gemini/antigravity/scratch/复刻视频文件/0518-爆款复刻分析")
    dataset_file = base_dir / "Omni_V4_爆款复刻_过程评测集_0514.xlsx"
    infer_9455_file = base_dir / "infer_result_9455.xlsx"
    infer_9477_file = base_dir / "infer_result_9477.xlsx"

    client = BlobStoreClient()
    
    # 1. 读取过程评测集，下载所有参考素材
    print("🎬 正在读取过程评测集并下载参考素材...")
    df_dataset = pd.read_excel(dataset_file)
    index_to_case_name = {}
    
    prompt_rows = []
    
    # 先收集 index -> case_name 映射
    for _, row in df_dataset.iterrows():
        raw_idx = row["index"]
        if pd.isna(raw_idx):
            continue
        index_suffix = str(raw_idx).split("_")[-1]
        category = str(row.get("类目", "效果广告爆款")).strip()
        case_name = f"{category}_{index_suffix}"
        index_to_case_name[raw_idx] = case_name
        
    # 执行下载
    for _, row in tqdm(list(df_dataset.iterrows()), desc="下载参考素材"):
        raw_idx = row["index"]
        if pd.isna(raw_idx):
            continue
        case_name = index_to_case_name[raw_idx]
        case_dir = base_dir / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        
        # 默认桶名称
        row_bucket = str(row.get("blobstore桶")).strip() if not pd.isna(row.get("blobstore桶")) else "mmu-model-eval"

        # 扁平参考图
        flat_keys = split_blob_keys(row.get("图片blobstorekey"))
        for i, ref in enumerate(flat_keys, start=1):
            dest = case_dir / f"Image {i}{suffix_from_ref(ref)}"
            client.download_file(str(dest), ref, row_bucket)

        # 扁平参考视频
        flat_videos = split_blob_keys(row.get("视频blobstorekey"))
        for base, ref in zip(video_dest_names(len(flat_videos)), flat_videos):
            dest = case_dir / f"{base}{suffix_from_ref(ref)}"
            client.download_file(str(dest), ref, row_bucket)

        # 主体参考
        rp = parse_running_params_cell(row.get("running_params"))
        subjects = parse_subject_list(rp)
        if subjects:
            for si, subject in enumerate(subjects):
                if not isinstance(subject, dict):
                    continue
                name = str(subject.get("name") or f"object_{si + 1}").strip()
                sub_dir = case_dir / name
                
                image_refs = _subject_url_list(subject, "images")
                for j, key_ref in enumerate(image_refs, start=1):
                    dest = sub_dir / f"{j}{suffix_from_ref(key_ref)}"
                    client.download_file(str(dest), key_ref, row_bucket)
                    
                video_refs = _subject_url_list(subject, "videos")
                for base, key_ref in zip(video_dest_names(len(video_refs)), video_refs):
                    dest = sub_dir / f"{base}{suffix_from_ref(key_ref)}"
                    client.download_file(str(dest), key_ref, row_bucket)
                    
        # 准备 prompt.xlsx 结构
        prompt_rows.append({
            "index": case_name,
            "中文prompt": row.get("中文prompt"),
            "英文prompt": row.get("英文prompt"),
            "running_params": running_params_without_subject_list(rp) if rp else None,
        })
        
    # 保存该目录下的 prompt.xlsx，供生成报告用
    prompt_df = pd.DataFrame(prompt_rows)
    if not prompt_df.empty:
        prompt_df = prompt_df.sort_values("index").reset_index(drop=True)
    prompt_path = base_dir / "prompt.xlsx"
    prompt_df.to_excel(prompt_path, index=False)
    print(f"✅ 完成参考素材下载并写入 {prompt_path}")

    # 2. 下载 9455 推理结果视频
    if infer_9455_file.exists():
        print("\n🚀 正在读取 9455 推理结果并下载生成视频...")
        df_9455 = pd.read_excel(infer_9455_file)
        for _, row in tqdm(list(df_9455.iterrows()), desc="下载 9455 视频"):
            raw_idx = row["index"]
            if pd.isna(raw_idx) or raw_idx not in index_to_case_name:
                continue
            case_name = index_to_case_name[raw_idx]
            case_dir = base_dir / case_name
            
            ref = row.get("视频blobstorekey") or row.get("视频blobstoreKey")
            if pd.isna(ref) or not str(ref).strip():
                continue
                
            bucket = row.get("blobstore桶") or row.get("bucketName") or "mmu-model-eval"
            dest = case_dir / "gen_9455.mp4"
            client.download_file(str(dest), str(ref).strip(), str(bucket).strip())
            
    # 3. 下载 9477 推理结果视频
    if infer_9477_file.exists():
        print("\n🚀 正在读取 9477 推理结果并下载生成视频...")
        df_9477 = pd.read_excel(infer_9477_file)
        for _, row in tqdm(list(df_9477.iterrows()), desc="下载 9477 视频"):
            raw_idx = row["index"]
            if pd.isna(raw_idx) or raw_idx not in index_to_case_name:
                continue
            case_name = index_to_case_name[raw_idx]
            case_dir = base_dir / case_name
            
            ref = row.get("视频blobstorekey") or row.get("视频blobstoreKey")
            if pd.isna(ref) or not str(ref).strip():
                continue
                
            bucket = row.get("blobstore桶") or row.get("bucketName") or "mmu-model-eval"
            dest = case_dir / "gen_9477.mp4"
            client.download_file(str(dest), str(ref).strip(), str(bucket).strip())
            
    print("\n🎉 所有的下载任务已成功完成！")

if __name__ == "__main__":
    main()
