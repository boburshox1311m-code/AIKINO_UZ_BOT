from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path

import boto3
from aiogram import Bot
from botocore.config import Config
from boto3.s3.transfer import TransferConfig

from .database import Database

log = logging.getLogger(__name__)


class R2Storage:
    def __init__(self) -> None:
        self.account_id = os.environ.get("R2_ACCOUNT_ID", "").strip()
        self.access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
        self.secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
        self.bucket = os.environ.get("R2_BUCKET_NAME", "").strip()
        self.enabled = bool(
            self.account_id and self.access_key and self.secret_key and self.bucket
        )
        self.client = None
        self.transfer_config = None
        if self.enabled:
            self.client = boto3.client(
                "s3",
                endpoint_url=f"https://{self.account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name="auto",
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 8, "mode": "adaptive"},
                    connect_timeout=30,
                    read_timeout=300,
                    max_pool_connections=12,
                ),
            )
            self.transfer_config = TransferConfig(
                multipart_threshold=64 * 1024 * 1024,
                multipart_chunksize=64 * 1024 * 1024,
                max_concurrency=4,
                num_download_attempts=8,
                use_threads=True,
            )

    def object_key(self, movie_id: int, episode_id: int, episode_number: int) -> str:
        return f"movies/{movie_id}/episodes/{episode_id}-{episode_number}.mp4"

    async def upload_file(
        self,
        local_path: str,
        key: str,
        content_type: str = "video/mp4",
    ) -> None:
        if not self.enabled or not self.client:
            raise RuntimeError("R2 storage sozlanmagan")
        path = Path(local_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(local_path)
        await asyncio.to_thread(
            self.client.upload_file,
            str(path),
            self.bucket,
            key,
            ExtraArgs={
                "ContentType": content_type or "video/mp4",
                "CacheControl": "private, max-age=0, no-store",
            },
            Config=self.transfer_config,
        )

    def presigned_get(self, key: str, expires: int = 7200) -> str:
        if not self.enabled or not self.client:
            raise RuntimeError("R2 storage sozlanmagan")
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )

    async def create_multipart_upload(self, key: str, content_type: str = "video/mp4") -> str:
        if not self.enabled or not self.client:
            raise RuntimeError("R2 storage sozlanmagan")
        result = await asyncio.to_thread(
            self.client.create_multipart_upload,
            Bucket=self.bucket,
            Key=key,
            ContentType=content_type or "video/mp4",
            CacheControl="private, max-age=0, no-store",
        )
        return str(result["UploadId"])

    def presigned_upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        expires: int = 3600,
    ) -> str:
        if not self.enabled or not self.client:
            raise RuntimeError("R2 storage sozlanmagan")
        return self.client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": int(part_number),
            },
            ExpiresIn=expires,
        )

    async def complete_multipart_upload(
        self,
        key: str,
        upload_id: str,
        parts: list[dict],
    ) -> None:
        if not self.enabled or not self.client:
            raise RuntimeError("R2 storage sozlanmagan")
        normalized = [
            {"ETag": str(part["ETag"]), "PartNumber": int(part["PartNumber"])}
            for part in sorted(parts, key=lambda item: int(item["PartNumber"]))
        ]
        await asyncio.to_thread(
            self.client.complete_multipart_upload,
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": normalized},
        )

    async def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        if not self.enabled or not self.client:
            return
        await asyncio.to_thread(
            self.client.abort_multipart_upload,
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
        )

    async def ensure_browser_cors(self, origin: str) -> None:
        if not self.enabled or not self.client or not origin:
            return
        cors = {
            "CORSRules": [{
                "AllowedOrigins": [origin],
                "AllowedMethods": ["GET", "HEAD", "PUT"],
                "AllowedHeaders": ["*"],
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 3600,
            }]
        }
        await asyncio.to_thread(
            self.client.put_bucket_cors,
            Bucket=self.bucket,
            CORSConfiguration=cors,
        )

    async def delete_object(self, key: str) -> None:
        if not self.enabled or not self.client:
            return
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)


async def storage_worker(
    db: Database,
    bot: Bot,
    storage: R2Storage,
    stop_event: asyncio.Event,
    admin_id: int | None = None,
) -> None:
    if not storage.enabled:
        log.info("R2 storage worker disabled: credentials are not configured")
        return
    reset_count = await db.reset_stuck_storage_jobs()
    if reset_count:
        log.info("Reset %s stuck R2 storage job(s) for retry", reset_count)
    log.info("R2 storage worker started")
    while not stop_event.is_set():
        try:
            jobs = await db.episodes_needing_storage(limit=2)
            if not jobs:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=8)
                except asyncio.TimeoutError:
                    pass
                continue
            for ep in jobs:
                if stop_event.is_set():
                    break
                episode_id = int(ep["id"])
                try:
                    await db.mark_episode_storage_uploading(episode_id)
                    tg_file = await bot.get_file(ep["file_id"])
                    local_path = str(tg_file.file_path or "")
                    if not local_path:
                        raise RuntimeError("Telegram file_path bo‘sh")
                    path = Path(local_path)
                    if not path.exists():
                        # Never buffer multi-GB movies in RAM. Download straight to an ephemeral file.
                        temp_dir = Path("/tmp/aikinouz-storage")
                        temp_dir.mkdir(parents=True, exist_ok=True)
                        temp_path = temp_dir / f"episode-{episode_id}.mp4"
                        await bot.download_file(
                            local_path,
                            destination=str(temp_path),
                            timeout=3600,
                        )
                        if not temp_path.exists() or temp_path.stat().st_size <= 0:
                            raise RuntimeError("Telegram faylini yuklab bo‘lmadi")
                        path = temp_path
                    mime = ep["mime_type"] or mimetypes.guess_type(str(path))[0] or "video/mp4"
                    key = storage.object_key(
                        int(ep["movie_id"]),
                        episode_id,
                        int(ep["episode_number"]),
                    )
                    await storage.upload_file(str(path), key, mime)
                    size = path.stat().st_size
                    await db.mark_episode_storage_ready(
                        episode_id,
                        key,
                        size,
                        mime,
                    )
                    log.info("Episode %s archived to R2: %s", episode_id, key)
                    ep_after = await db.episode(episode_id)
                    if admin_id and ep_after and ep_after["movie_is_vip"]:
                        try:
                            size_gb = size / (1024 ** 3)
                            await bot.send_message(
                                admin_id,
                                "✅ <b>VIP kino Cloudflare R2 ga to‘liq yuklandi.</b>\n\n"
                                f"🎬 <b>{ep_after['movie_title']}</b>\n"
                                f"🎞 {ep_after['episode_number']}-QISM\n"
                                f"💾 {size_gb:.2f} GB\n\n"
                                "Mini App VIP bo‘limida endi tomosha qilish uchun aktiv.",
                            )
                        except Exception:
                            log.exception("VIP R2 ready admin notification failed: episode_id=%s", episode_id)
                    # Temporary Railway files are removed after R2 confirms the object.
                    if str(path).startswith("/tmp/"):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                except Exception as exc:
                    error_name = type(exc).__name__
                    safe_error = str(exc)
                    if "/var/lib/telegram-bot-api/" in safe_error:
                        safe_error = "Local Telegram file operation failed"
                    log.error(
                        "R2 archive failed: episode_id=%s error_type=%s",
                        episode_id,
                        error_name,
                    )
                    await db.mark_episode_storage_failed(
                        episode_id,
                        f"{error_name}: {safe_error}"[:500],
                    )
        except Exception as exc:
            log.error("Storage worker loop failed: error_type=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
