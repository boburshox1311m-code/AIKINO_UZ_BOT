from __future__ import annotations

import asyncpg


class Database:
    def __init__(self, url: str):
        self.url = url
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.url, min_size=1, max_size=5)

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def init_schema(self):
        assert self.pool
        await self.pool.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT '🎬',
                poster_file_id TEXT,
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            ALTER TABLE movies ADD COLUMN IF NOT EXISTS poster_file_id TEXT;
            ALTER TABLE movies ADD COLUMN IF NOT EXISTS description TEXT;
            ALTER TABLE movies ADD COLUMN IF NOT EXISTS is_vip BOOLEAN NOT NULL DEFAULT FALSE;
            CREATE UNIQUE INDEX IF NOT EXISTS movies_title_lower_uq ON movies (LOWER(title));
            CREATE TABLE IF NOT EXISTS episodes (
                id BIGSERIAL PRIMARY KEY,
                movie_id BIGINT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                episode_number INTEGER NOT NULL CHECK (episode_number > 0),
                title TEXT,
                file_id TEXT,
                file_unique_id TEXT,
                caption TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(movie_id, episode_number)
            );
            ALTER TABLE episodes ADD COLUMN IF NOT EXISTS r2_key TEXT;
            ALTER TABLE episodes ADD COLUMN IF NOT EXISTS storage_status TEXT NOT NULL DEFAULT 'pending';
            ALTER TABLE episodes ADD COLUMN IF NOT EXISTS storage_error TEXT;
            ALTER TABLE episodes ADD COLUMN IF NOT EXISTS storage_attempts INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE episodes ADD COLUMN IF NOT EXISTS video_size BIGINT;
            ALTER TABLE episodes ADD COLUMN IF NOT EXISTS mime_type TEXT;
            CREATE INDEX IF NOT EXISTS episodes_movie_number_idx ON episodes(movie_id, episode_number);
            CREATE INDEX IF NOT EXISTS episodes_storage_status_idx ON episodes(storage_status, id);
            CREATE TABLE IF NOT EXISTS direct_upload_sessions (
                id BIGSERIAL PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                admin_id BIGINT NOT NULL,
                episode_id BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                r2_key TEXT NOT NULL,
                upload_id TEXT NOT NULL,
                file_name TEXT,
                file_size BIGINT,
                mime_type TEXT,
                status TEXT NOT NULL DEFAULT 'uploading'
                    CHECK (status IN ('uploading','completed','aborted','failed')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS direct_upload_sessions_episode_idx
                ON direct_upload_sessions(episode_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS direct_upload_sessions_status_idx
                ON direct_upload_sessions(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS watch_progress (
                user_id BIGINT PRIMARY KEY,
                episode_id BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                position_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                duration_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            ALTER TABLE watch_progress ADD COLUMN IF NOT EXISTS position_seconds DOUBLE PRECISION NOT NULL DEFAULT 0;
            ALTER TABLE watch_progress ADD COLUMN IF NOT EXISTS duration_seconds DOUBLE PRECISION NOT NULL DEFAULT 0;
            CREATE INDEX IF NOT EXISTS watch_progress_updated_idx ON watch_progress(updated_at DESC);
            CREATE TABLE IF NOT EXISTS movie_watch_progress (
                user_id BIGINT NOT NULL,
                movie_id BIGINT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                episode_id BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                position_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                duration DOUBLE PRECISION NOT NULL DEFAULT 0,
                last_watched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(user_id, movie_id)
            );
            CREATE INDEX IF NOT EXISTS movie_watch_progress_updated_idx
                ON movie_watch_progress(last_watched_at DESC);
            CREATE INDEX IF NOT EXISTS movie_watch_progress_movie_idx
                ON movie_watch_progress(movie_id, last_watched_at DESC);
            CREATE TABLE IF NOT EXISTS favorite_movies (
                user_id BIGINT NOT NULL,
                movie_id BIGINT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(user_id, movie_id)
            );
            CREATE INDEX IF NOT EXISTS favorite_movies_user_created_idx
                ON favorite_movies(user_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS favorite_episodes (
                user_id BIGINT NOT NULL,
                episode_id BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(user_id, episode_id)
            );
            CREATE INDEX IF NOT EXISTS favorite_episodes_user_created_idx
                ON favorite_episodes(user_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id BIGINT PRIMARY KEY,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            );
            ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'uz';
            CREATE INDEX IF NOT EXISTS bot_users_last_seen_idx
                ON bot_users(last_seen_at DESC);
            CREATE TABLE IF NOT EXISTS episode_views (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                episode_id BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS episode_views_user_date_idx
                ON episode_views(user_id, viewed_at DESC);
            CREATE INDEX IF NOT EXISTS episode_views_episode_date_idx
                ON episode_views(episode_id, viewed_at DESC);
            CREATE TABLE IF NOT EXISTS app_visits (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                visited_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS app_visits_user_date_idx
                ON app_visits(user_id, visited_at DESC);
            CREATE INDEX IF NOT EXISTS app_visits_date_idx
                ON app_visits(visited_at DESC);
            CREATE TABLE IF NOT EXISTS app_watch_events (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                episode_id BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS app_watch_events_user_date_idx
                ON app_watch_events(user_id, viewed_at DESC);
            CREATE INDEX IF NOT EXISTS app_watch_events_episode_date_idx
                ON app_watch_events(episode_id, viewed_at DESC);
            CREATE TABLE IF NOT EXISTS movie_requests (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'done')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS movie_requests_status_created_idx
                ON movie_requests(status, created_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS movie_requests_pending_user_title_uq
                ON movie_requests(user_id, LOWER(title)) WHERE status='pending';
            ALTER TABLE movie_requests ADD COLUMN IF NOT EXISTS replied_at TIMESTAMPTZ;
            ALTER TABLE movie_requests ADD COLUMN IF NOT EXISTS last_reply_text TEXT;
            CREATE TABLE IF NOT EXISTS vip_users (
                user_id BIGINT PRIMARY KEY,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS vip_users_expires_idx ON vip_users(expires_at);
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            INSERT INTO bot_settings(key, value) VALUES
                ('vip_price_stars', '100'),
                ('stars_payments_enabled', 'true'),
                ('vip_stars_plans', '10:50,20:80,30:100')
            ON CONFLICT (key) DO NOTHING;
            CREATE TABLE IF NOT EXISTS payment_terms_acceptance (
                user_id BIGINT PRIMARY KEY,
                accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS vip_payments (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                telegram_payment_charge_id TEXT NOT NULL UNIQUE,
                provider_payment_charge_id TEXT,
                invoice_payload TEXT NOT NULL,
                currency TEXT NOT NULL,
                total_amount INTEGER NOT NULL,
                subscription_expires_at TIMESTAMPTZ NOT NULL,
                is_recurring BOOLEAN NOT NULL DEFAULT FALSE,
                is_first_recurring BOOLEAN NOT NULL DEFAULT FALSE,
                renewal_canceled BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS vip_payments_user_created_idx
                ON vip_payments(user_id, created_at DESC);
            INSERT INTO bot_settings(key, value) VALUES
                ('manual_payments_enabled', 'false'),
                ('manual_card_number', ''),
                ('manual_card_holder', ''),
                ('vip_price_uzs', '50000'),
                ('vip_days', '30'),
                ('manual_vip_plans', '10:15000,29:25000,30:40000,180:200000,365:400000')
            ON CONFLICT (key) DO NOTHING;
            CREATE TABLE IF NOT EXISTS manual_payment_requests (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount_uzs BIGINT NOT NULL,
                vip_days INTEGER NOT NULL DEFAULT 30,
                receipt_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reviewed_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS manual_payment_requests_status_created_idx
                ON manual_payment_requests(status, created_at DESC);
            ALTER TABLE manual_payment_requests ADD COLUMN IF NOT EXISTS vip_days INTEGER NOT NULL DEFAULT 30;
            CREATE TABLE IF NOT EXISTS broadcast_history (
                id BIGSERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL,
                source_chat_id BIGINT NOT NULL,
                source_message_id BIGINT NOT NULL,
                content_type TEXT NOT NULL,
                preview_text TEXT,
                link_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                total_recipients INTEGER NOT NULL DEFAULT 0,
                sent_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS broadcast_history_created_idx
                ON broadcast_history(created_at DESC);
            CREATE TABLE IF NOT EXISTS admin_action_history (
                id BIGSERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL,
                admin_name TEXT,
                action TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS admin_action_history_created_idx
                ON admin_action_history(created_at DESC);
        """)

    async def add_movie(self, title: str, emoji: str = "🎬"):
        assert self.pool
        return await self.pool.fetchrow(
            "INSERT INTO movies(title, emoji) VALUES($1,$2) RETURNING *", title.strip(), emoji.strip() or "🎬"
        )

    async def movies(self, offset=0, limit=10):
        assert self.pool
        return await self.pool.fetch("SELECT * FROM movies ORDER BY created_at DESC, id DESC OFFSET $1 LIMIT $2", offset, limit)

    async def movie_count(self):
        assert self.pool
        return await self.pool.fetchval("SELECT COUNT(*) FROM movies")

    async def public_movies(self, offset=0, limit=10):
        assert self.pool
        return await self.pool.fetch("""
            SELECT m.*, COUNT(v.id) AS view_count
            FROM movies m
            LEFT JOIN episodes e ON e.movie_id=m.id
            LEFT JOIN episode_views v ON v.episode_id=e.id
            WHERE m.is_vip=FALSE
            GROUP BY m.id
            ORDER BY m.created_at DESC, m.id DESC
            OFFSET $1 LIMIT $2
        """, offset, limit)

    async def public_movie_count(self):
        assert self.pool
        return await self.pool.fetchval("SELECT COUNT(*) FROM movies WHERE is_vip=FALSE")

    async def latest_movies(self, limit: int = 8):
        assert self.pool
        return await self.pool.fetch("""
            SELECT m.*, COUNT(v.id) AS view_count
            FROM movies m
            LEFT JOIN episodes e ON e.movie_id=m.id
            LEFT JOIN episode_views v ON v.episode_id=e.id
            WHERE m.is_vip=FALSE
            GROUP BY m.id
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT $1
        """, limit)

    async def miniapp_movies(self, offset: int = 0, limit: int = 36):
        assert self.pool
        return await self.pool.fetch("""
            SELECT m.*, COUNT(v.id) AS view_count,
                   COUNT(DISTINCT e.id) FILTER (
                       WHERE (
                           e.file_id IS NOT NULL
                           OR (e.storage_status='ready' AND e.r2_key IS NOT NULL)
                         )
                         AND (
                           m.is_vip=FALSE
                           OR (e.storage_status='ready' AND e.r2_key IS NOT NULL)
                         )
                   ) AS episode_count
            FROM movies m
            LEFT JOIN episodes e ON e.movie_id=m.id
            LEFT JOIN episode_views v ON v.episode_id=e.id
            WHERE m.is_vip=FALSE
               OR EXISTS (
                   SELECT 1 FROM episodes ready_ep
                   WHERE ready_ep.movie_id=m.id
                     AND ready_ep.storage_status='ready'
                     AND ready_ep.r2_key IS NOT NULL
               )
            GROUP BY m.id
            ORDER BY m.created_at DESC, m.id DESC
            OFFSET $1 LIMIT $2
        """, max(0, offset), max(1, min(limit, 50)))

    async def miniapp_movie_count(self):
        assert self.pool
        return int(await self.pool.fetchval("""
            SELECT COUNT(*)
            FROM movies m
            WHERE m.is_vip=FALSE
               OR EXISTS (
                   SELECT 1 FROM episodes ready_ep
                   WHERE ready_ep.movie_id=m.id
                     AND ready_ep.file_id IS NOT NULL
                     AND ready_ep.storage_status='ready'
                     AND ready_ep.r2_key IS NOT NULL
               )
        """) or 0)

    async def movie_with_stats(self, movie_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT m.*, COUNT(v.id) AS view_count,
                   COUNT(DISTINCT e.id) FILTER (WHERE e.file_id IS NOT NULL) AS episode_count
            FROM movies m
            LEFT JOIN episodes e ON e.movie_id=m.id
            LEFT JOIN episode_views v ON v.episode_id=e.id
            WHERE m.id=$1
            GROUP BY m.id
        """, movie_id)

    async def trending_movies(self, admin_id: int, limit: int = 8):
        assert self.pool
        return await self.pool.fetch("""
            SELECT m.*, COUNT(v.id) AS view_count
            FROM movies m
            LEFT JOIN episodes e ON e.movie_id=m.id
            LEFT JOIN episode_views v ON v.episode_id=e.id AND v.user_id<>$1
            WHERE m.is_vip=FALSE
            GROUP BY m.id
            HAVING COUNT(v.id) >= 1000
            ORDER BY COUNT(v.id) DESC, m.created_at DESC, m.id DESC
            LIMIT $2
        """, admin_id, limit)

    async def vip_movies(self, offset=0, limit=10):
        assert self.pool
        return await self.pool.fetch("""
            SELECT * FROM movies WHERE is_vip=TRUE
            ORDER BY created_at DESC, id DESC OFFSET $1 LIMIT $2
        """, offset, limit)

    async def vip_movie_count(self):
        assert self.pool
        return await self.pool.fetchval("SELECT COUNT(*) FROM movies WHERE is_vip=TRUE")

    async def movie(self, movie_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT m.*, COUNT(v.id) AS view_count
            FROM movies m
            LEFT JOIN episodes e ON e.movie_id=m.id
            LEFT JOIN episode_views v ON v.episode_id=e.id
            WHERE m.id=$1
            GROUP BY m.id
        """, movie_id)

    async def search_movies(self, query: str, limit=20):
        assert self.pool
        return await self.pool.fetch(
            "SELECT * FROM movies WHERE is_vip=FALSE AND title ILIKE $1 ORDER BY title LIMIT $2",
            f"%{query.strip()}%",
            limit,
        )

    async def rename_movie(self, movie_id: int, title: str):
        assert self.pool
        return await self.pool.execute("UPDATE movies SET title=$2 WHERE id=$1", movie_id, title.strip())

    async def set_movie_poster(self, movie_id: int, poster_file_id: str | None):
        assert self.pool
        return await self.pool.execute(
            "UPDATE movies SET poster_file_id=$2 WHERE id=$1", movie_id, poster_file_id
        )

    async def set_movie_description(self, movie_id: int, description: str | None):
        assert self.pool
        clean = description.strip() if description else None
        return await self.pool.execute(
            "UPDATE movies SET description=$2 WHERE id=$1", movie_id, clean or None
        )

    async def delete_movie(self, movie_id: int):
        assert self.pool
        return await self.pool.execute("DELETE FROM movies WHERE id=$1", movie_id)

    async def upsert_episode(self, movie_id: int, number: int, file_id=None, file_unique_id=None, caption=None):
        assert self.pool
        return await self.pool.fetchrow("""
            INSERT INTO episodes(movie_id, episode_number, file_id, file_unique_id, caption)
            VALUES($1,$2,$3,$4,$5)
            ON CONFLICT(movie_id, episode_number) DO UPDATE SET
                file_id=COALESCE(EXCLUDED.file_id, episodes.file_id),
                file_unique_id=COALESCE(EXCLUDED.file_unique_id, episodes.file_unique_id),
                caption=COALESCE(EXCLUDED.caption, episodes.caption),
                r2_key=CASE WHEN EXCLUDED.file_id IS NOT NULL THEN NULL ELSE episodes.r2_key END,
                storage_status=CASE WHEN EXCLUDED.file_id IS NOT NULL THEN 'pending' ELSE episodes.storage_status END,
                storage_error=CASE WHEN EXCLUDED.file_id IS NOT NULL THEN NULL ELSE episodes.storage_error END,
                storage_attempts=CASE WHEN EXCLUDED.file_id IS NOT NULL THEN 0 ELSE episodes.storage_attempts END
            RETURNING *
        """, movie_id, number, file_id, file_unique_id, caption)

    async def episodes(self, movie_id: int, offset=0, limit=12):
        assert self.pool
        return await self.pool.fetch("""
            SELECT * FROM episodes WHERE movie_id=$1 AND file_id IS NOT NULL
            ORDER BY episode_number OFFSET $2 LIMIT $3
        """, movie_id, offset, limit)

    async def miniapp_episodes(self, movie_id: int, is_vip: bool, offset: int = 0, limit: int = 200):
        assert self.pool
        if is_vip:
            return await self.pool.fetch("""
                SELECT * FROM episodes
                WHERE movie_id=$1
                  AND storage_status='ready'
                  AND r2_key IS NOT NULL
                ORDER BY episode_number
                OFFSET $2 LIMIT $3
            """, movie_id, max(0, offset), max(1, min(limit, 200)))
        return await self.pool.fetch("""
            SELECT * FROM episodes
            WHERE movie_id=$1
              AND (
                   file_id IS NOT NULL
                   OR (storage_status='ready' AND r2_key IS NOT NULL)
              )
            ORDER BY episode_number
            OFFSET $2 LIMIT $3
        """, movie_id, max(0, offset), max(1, min(limit, 200)))

    async def all_episodes_admin(self, movie_id: int, offset=0, limit=12):
        assert self.pool
        return await self.pool.fetch("SELECT * FROM episodes WHERE movie_id=$1 ORDER BY episode_number OFFSET $2 LIMIT $3", movie_id, offset, limit)

    async def episode_count(self, movie_id: int, ready_only=True):
        assert self.pool
        extra = " AND file_id IS NOT NULL" if ready_only else ""
        return await self.pool.fetchval(f"SELECT COUNT(*) FROM episodes WHERE movie_id=$1{extra}", movie_id)

    async def episode(self, episode_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT e.*, m.title AS movie_title, m.emoji AS movie_emoji,
                   m.is_vip AS movie_is_vip
            FROM episodes e JOIN movies m ON m.id=e.movie_id WHERE e.id=$1
        """, episode_id)

    async def episodes_needing_storage(self, limit: int = 2):
        assert self.pool
        return await self.pool.fetch("""
            SELECT e.*
            FROM episodes e
            WHERE e.file_id IS NOT NULL
              AND e.r2_key IS NULL
              AND e.storage_attempts < 8
              AND e.storage_status IN ('pending', 'failed')
            ORDER BY e.id
            LIMIT $1
        """, limit)

    async def mark_episode_storage_pending(
        self,
        episode_id: int,
        mime_type: str | None = None,
        video_size: int | None = None,
    ):
        assert self.pool
        return await self.pool.execute("""
            UPDATE episodes
            SET storage_status='pending',
                storage_error=NULL,
                mime_type=COALESCE($2, mime_type),
                video_size=COALESCE($3, video_size)
            WHERE id=$1
        """, episode_id, mime_type, video_size)

    async def mark_episode_storage_uploading(self, episode_id: int):
        assert self.pool
        return await self.pool.execute("""
            UPDATE episodes
            SET storage_status='uploading',
                storage_attempts=storage_attempts+1,
                storage_error=NULL
            WHERE id=$1
        """, episode_id)

    async def mark_episode_storage_ready(
        self,
        episode_id: int,
        r2_key: str,
        video_size: int | None,
        mime_type: str | None,
    ):
        assert self.pool
        return await self.pool.execute("""
            UPDATE episodes
            SET r2_key=$2,
                storage_status='ready',
                storage_error=NULL,
                video_size=COALESCE($3, video_size),
                mime_type=COALESCE($4, mime_type)
            WHERE id=$1
        """, episode_id, r2_key, video_size, mime_type)

    async def mark_episode_storage_failed(self, episode_id: int, error: str):
        assert self.pool
        return await self.pool.execute("""
            UPDATE episodes
            SET storage_status='failed',
                storage_error=$2
            WHERE id=$1
        """, episode_id, error)

    async def reset_stuck_storage_jobs(self) -> int:
        assert self.pool
        rows = await self.pool.fetch("""
            UPDATE episodes
            SET storage_status='pending',
                storage_error=NULL,
                storage_attempts=CASE
                    WHEN storage_attempts >= 8 THEN 0
                    ELSE storage_attempts
                END
            WHERE r2_key IS NULL
              AND file_id IS NOT NULL
              AND (
                    storage_attempts >= 8
                    OR storage_status='uploading'
              )
            RETURNING id
        """)
        return len(rows)

    async def active_direct_upload_for_episode(self, episode_id: int, admin_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT * FROM direct_upload_sessions
            WHERE episode_id=$1 AND admin_id=$2 AND status='uploading'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """, episode_id, admin_id)

    async def create_direct_upload_session(
        self,
        token: str,
        admin_id: int,
        episode_id: int,
        r2_key: str,
        upload_id: str,
        file_name: str | None,
        file_size: int | None,
        mime_type: str | None,
    ):
        assert self.pool
        await self.pool.execute("""
            UPDATE direct_upload_sessions
            SET status='aborted'
            WHERE episode_id=$1 AND status='uploading'
        """, episode_id)
        return await self.pool.fetchrow("""
            INSERT INTO direct_upload_sessions(
                token, admin_id, episode_id, r2_key, upload_id,
                file_name, file_size, mime_type, status
            )
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,'uploading')
            RETURNING *
        """, token, admin_id, episode_id, r2_key, upload_id,
             file_name, file_size, mime_type)

    async def direct_upload_session(self, token: str, admin_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT s.*, e.movie_id, e.episode_number,
                   m.title AS movie_title, m.is_vip AS movie_is_vip
            FROM direct_upload_sessions s
            JOIN episodes e ON e.id=s.episode_id
            JOIN movies m ON m.id=e.movie_id
            WHERE s.token=$1 AND s.admin_id=$2
            LIMIT 1
        """, token, admin_id)

    async def finish_direct_upload_session(self, token: str, admin_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            UPDATE direct_upload_sessions
            SET status='completed', completed_at=NOW()
            WHERE token=$1 AND admin_id=$2 AND status='uploading'
            RETURNING *
        """, token, admin_id)

    async def abort_direct_upload_session(self, token: str, admin_id: int, failed: bool = False):
        assert self.pool
        return await self.pool.fetchrow("""
            UPDATE direct_upload_sessions
            SET status=$3
            WHERE token=$1 AND admin_id=$2 AND status='uploading'
            RETURNING *
        """, token, admin_id, "failed" if failed else "aborted")

    async def storage_backlog_count(self) -> int:
        assert self.pool
        return int(await self.pool.fetchval("""
            SELECT COUNT(*)
            FROM episodes
            WHERE file_id IS NOT NULL
              AND r2_key IS NULL
        """) or 0)

    async def adjacent_episode(self, movie_id: int, number: int, direction: str):
        assert self.pool
        if direction == "prev":
            return await self.pool.fetchrow("SELECT id FROM episodes WHERE movie_id=$1 AND episode_number<$2 AND (file_id IS NOT NULL OR (storage_status='ready' AND r2_key IS NOT NULL)) ORDER BY episode_number DESC LIMIT 1", movie_id, number)
        return await self.pool.fetchrow("SELECT id FROM episodes WHERE movie_id=$1 AND episode_number>$2 AND (file_id IS NOT NULL OR (storage_status='ready' AND r2_key IS NOT NULL)) ORDER BY episode_number LIMIT 1", movie_id, number)

    async def latest_episodes(self, limit=12):
        assert self.pool
        return await self.pool.fetch("""
            SELECT e.*, m.title AS movie_title, m.emoji AS movie_emoji
            FROM episodes e JOIN movies m ON m.id=e.movie_id
            WHERE (e.file_id IS NOT NULL OR (e.storage_status='ready' AND e.r2_key IS NOT NULL)) AND m.is_vip=FALSE
            ORDER BY e.created_at DESC LIMIT $1
        """, limit)

    async def is_vip_user(self, user_id: int) -> bool:
        assert self.pool
        return bool(await self.pool.fetchval("""
            SELECT EXISTS(
                SELECT 1 FROM vip_users WHERE user_id=$1 AND expires_at>NOW()
            )
        """, user_id))

    async def vip_user(self, user_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT * FROM vip_users WHERE user_id=$1 AND expires_at>NOW()
        """, user_id)

    async def grant_vip(self, user_id: int, days: int):
        assert self.pool
        return await self.pool.fetchrow("""
            INSERT INTO vip_users(user_id, expires_at)
            VALUES($1, NOW() + make_interval(days => $2))
            ON CONFLICT(user_id) DO UPDATE SET
                expires_at=GREATEST(vip_users.expires_at, NOW()) + make_interval(days => $2),
                updated_at=NOW()
            RETURNING *
        """, user_id, days)

    async def revoke_vip(self, user_id: int):
        assert self.pool
        return await self.pool.fetchrow(
            "DELETE FROM vip_users WHERE user_id=$1 RETURNING *", user_id
        )

    async def active_vip_users(self, limit: int = 100):
        assert self.pool
        return await self.pool.fetch("""
            SELECT * FROM vip_users WHERE expires_at>NOW()
            ORDER BY expires_at LIMIT $1
        """, limit)

    async def set_movie_vip(self, movie_id: int, is_vip: bool):
        assert self.pool
        return await self.pool.fetchrow(
            "UPDATE movies SET is_vip=$2 WHERE id=$1 RETURNING *", movie_id, is_vip
        )

    async def save_watch_progress(
        self,
        user_id: int,
        episode_id: int,
        position_seconds: float = 0,
        duration_seconds: float = 0,
    ):
        assert self.pool
        position = max(0.0, float(position_seconds or 0))
        duration = max(0.0, float(duration_seconds or 0))
        if duration > 0:
            position = min(position, duration)
        movie_id = await self.pool.fetchval(
            "SELECT movie_id FROM episodes WHERE id=$1",
            episode_id,
        )
        if movie_id:
            await self.pool.execute("""
                INSERT INTO movie_watch_progress(
                    user_id, movie_id, episode_id, position_seconds, duration, last_watched_at
                )
                VALUES($1,$2,$3,$4,$5,NOW())
                ON CONFLICT(user_id, movie_id) DO UPDATE SET
                    episode_id=EXCLUDED.episode_id,
                    position_seconds=EXCLUDED.position_seconds,
                    duration=EXCLUDED.duration,
                    last_watched_at=NOW()
            """, user_id, movie_id, episode_id, position, duration)
        return await self.pool.execute("""
            INSERT INTO watch_progress(
                user_id, episode_id, position_seconds, duration_seconds, updated_at
            )
            VALUES($1, $2, $3, $4, NOW())
            ON CONFLICT(user_id) DO UPDATE SET
                episode_id=EXCLUDED.episode_id,
                position_seconds=EXCLUDED.position_seconds,
                duration_seconds=EXCLUDED.duration_seconds,
                updated_at=NOW()
        """, user_id, episode_id, position, duration)

    async def record_episode_view(self, user_id: int, episode_id: int):
        assert self.pool
        return await self.pool.execute(
            "INSERT INTO episode_views(user_id, episode_id) VALUES($1,$2)",
            user_id,
            episode_id,
        )

    async def record_episode_view_once(self, user_id: int, episode_id: int):
        assert self.pool
        return await self.pool.execute("""
            INSERT INTO episode_views(user_id, episode_id)
            SELECT $1, $2
            WHERE NOT EXISTS (
                SELECT 1
                FROM episode_views
                WHERE user_id=$1
                  AND episode_id=$2
                  AND viewed_at > NOW() - INTERVAL '6 hours'
            )
        """, user_id, episode_id)

    async def watch_progress(self, user_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT e.*, m.title AS movie_title, m.emoji AS movie_emoji,
                   m.is_vip AS movie_is_vip,
                   w.position_seconds AS position_seconds,
                   w.duration_seconds AS duration_seconds
            FROM watch_progress w
            JOIN episodes e ON e.id=w.episode_id
            JOIN movies m ON m.id=e.movie_id
            WHERE w.user_id=$1 AND e.file_id IS NOT NULL
        """, user_id)

    async def movie_watch_progress(self, user_id: int, movie_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT * FROM movie_watch_progress
            WHERE user_id=$1 AND movie_id=$2
        """, user_id, movie_id)

    async def vip_movie_watch_stats(self, movie_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT
                (SELECT COUNT(*)
                 FROM app_watch_events a
                 JOIN episodes e ON e.id=a.episode_id
                 WHERE e.movie_id=$1) AS views,
                (SELECT COUNT(DISTINCT a.user_id)
                 FROM app_watch_events a
                 JOIN episodes e ON e.id=a.episode_id
                 WHERE e.movie_id=$1) AS unique_viewers,
                (SELECT COALESCE(AVG(p.position_seconds),0)
                 FROM movie_watch_progress p
                 WHERE p.movie_id=$1) AS average_watch_time,
                (SELECT COUNT(*)
                 FROM movie_watch_progress p
                 WHERE p.movie_id=$1
                   AND p.duration>0
                   AND p.position_seconds/p.duration>=0.90) AS completed_views
        """, movie_id)

    async def vip_movies_watch_stats(self, limit: int = 20):
        assert self.pool
        return await self.pool.fetch("""
            SELECT
                m.id,
                m.title,
                COALESCE(v.views,0) AS views,
                COALESCE(v.unique_viewers,0) AS unique_viewers,
                COALESCE(p.average_watch_time,0) AS average_watch_time,
                COALESCE(p.completed_views,0) AS completed_views
            FROM movies m
            LEFT JOIN (
                SELECT e.movie_id,
                       COUNT(*) AS views,
                       COUNT(DISTINCT a.user_id) AS unique_viewers
                FROM app_watch_events a
                JOIN episodes e ON e.id=a.episode_id
                GROUP BY e.movie_id
            ) v ON v.movie_id=m.id
            LEFT JOIN (
                SELECT movie_id,
                       AVG(position_seconds) AS average_watch_time,
                       COUNT(*) FILTER (
                           WHERE duration>0 AND position_seconds/duration>=0.90
                       ) AS completed_views
                FROM movie_watch_progress
                GROUP BY movie_id
            ) p ON p.movie_id=m.id
            WHERE m.is_vip=TRUE
              AND EXISTS (
                  SELECT 1 FROM episodes e2
                  WHERE e2.movie_id=m.id
                    AND e2.storage_status='ready'
                    AND e2.r2_key IS NOT NULL
              )
            ORDER BY views DESC, m.created_at DESC
            LIMIT $1
        """, max(1, min(limit, 100)))

    async def is_movie_favorite(self, user_id: int, movie_id: int) -> bool:
        assert self.pool
        return bool(await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM favorite_movies WHERE user_id=$1 AND movie_id=$2)",
            user_id,
            movie_id,
        ))

    async def toggle_movie_favorite(self, user_id: int, movie_id: int) -> bool:
        assert self.pool
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                added = await connection.fetchval("""
                    INSERT INTO favorite_movies(user_id, movie_id)
                    VALUES($1, $2)
                    ON CONFLICT DO NOTHING
                    RETURNING TRUE
                """, user_id, movie_id)
                if added:
                    return True
                await connection.execute(
                    "DELETE FROM favorite_movies WHERE user_id=$1 AND movie_id=$2",
                    user_id,
                    movie_id,
                )
                return False

    async def favorite_movies(self, user_id: int, limit: int = 100):
        assert self.pool
        return await self.pool.fetch("""
            SELECT m.*
            FROM favorite_movies f
            JOIN movies m ON m.id=f.movie_id
            WHERE f.user_id=$1
            ORDER BY f.created_at DESC
            LIMIT $2
        """, user_id, limit)

    async def is_episode_favorite(self, user_id: int, episode_id: int) -> bool:
        assert self.pool
        return bool(await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM favorite_episodes WHERE user_id=$1 AND episode_id=$2)",
            user_id,
            episode_id,
        ))

    async def toggle_episode_favorite(self, user_id: int, episode_id: int) -> bool:
        assert self.pool
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                added = await connection.fetchval("""
                    INSERT INTO favorite_episodes(user_id, episode_id)
                    VALUES($1, $2)
                    ON CONFLICT DO NOTHING
                    RETURNING TRUE
                """, user_id, episode_id)
                if added:
                    return True
                await connection.execute(
                    "DELETE FROM favorite_episodes WHERE user_id=$1 AND episode_id=$2",
                    user_id,
                    episode_id,
                )
                return False

    async def favorite_episodes(self, user_id: int, limit: int = 100):
        assert self.pool
        return await self.pool.fetch("""
            SELECT e.*, m.title AS movie_title, m.emoji AS movie_emoji,
                   m.is_vip AS movie_is_vip
            FROM favorite_episodes f
            JOIN episodes e ON e.id=f.episode_id
            JOIN movies m ON m.id=e.movie_id
            WHERE f.user_id=$1 AND e.file_id IS NOT NULL
            ORDER BY f.created_at DESC
            LIMIT $2
        """, user_id, limit)

    async def track_user(self, user_id: int):
        assert self.pool
        return await self.pool.execute("""
            INSERT INTO bot_users(user_id, first_seen_at, last_seen_at)
            VALUES($1, NOW(), NOW())
            ON CONFLICT(user_id) DO UPDATE SET last_seen_at=NOW(), is_active=TRUE
        """, user_id)

    async def user_language(self, user_id: int) -> str:
        assert self.pool
        value = await self.pool.fetchval(
            "SELECT language FROM bot_users WHERE user_id=$1",
            user_id,
        )
        return value if value in {"uz", "ru", "en"} else "uz"

    async def set_user_language(self, user_id: int, language: str):
        assert self.pool
        if language not in {"uz", "ru", "en"}:
            raise ValueError("Unsupported language")
        return await self.pool.execute("""
            INSERT INTO bot_users(user_id, first_seen_at, last_seen_at, is_active, language)
            VALUES($1, NOW(), NOW(), TRUE, $2)
            ON CONFLICT(user_id) DO UPDATE SET
                language=EXCLUDED.language,
                last_seen_at=NOW(),
                is_active=TRUE
        """, user_id, language)

    async def broadcast_user_ids(self, admin_id: int):
        assert self.pool
        return await self.pool.fetch(
            "SELECT user_id FROM bot_users WHERE user_id<>$1 AND is_active=TRUE ORDER BY user_id",
            admin_id,
        )

    async def create_broadcast_history(
        self,
        admin_id: int,
        source_chat_id: int,
        source_message_id: int,
        content_type: str,
        preview_text: str | None,
        link_enabled: bool,
        total_recipients: int,
    ):
        assert self.pool
        return await self.pool.fetchrow("""
            INSERT INTO broadcast_history(
                admin_id, source_chat_id, source_message_id, content_type,
                preview_text, link_enabled, total_recipients
            )
            VALUES($1,$2,$3,$4,$5,$6,$7)
            RETURNING *
        """, admin_id, source_chat_id, source_message_id, content_type,
            preview_text, link_enabled, total_recipients)

    async def finish_broadcast_history(self, history_id: int, sent: int, failed: int):
        assert self.pool
        return await self.pool.fetchrow("""
            UPDATE broadcast_history
            SET sent_count=$2, failed_count=$3, completed_at=NOW()
            WHERE id=$1
            RETURNING *
        """, history_id, sent, failed)

    async def broadcast_history_count(self):
        assert self.pool
        return await self.pool.fetchval("SELECT COUNT(*) FROM broadcast_history")

    async def broadcast_history(self, offset: int = 0, limit: int = 10):
        assert self.pool
        return await self.pool.fetch("""
            SELECT * FROM broadcast_history
            ORDER BY created_at DESC, id DESC
            OFFSET $1 LIMIT $2
        """, offset, limit)

    async def broadcast_history_item(self, history_id: int):
        assert self.pool
        return await self.pool.fetchrow(
            "SELECT * FROM broadcast_history WHERE id=$1", history_id
        )

    async def delete_broadcast_history(self, history_id: int):
        assert self.pool
        return await self.pool.fetchrow(
            "DELETE FROM broadcast_history WHERE id=$1 RETURNING *", history_id
        )

    async def mark_user_inactive(self, user_id: int):
        assert self.pool
        return await self.pool.execute(
            "UPDATE bot_users SET is_active=FALSE WHERE user_id=$1",
            user_id,
        )

    async def create_movie_request(self, user_id: int, title: str):
        assert self.pool
        clean_title = title.strip()
        async with self.pool.acquire() as connection:
            duplicate = await connection.fetchrow("""
                SELECT * FROM movie_requests
                WHERE user_id=$1 AND LOWER(title)=LOWER($2) AND status='pending'
                LIMIT 1
            """, user_id, clean_title)
            if duplicate:
                return "duplicate", duplicate
            recent = await connection.fetchval("""
                SELECT EXISTS(
                    SELECT 1 FROM movie_requests
                    WHERE user_id=$1 AND created_at > NOW() - INTERVAL '60 seconds'
                )
            """, user_id)
            if recent:
                return "cooldown", None
            request = await connection.fetchrow("""
                INSERT INTO movie_requests(user_id, title)
                VALUES($1, $2)
                RETURNING *
            """, user_id, clean_title)
            return "created", request

    async def movie_request_count(self):
        assert self.pool
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM movie_requests WHERE status='pending'"
        )

    async def movie_requests(self, offset: int = 0, limit: int = 10):
        assert self.pool
        return await self.pool.fetch("""
            SELECT * FROM movie_requests
            WHERE status='pending'
            ORDER BY created_at, id
            OFFSET $1 LIMIT $2
        """, offset, limit)

    async def movie_request(self, request_id: int):
        assert self.pool
        return await self.pool.fetchrow(
            "SELECT * FROM movie_requests WHERE id=$1", request_id
        )

    async def mark_movie_request_replied(self, request_id: int, reply_text: str):
        assert self.pool
        return await self.pool.fetchrow("""
            UPDATE movie_requests
            SET replied_at=NOW(), last_reply_text=$2
            WHERE id=$1 AND status='pending'
            RETURNING *
        """, request_id, reply_text.strip())

    async def complete_movie_request(self, request_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            UPDATE movie_requests
            SET status='done', completed_at=NOW()
            WHERE id=$1 AND status='pending'
            RETURNING *
        """, request_id)

    async def delete_movie_request(self, request_id: int):
        assert self.pool
        return await self.pool.fetchrow(
            "DELETE FROM movie_requests WHERE id=$1 RETURNING *", request_id
        )

    async def record_episode_view(self, user_id: int, episode_id: int):
        assert self.pool
        return await self.pool.execute(
            "INSERT INTO episode_views(user_id, episode_id) VALUES($1, $2)",
            user_id,
            episode_id,
        )

    async def record_app_visit_once(self, user_id: int):
        assert self.pool
        return await self.pool.execute("""
            INSERT INTO app_visits(user_id)
            SELECT $1
            WHERE NOT EXISTS (
                SELECT 1 FROM app_visits
                WHERE user_id=$1
                  AND visited_at > NOW() - INTERVAL '30 minutes'
            )
        """, user_id)

    async def record_app_watch_once(self, user_id: int, episode_id: int):
        assert self.pool
        return await self.pool.execute("""
            INSERT INTO app_watch_events(user_id, episode_id)
            SELECT $1, $2
            WHERE NOT EXISTS (
                SELECT 1 FROM app_watch_events
                WHERE user_id=$1
                  AND episode_id=$2
                  AND viewed_at > NOW() - INTERVAL '6 hours'
            )
        """, user_id, episode_id)

    async def app_statistics(self, admin_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT
                (SELECT COUNT(DISTINCT user_id) FROM app_visits WHERE user_id<>$1) AS total_users,
                (SELECT COUNT(*) FROM app_visits WHERE user_id<>$1) AS total_sessions,
                (SELECT COUNT(DISTINCT user_id) FROM app_visits
                    WHERE user_id<>$1
                      AND (visited_at AT TIME ZONE 'Europe/London')::date =
                          (NOW() AT TIME ZONE 'Europe/London')::date) AS today_users,
                (SELECT COUNT(*) FROM app_visits
                    WHERE user_id<>$1
                      AND (visited_at AT TIME ZONE 'Europe/London')::date =
                          (NOW() AT TIME ZONE 'Europe/London')::date) AS today_sessions,
                (SELECT COUNT(DISTINCT user_id) FROM app_visits
                    WHERE user_id<>$1 AND visited_at >= NOW() - INTERVAL '7 days') AS users_7d,
                (SELECT COUNT(DISTINCT user_id) FROM app_visits
                    WHERE user_id<>$1 AND visited_at >= NOW() - INTERVAL '30 days') AS users_30d,
                (SELECT COUNT(DISTINCT user_id) FROM app_watch_events WHERE user_id<>$1) AS total_viewers,
                (SELECT COUNT(*) FROM app_watch_events WHERE user_id<>$1) AS total_views,
                (SELECT COUNT(DISTINCT user_id) FROM app_watch_events
                    WHERE user_id<>$1
                      AND (viewed_at AT TIME ZONE 'Europe/London')::date =
                          (NOW() AT TIME ZONE 'Europe/London')::date) AS today_viewers,
                (SELECT COUNT(*) FROM app_watch_events
                    WHERE user_id<>$1
                      AND (viewed_at AT TIME ZONE 'Europe/London')::date =
                          (NOW() AT TIME ZONE 'Europe/London')::date) AS today_views,
                (SELECT COUNT(*) FROM vip_users WHERE expires_at>NOW() AND user_id<>$1) AS active_vips
        """, admin_id)

    async def app_top_movies(self, admin_id: int, limit: int = 5):
        assert self.pool
        return await self.pool.fetch("""
            SELECT m.id, m.title, m.emoji, COUNT(*) AS view_count,
                   COUNT(DISTINCT v.user_id) AS unique_viewers
            FROM app_watch_events v
            JOIN episodes e ON e.id=v.episode_id
            JOIN movies m ON m.id=e.movie_id
            WHERE v.user_id<>$1
            GROUP BY m.id, m.title, m.emoji
            ORDER BY view_count DESC, unique_viewers DESC, m.title
            LIMIT $2
        """, admin_id, limit)

    async def statistics(self, admin_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT
                (SELECT COUNT(*) FROM bot_users WHERE user_id<>$1) AS total_users,
                (SELECT COUNT(*) FROM bot_users
                    WHERE user_id<>$1
                      AND (last_seen_at AT TIME ZONE 'Europe/London')::date =
                          (NOW() AT TIME ZONE 'Europe/London')::date) AS today_users,
                (SELECT COUNT(*) FROM vip_users WHERE expires_at>NOW()) AS active_vips,
                (SELECT COUNT(*) FROM episode_views WHERE user_id<>$1) AS total_views,
                (SELECT COUNT(*) FROM episode_views
                    WHERE user_id<>$1
                      AND (viewed_at AT TIME ZONE 'Europe/London')::date =
                          (NOW() AT TIME ZONE 'Europe/London')::date) AS today_views,
                (SELECT COUNT(*) FROM movies) AS movie_count,
                (SELECT COUNT(*) FROM episodes WHERE file_id IS NOT NULL) AS episode_count,
                (SELECT COUNT(*) FROM favorite_movies WHERE user_id<>$1) AS movie_favorites,
                (SELECT COUNT(*) FROM favorite_episodes WHERE user_id<>$1) AS episode_favorites
        """, admin_id)

    async def top_viewed_movies(self, admin_id: int, limit: int = 5):
        assert self.pool
        return await self.pool.fetch("""
            SELECT m.id, m.title, m.emoji, COUNT(*) AS view_count
            FROM episode_views v
            JOIN episodes e ON e.id=v.episode_id
            JOIN movies m ON m.id=e.movie_id
            WHERE v.user_id<>$1
            GROUP BY m.id, m.title, m.emoji
            ORDER BY view_count DESC, m.title
            LIMIT $2
        """, admin_id, limit)

    async def get_setting(self, key: str, default: str | None = None):
        assert self.pool
        value = await self.pool.fetchval(
            "SELECT value FROM bot_settings WHERE key=$1", key
        )
        return default if value is None else value

    async def set_setting(self, key: str, value: str):
        assert self.pool
        return await self.pool.execute("""
            INSERT INTO bot_settings(key, value)
            VALUES($1, $2)
            ON CONFLICT (key) DO UPDATE
            SET value=EXCLUDED.value, updated_at=NOW()
        """, key, value)

    async def stars_payments_enabled(self) -> bool:
        return (await self.get_setting("stars_payments_enabled", "true")).lower() == "true"

    async def stars_plans(self) -> list[tuple[int, int]]:
        raw = await self.get_setting("vip_stars_plans", "10:50,20:80,30:100")
        plans: list[tuple[int, int]] = []
        try:
            for item in raw.split(","):
                days_text, stars_text = item.strip().split(":", 1)
                days, stars = int(days_text), int(stars_text)
                if 1 <= days <= 3650 and 1 <= stars <= 10000:
                    plans.append((days, stars))
        except (TypeError, ValueError):
            plans = []
        return sorted(set(plans)) or [(10, 50), (20, 80), (30, 100)]

    async def set_stars_plans(self, plans: list[tuple[int, int]]):
        value = ",".join(f"{days}:{stars}" for days, stars in sorted(set(plans)))
        await self.set_setting("vip_stars_plans", value)
        for days, stars in plans:
            if days == 30:
                await self.set_setting("vip_price_stars", str(stars))
                break

    async def has_accepted_payment_terms(self, user_id: int) -> bool:
        assert self.pool
        return bool(await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM payment_terms_acceptance WHERE user_id=$1)",
            user_id,
        ))

    async def accept_payment_terms(self, user_id: int):
        assert self.pool
        return await self.pool.execute("""
            INSERT INTO payment_terms_acceptance(user_id, accepted_at)
            VALUES($1, NOW())
            ON CONFLICT(user_id) DO UPDATE SET accepted_at=NOW()
        """, user_id)

    async def vip_price_stars(self) -> int:
        value = await self.get_setting("vip_price_stars", "100")
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 100

    async def set_vip_price_stars(self, amount: int):
        await self.set_setting("vip_price_stars", str(amount))

    async def record_vip_payment(
        self,
        user_id: int,
        telegram_charge_id: str,
        provider_charge_id: str | None,
        invoice_payload: str,
        currency: str,
        total_amount: int,
        expires_at,
        is_recurring: bool,
        is_first_recurring: bool,
    ) -> bool:
        assert self.pool
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                inserted = await connection.fetchrow("""
                    INSERT INTO vip_payments(
                        user_id, telegram_payment_charge_id,
                        provider_payment_charge_id, invoice_payload,
                        currency, total_amount, subscription_expires_at,
                        is_recurring, is_first_recurring
                    )
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    ON CONFLICT (telegram_payment_charge_id) DO NOTHING
                    RETURNING id
                """, user_id, telegram_charge_id, provider_charge_id,
                    invoice_payload, currency, total_amount, expires_at,
                    is_recurring, is_first_recurring)
                if not inserted:
                    return False
                await connection.execute("""
                    INSERT INTO vip_users(user_id, expires_at)
                    VALUES($1, $2)
                    ON CONFLICT (user_id) DO UPDATE
                    SET expires_at=GREATEST(vip_users.expires_at, EXCLUDED.expires_at),
                        updated_at=NOW()
                """, user_id, expires_at)
                return True

    async def active_subscription_payment(self, user_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT * FROM vip_payments
            WHERE user_id=$1
              AND subscription_expires_at>NOW()
              AND renewal_canceled=FALSE
            ORDER BY created_at DESC
            LIMIT 1
        """, user_id)

    async def cancel_subscription_renewal(self, telegram_charge_id: str):
        assert self.pool
        return await self.pool.execute("""
            UPDATE vip_payments
            SET renewal_canceled=TRUE
            WHERE telegram_payment_charge_id=$1
        """, telegram_charge_id)

    async def vip_payment_stats(self):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT COUNT(*) AS payment_count,
                   COALESCE(SUM(total_amount), 0) AS total_stars
            FROM vip_payments
        """)

    async def manual_vip_plans(self) -> list[tuple[int, int]]:
        raw = await self.get_setting("manual_vip_plans", "")
        plans: list[tuple[int, int]] = []
        if raw:
            try:
                for item in raw.split(","):
                    days_text, price_text = item.strip().split(":", 1)
                    days, price = int(days_text), int(price_text)
                    if 1 <= days <= 3650 and 1000 <= price <= 1_000_000_000:
                        plans.append((days, price))
            except (TypeError, ValueError):
                plans = []
        if plans:
            return sorted(set(plans))
        return [
            (10, 15000),
            (29, 25000),
            (30, 40000),
            (180, 200000),
            (365, 400000),
        ]

    async def set_manual_vip_plans(self, plans: list[tuple[int, int]]):
        value = ",".join(f"{days}:{price}" for days, price in sorted(set(plans)))
        await self.set_setting("manual_vip_plans", value)

    async def manual_payment_settings(self):
        return {
            "enabled": (await self.get_setting("manual_payments_enabled", "false")).lower() == "true",
            "card_number": await self.get_setting("manual_card_number", ""),
            "card_holder": await self.get_setting("manual_card_holder", ""),
            "price_uzs": int(await self.get_setting("vip_price_uzs", "50000")),
            "vip_days": int(await self.get_setting("vip_days", "30")),
            "plans": await self.manual_vip_plans(),
        }

    async def create_manual_payment_request(
        self,
        user_id: int,
        amount_uzs: int,
        vip_days: int = 30,
    ):
        assert self.pool
        existing = await self.pool.fetchrow("""
            SELECT * FROM manual_payment_requests
            WHERE user_id=$1 AND status='pending'
            ORDER BY created_at DESC LIMIT 1
        """, user_id)
        if existing:
            return "duplicate", existing
        request = await self.pool.fetchrow("""
            INSERT INTO manual_payment_requests(user_id, amount_uzs, vip_days)
            VALUES($1, $2, $3)
            RETURNING *
        """, user_id, amount_uzs, vip_days)
        return "created", request

    async def set_manual_payment_receipt(self, request_id: int, file_id: str):
        assert self.pool
        return await self.pool.execute("""
            UPDATE manual_payment_requests
            SET receipt_file_id=$2
            WHERE id=$1
        """, request_id, file_id)

    async def manual_payment_request(self, request_id: int):
        assert self.pool
        return await self.pool.fetchrow(
            "SELECT * FROM manual_payment_requests WHERE id=$1", request_id
        )

    async def review_manual_payment(self, request_id: int, status: str):
        assert self.pool
        if status not in {"approved", "rejected"}:
            raise ValueError("Invalid manual payment status")
        return await self.pool.fetchrow("""
            UPDATE manual_payment_requests
            SET status=$2, reviewed_at=NOW()
            WHERE id=$1 AND status='pending'
            RETURNING *
        """, request_id, status)

    async def add_admin_action(
        self,
        admin_id: int,
        admin_name: str | None,
        action: str,
        details: str | None = None,
    ):
        assert self.pool
        return await self.pool.execute("""
            INSERT INTO admin_action_history(admin_id, admin_name, action, details)
            VALUES($1, $2, $3, $4)
        """, admin_id, admin_name, action, details)

    async def admin_action_count(self) -> int:
        assert self.pool
        return await self.pool.fetchval("SELECT COUNT(*) FROM admin_action_history")

    async def admin_actions(self, offset: int = 0, limit: int = 10):
        assert self.pool
        return await self.pool.fetch("""
            SELECT id, admin_id, admin_name, action, details, created_at
            FROM admin_action_history
            ORDER BY created_at DESC, id DESC
            OFFSET $1 LIMIT $2
        """, offset, limit)

    async def set_episode_number(self, episode_id: int, number: int):
        assert self.pool
        return await self.pool.execute("UPDATE episodes SET episode_number=$2 WHERE id=$1", episode_id, number)

    async def set_episode_video(self, episode_id: int, file_id: str, file_unique_id: str | None):
        assert self.pool
        return await self.pool.execute("""
            UPDATE episodes
            SET file_id=$2,
                file_unique_id=$3,
                r2_key=NULL,
                storage_status='pending',
                storage_error=NULL,
                storage_attempts=0
            WHERE id=$1
        """, episode_id, file_id, file_unique_id)

    async def delete_episode(self, episode_id: int):
        assert self.pool
        return await self.pool.execute("DELETE FROM episodes WHERE id=$1", episode_id)
