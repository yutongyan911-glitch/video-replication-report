import json
import re
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound", "Not Found"})


class BlobStoreClient:
    """
    A client for interacting with an S3-compatible blob storage.

    This class provides methods to download, upload, and check the existence of files
    in an S3-compatible blob storage service.
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
        """
        Download a file from the blob storage.

        Args:
            file_path (str): The local path where the file will be saved.
            key (str): The key of the file in the bucket.
            bucket (str): The bucket name. If not provided, defaults to the instance's bucket.

        Returns:
            bool: True if the file was downloaded successfully, False otherwise.
        """
        bkt = self._bucket(bucket)
        try:
            response = self.blob_client.get_object(Bucket=bkt, Key=key)
            video_bytes = response["Body"].read()
            with open(file_path, "wb") as f:
                f.write(video_bytes)
            return True
        except Exception as e:
            print(
                f"Download failed: {e}, bucket: {bkt}, file_path: {file_path}, key: {key}"
            )
            return False

    def upload_file(self, file_path: str, key: str, bucket: str = "") -> bool:
        """
        Upload a file to the blob storage.

        Args:
            file_path (str): The local path of the file to upload.
            key (str): The key under which the file will be stored in the bucket.
            bucket (str): The bucket name. If not provided, defaults to the instance's bucket.

        Returns:
            bool: True if the file was uploaded successfully, False otherwise.
        """
        bkt = self._bucket(bucket)
        try:
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            self.blob_client.put_object(
                Bucket=bkt,
                Body=file_bytes,
                Key=key,
            )
            print(f"Upload success: bucket: {bkt}, file_path: {file_path}, key: {key}")
            return True
        except Exception as e:
            print(
                f"Upload failed: {e}, bucket: {bkt}, file_path: {file_path}, key: {key}"
            )
            return False

    def file_exists(self, key: str, bucket: str = "") -> bool:
        """
        Check if a file exists in the specified bucket and key.

        Args:
            key (str): The key of the file in the bucket.
            bucket (str): The bucket name. If not provided, defaults to self.bucket.

        Returns:
            bool: True if the file exists, False otherwise.
        """
        bkt = self._bucket(bucket)
        try:
            self.blob_client.head_object(Bucket=bkt, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _MISSING_CODES:
                return False
            print(f"Error checking if file exists: {e}, bucket: {bkt}, key: {key}")
            return False
        except Exception as e:
            print(f"Error checking if file exists: {e}, bucket: {bkt}, key: {key}")
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


def download_ref(client: BlobStoreClient, ref: str, dest: Path) -> bool:
    key = str(ref).strip()
    if not key:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    return client.download_file(str(dest), key)


def running_params_without_subject_list(rp: dict | None) -> str | None:
    if rp is None:
        return None
    rest = {k: v for k, v in rp.items() if k != "subject_list"}
    if "prefer_multi_shots" not in rest:
        rest["prefer_multi_shots"] = False
    return json.dumps(rest, ensure_ascii=False)


def _subject_url_list(subject: dict, key: str) -> list[str]:
    arr = subject.get(key)
    if not isinstance(arr, list):
        return []
    return [subject_blob_key(str(x).strip()) for x in arr if str(x).strip()]


def video_dest_names(count: int) -> list[str]:
    """Flat or subject videos: one → 'video', several → 'video 1', 'video 2', …"""
    if count == 1:
        return ["video"]
    return [f"video {i}" for i in range(1, count + 1)]


if __name__ == "__main__":
    input_file = "/Users/yanyutong/.gemini/antigravity/scratch/复刻视频文件/Omni_爆款复刻_V4_v1.0_1775529613280.xlsx"
    output_dir = "/Users/yanyutong/.gemini/antigravity/scratch/复刻视频文件"
    client = BlobStoreClient()
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    df = pd.read_excel(input_file)
    prompt_rows: list[dict] = []

    for _, row in df.iterrows():
        raw_idx = row["index"]
        if pd.isna(raw_idx):
            continue
        index = str(raw_idx).split("_")[-1]
        case_name = f"{row['类目']}_{index}"
        case_dir = out_root / case_name
        case_dir.mkdir(parents=True, exist_ok=True)

        rp = parse_running_params_cell(row.get("running_params"))
        subjects = parse_subject_list(rp)

        # 扁平参考图：逗号分隔 blob key → Image 1.ext, Image 2.ext
        flat_keys = split_blob_keys(row.get("图片blobstorekey"))
        for i, ref in enumerate(flat_keys, start=1):
            dest = case_dir / f"Image {i}{suffix_from_ref(ref)}"
            download_ref(client, ref, dest)

        # 扁平参考视频：视频blobstorekey → video.ext 或 video 1.ext …
        flat_videos = split_blob_keys(row.get("视频blobstorekey"))
        for base, ref in zip(video_dest_names(len(flat_videos)), flat_videos):
            dest = case_dir / f"{base}{suffix_from_ref(ref)}"
            download_ref(client, ref, dest)

        # 有主体：object_x 内 图片 1、2…；视频 video 或 video 1、2…
        if subjects:
            for si, subject in enumerate(subjects):
                if not isinstance(subject, dict):
                    continue
                name = str(subject.get("name") or f"object_{si + 1}").strip() or f"object_{si + 1}"
                sub_dir = case_dir / name
                image_refs = _subject_url_list(subject, "images")
                for j, key_ref in enumerate(image_refs, start=1):
                    dest = sub_dir / f"{j}{suffix_from_ref(key_ref)}"
                    download_ref(client, key_ref, dest)
                video_refs = _subject_url_list(subject, "videos")
                for base, key_ref in zip(video_dest_names(len(video_refs)), video_refs):
                    dest = sub_dir / f"{base}{suffix_from_ref(key_ref)}"
                    download_ref(client, key_ref, dest)

        prompt_rows.append(
            {
                "index": case_name,
                "中文prompt": row.get("中文prompt"),
                "英文prompt": row.get("英文prompt"),
                "running_params": running_params_without_subject_list(rp),
            }
        )

    prompt_df = pd.DataFrame(prompt_rows)
    if not prompt_df.empty and "index" in prompt_df.columns:
        prompt_df = prompt_df.sort_values("index").reset_index(drop=True)
    prompt_path = out_root / "prompt.xlsx"
    prompt_df.to_excel(prompt_path, index=False)
    print(f"Wrote {prompt_path} ({len(prompt_df)} rows)")
