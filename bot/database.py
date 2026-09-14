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
            CREATE INDEX IF NOT EXISTS episodes_movie_number_idx ON episodes(movie_id, episode_number);
            CREATE TABLE IF NOT EXISTS watch_progress (
                user_id BIGINT PRIMARY KEY,
                episode_id BIGINT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS watch_progress_updated_idx ON watch_progress(updated_at DESC);
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
            CREATE TABLE IF NOT EXISTS vip_users (
                user_id BIGINT PRIMARY KEY,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS vip_users_expires_idx ON vip_users(expires_at);
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
            SELECT * FROM movies WHERE is_vip=FALSE
            ORDER BY created_at DESC, id DESC OFFSET $1 LIMIT $2
        """, offset, limit)

    async def public_movie_count(self):
        assert self.pool
        return await self.pool.fetchval("SELECT COUNT(*) FROM movies WHERE is_vip=FALSE")

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
        return await self.pool.fetchrow("SELECT * FROM movies WHERE id=$1", movie_id)

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
                caption=COALESCE(EXCLUDED.caption, episodes.caption)
            RETURNING *
        """, movie_id, number, file_id, file_unique_id, caption)

    async def episodes(self, movie_id: int, offset=0, limit=12):
        assert self.pool
        return await self.pool.fetch("""
            SELECT * FROM episodes WHERE movie_id=$1 AND file_id IS NOT NULL
            ORDER BY episode_number OFFSET $2 LIMIT $3
        """, movie_id, offset, limit)

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

    async def adjacent_episode(self, movie_id: int, number: int, direction: str):
        assert self.pool
        if direction == "prev":
            return await self.pool.fetchrow("SELECT id FROM episodes WHERE movie_id=$1 AND episode_number<$2 AND file_id IS NOT NULL ORDER BY episode_number DESC LIMIT 1", movie_id, number)
        return await self.pool.fetchrow("SELECT id FROM episodes WHERE movie_id=$1 AND episode_number>$2 AND file_id IS NOT NULL ORDER BY episode_number LIMIT 1", movie_id, number)

    async def latest_episodes(self, limit=12):
        assert self.pool
        return await self.pool.fetch("""
            SELECT e.*, m.title AS movie_title, m.emoji AS movie_emoji
            FROM episodes e JOIN movies m ON m.id=e.movie_id
            WHERE e.file_id IS NOT NULL AND m.is_vip=FALSE
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

    async def save_watch_progress(self, user_id: int, episode_id: int):
        assert self.pool
        return await self.pool.execute("""
            INSERT INTO watch_progress(user_id, episode_id, updated_at)
            VALUES($1, $2, NOW())
            ON CONFLICT(user_id) DO UPDATE SET
                episode_id=EXCLUDED.episode_id,
                updated_at=NOW()
        """, user_id, episode_id)

    async def watch_progress(self, user_id: int):
        assert self.pool
        return await self.pool.fetchrow("""
            SELECT e.*, m.title AS movie_title, m.emoji AS movie_emoji,
                   m.is_vip AS movie_is_vip
            FROM watch_progress w
            JOIN episodes e ON e.id=w.episode_id
            JOIN movies m ON m.id=e.movie_id
            WHERE w.user_id=$1 AND e.file_id IS NOT NULL
        """, user_id)

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

    async def broadcast_user_ids(self, admin_id: int):
        assert self.pool
        return await self.pool.fetch(
            "SELECT user_id FROM bot_users WHERE user_id<>$1 AND is_active=TRUE ORDER BY user_id",
            admin_id,
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

    async def set_episode_number(self, episode_id: int, number: int):
        assert self.pool
        return await self.pool.execute("UPDATE episodes SET episode_number=$2 WHERE id=$1", episode_id, number)

    async def set_episode_video(self, episode_id: int, file_id: str, file_unique_id: str | None):
        assert self.pool
        return await self.pool.execute("UPDATE episodes SET file_id=$2,file_unique_id=$3 WHERE id=$1", episode_id, file_id, file_unique_id)

    async def delete_episode(self, episode_id: int):
        assert self.pool
        return await self.pool.execute("DELETE FROM episodes WHERE id=$1", episode_id)
