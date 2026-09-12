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
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
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

    async def movie(self, movie_id: int):
        assert self.pool
        return await self.pool.fetchrow("SELECT * FROM movies WHERE id=$1", movie_id)

    async def search_movies(self, query: str, limit=20):
        assert self.pool
        return await self.pool.fetch(
            "SELECT * FROM movies WHERE title ILIKE $1 ORDER BY title LIMIT $2", f"%{query.strip()}%", limit
        )

    async def rename_movie(self, movie_id: int, title: str):
        assert self.pool
        return await self.pool.execute("UPDATE movies SET title=$2 WHERE id=$1", movie_id, title.strip())

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
            SELECT e.*, m.title AS movie_title, m.emoji AS movie_emoji
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
            WHERE e.file_id IS NOT NULL ORDER BY e.created_at DESC LIMIT $1
        """, limit)

    async def set_episode_number(self, episode_id: int, number: int):
        assert self.pool
        return await self.pool.execute("UPDATE episodes SET episode_number=$2 WHERE id=$1", episode_id, number)

    async def set_episode_video(self, episode_id: int, file_id: str, file_unique_id: str | None):
        assert self.pool
        return await self.pool.execute("UPDATE episodes SET file_id=$2,file_unique_id=$3 WHERE id=$1", episode_id, file_id, file_unique_id)

    async def delete_episode(self, episode_id: int):
        assert self.pool
        return await self.pool.execute("DELETE FROM episodes WHERE id=$1", episode_id)

