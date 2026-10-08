from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from html import escape

import aiohttp


SARDOR_VOICE = "uz-UZ-SardorNeural"
MADINA_VOICE = "uz-UZ-MadinaNeural"
UZBEK_VOICES = frozenset({SARDOR_VOICE, MADINA_VOICE})
_REGION_RE = re.compile(r"^[a-z0-9-]+$")


class AzureSpeechError(RuntimeError):
    """A safe-to-display Azure Speech error without secret response data."""


@dataclass(frozen=True)
class AzureSpeechConfig:
    key: str
    region: str

    @classmethod
    def from_env(cls) -> "AzureSpeechConfig":
        return cls(
            key=os.environ.get("SPEECH_KEY", "").strip(),
            region=os.environ.get("SPEECH_REGION", "").strip().lower(),
        )

    @property
    def configured(self) -> bool:
        return bool(self.key and self.region and _REGION_RE.fullmatch(self.region))


class AzureSpeechClient:
    """Small Azure Speech REST client tailored to the native Uzbek voices."""

    def __init__(self, config: AzureSpeechConfig | None = None) -> None:
        self.config = config or AzureSpeechConfig.from_env()
        self.health_status = "not_configured" if not self.config.configured else "unchecked"

    @classmethod
    def from_env(cls) -> "AzureSpeechClient":
        return cls(AzureSpeechConfig.from_env())

    @property
    def configured(self) -> bool:
        return self.config.configured

    @property
    def _base_url(self) -> str:
        if not self.config.configured:
            raise AzureSpeechError("Azure Speech sozlanmagan.")
        return f"https://{self.config.region}.tts.speech.microsoft.com"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Ocp-Apim-Subscription-Key": self.config.key,
            "User-Agent": "AIKINO-UZ-BOT",
        }

    async def check_health(self) -> str:
        """Validate credentials and confirm that both native Uzbek voices exist."""
        if not self.configured:
            self.health_status = "not_configured"
            return self.health_status

        timeout = aiohttp.ClientTimeout(total=12, connect=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self._base_url}/cognitiveservices/voices/list",
                    headers=self._headers,
                ) as response:
                    if response.status != 200:
                        self.health_status = "error"
                        return self.health_status
                    payload = await response.json(content_type=None)
            names = {
                item.get("ShortName")
                for item in payload
                if isinstance(item, dict)
            }
            self.health_status = "ok" if UZBEK_VOICES.issubset(names) else "voices_missing"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            logging.getLogger(__name__).warning(
                "Azure Speech health check failed", exc_info=True
            )
            self.health_status = "error"
        return self.health_status

    async def synthesize(
        self,
        text: str,
        *,
        voice: str,
        rate_percent: int = 0,
        pitch_hz: int = 0,
        volume_percent: int = 0,
    ) -> bytes:
        """Create 48 kHz PCM WAV from plain text using a native Uzbek voice."""
        if not self.configured:
            raise AzureSpeechError("Azure Speech sozlanmagan.")
        if voice not in UZBEK_VOICES:
            raise AzureSpeechError("Ruxsat berilmagan ovoz tanlandi.")

        clean_text = text.strip()
        if not clean_text:
            raise AzureSpeechError("Ovoz yaratish uchun matn bo‘sh.")
        if len(clean_text) > 5000:
            raise AzureSpeechError("Bitta ovoz bo‘lagi uchun matn juda uzun.")
        if not -15 <= rate_percent <= 10:
            raise AzureSpeechError("Ovoz tezligi xavfsiz oraliqdan tashqarida.")
        if not -12 <= pitch_hz <= 12:
            raise AzureSpeechError("Ovoz balandligi xavfsiz oraliqdan tashqarida.")
        if not -10 <= volume_percent <= 10:
            raise AzureSpeechError("Ovoz kuchi xavfsiz oraliqdan tashqarida.")

        ssml = self._build_ssml(
            clean_text,
            voice=voice,
            rate_percent=rate_percent,
            pitch_hz=pitch_hz,
            volume_percent=volume_percent,
        )
        headers = {
            **self._headers,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "riff-48khz-16bit-mono-pcm",
        }
        timeout = aiohttp.ClientTimeout(total=45, connect=8)

        for attempt in range(2):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        f"{self._base_url}/cognitiveservices/v1",
                        headers=headers,
                        data=ssml.encode("utf-8"),
                    ) as response:
                        audio = await response.read()
                        if response.status == 200 and audio:
                            self.health_status = "ok"
                            return audio
                        retryable = response.status == 429 or response.status >= 500
                        status = response.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                retryable = True
                status = type(exc).__name__

            if attempt == 0 and retryable:
                await asyncio.sleep(0.7)
                continue
            self.health_status = "error"
            logging.getLogger(__name__).warning(
                "Azure Speech synthesis failed: status=%s", status
            )
            raise AzureSpeechError("Azure ovoz yaratishda xatolik yuz berdi.")

        raise AzureSpeechError("Azure ovoz yaratishda xatolik yuz berdi.")

    @staticmethod
    def _build_ssml(
        text: str,
        *,
        voice: str,
        rate_percent: int,
        pitch_hz: int,
        volume_percent: int,
    ) -> str:
        rate = f"{rate_percent:+d}%"
        pitch = f"{pitch_hz:+d}Hz"
        volume = f"{volume_percent:+d}%"
        return (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xml:lang="uz-UZ">'
            f'<voice name="{voice}"><prosody rate="{rate}" pitch="{pitch}" '
            f'volume="{volume}">{escape(text)}</prosody></voice></speak>'
        )
