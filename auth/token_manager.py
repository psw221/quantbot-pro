from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from core.exceptions import AuthenticationError
from core.settings import RuntimeEnv, Settings, get_settings
from data.database import TokenStore, utc_now
from execution.kis_api import KISApiClient
from execution.writer_queue import WriterQueue


@dataclass(slots=True)
class AccessToken:
    token: str
    issued_at: datetime
    expires_at: datetime

    def is_expired(self, skew_seconds: int = 30) -> bool:
        return utc_now() >= self.expires_at - timedelta(seconds=skew_seconds)


class TokenManager:
    def __init__(
        self,
        writer_queue: WriterQueue,
        api_client: KISApiClient,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.writer_queue = writer_queue
        self.api_client = api_client
        self._tokens: dict[RuntimeEnv, AccessToken] = {}

    def get_valid_token(self, env: RuntimeEnv | None = None) -> str:
        target_env = env or self.settings.env
        token = self._tokens.get(target_env)
        if token is None or token.is_expired():
            token = self.refresh_token(target_env)
        return token.token

    def refresh_token(self, env: RuntimeEnv | None = None) -> AccessToken:
        target_env = env or self.settings.env
        payload = self.api_client.request_access_token()
        access_token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 86400))
        if not access_token:
            raise AuthenticationError("Token response did not include access_token")

        issued_at = utc_now()
        token = AccessToken(
            token=access_token,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=expires_in),
        )
        self._tokens[target_env] = token

        future = self.writer_queue.submit(
            lambda session: self._upsert_token_metadata(session, target_env, token),
            description=f"token metadata upsert:{target_env.value}",
        )
        future.result()
        return token

    @staticmethod
    def _upsert_token_metadata(session, env: RuntimeEnv, token: AccessToken) -> None:
        existing = session.scalar(select(TokenStore).where(TokenStore.env == env.value))
        if existing is None:
            session.add(
                TokenStore(
                    env=env.value,
                    issued_at=token.issued_at,
                    expires_at=token.expires_at,
                    is_valid=True,
                )
            )
            return

        existing.issued_at = token.issued_at
        existing.expires_at = token.expires_at
        existing.is_valid = True

    def invalidate_token(self, env: RuntimeEnv | None = None) -> None:
        target_env = env or self.settings.env
        self._tokens.pop(target_env, None)
        future = self.writer_queue.submit(
            lambda session: self._mark_invalid(session, target_env),
            description=f"token metadata invalidate:{target_env.value}",
        )
        future.result()

    @staticmethod
    def _mark_invalid(session, env: RuntimeEnv) -> None:
        existing = session.scalar(select(TokenStore).where(TokenStore.env == env.value))
        if existing is not None:
            existing.is_valid = False
