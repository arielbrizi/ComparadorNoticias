"""
Persistencia de artículos y grupos de noticias.
Acumula artículos en DB para consultas históricas (hasta 15 días).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.article_grouper import sort_groups
from app.db import get_conn, query, execute, is_postgres
from app.models import Article, ArticleGroup

logger = logging.getLogger(__name__)

if is_postgres():
    import psycopg2.extras

NEWS_RETENTION_DAYS = 15


# ── Schema ───────────────────────────────────────────────────────────────────


def init_news_tables() -> None:
    """Create articles and article_groups tables if they don't exist."""
    with get_conn() as conn:
        execute(conn, """
            CREATE TABLE IF NOT EXISTS articles (
                id           TEXT PRIMARY KEY,
                source       TEXT NOT NULL,
                source_color TEXT DEFAULT '',
                title        TEXT NOT NULL,
                summary      TEXT DEFAULT '',
                link         TEXT DEFAULT '',
                image        TEXT DEFAULT '',
                category     TEXT DEFAULT 'portada',
                published    TEXT,
                group_id     TEXT,
                fetched_at   TEXT NOT NULL
            )
        """)
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_art_published ON articles (published)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_art_group_id ON articles (group_id)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_art_source ON articles (source)")

        execute(conn, """
            CREATE TABLE IF NOT EXISTS article_groups (
                group_id             TEXT PRIMARY KEY,
                representative_title TEXT NOT NULL,
                representative_image TEXT DEFAULT '',
                category             TEXT DEFAULT 'portada',
                published            TEXT,
                source_count         INTEGER DEFAULT 0,
                created_at           TEXT NOT NULL
            )
        """)
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_grp_published ON article_groups (published)")

    logger.info("News tables ready")


# ── Write ────────────────────────────────────────────────────────────────────


def save_articles_and_groups(
    articles: list[Article],
    groups: list[ArticleGroup],
) -> tuple[int, int]:
    """
    Persist articles and groups to DB via UPSERT.
    Returns (articles_inserted_or_updated, groups_inserted_or_updated).
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    group_id_map: dict[str, str] = {}
    for g in groups:
        for a in g.articles:
            group_id_map[a.id] = g.group_id

    art_seen: dict[str, tuple] = {}
    for a in articles:
        if not a.id or a.id in art_seen:
            continue
        pub_utc = a.published.astimezone(timezone.utc) if a.published else None
        pub_iso = pub_utc.strftime("%Y-%m-%dT%H:%M:%S") if pub_utc else None
        art_seen[a.id] = (
            a.id,
            a.source,
            a.source_color,
            a.title,
            a.summary,
            a.link,
            a.image,
            a.category,
            pub_iso,
            group_id_map.get(a.id, ""),
            now_iso,
        )
    art_rows = list(art_seen.values())

    grp_seen: dict[str, tuple] = {}
    for g in groups:
        if g.group_id in grp_seen:
            continue
        pub_utc = g.published.astimezone(timezone.utc) if g.published else None
        pub_iso = pub_utc.strftime("%Y-%m-%dT%H:%M:%S") if pub_utc else None
        grp_seen[g.group_id] = (
            g.group_id,
            g.representative_title,
            g.representative_image,
            g.category,
            pub_iso,
            g.source_count,
            now_iso,
        )
    grp_rows = list(grp_seen.values())

    art_count = 0
    grp_count = 0

    with get_conn() as conn:
        if is_postgres():
            if art_rows:
                cur = conn.cursor()
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO articles
                       (id, source, source_color, title, summary, link, image,
                        category, published, group_id, fetched_at)
                       VALUES %s
                       ON CONFLICT (id) DO UPDATE SET group_id = EXCLUDED.group_id""",
                    art_rows,
                )
                art_count = cur.rowcount

            if grp_rows:
                cur = conn.cursor()
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO article_groups
                       (group_id, representative_title, representative_image,
                        category, published, source_count, created_at)
                       VALUES %s
                       ON CONFLICT (group_id) DO UPDATE SET
                           representative_title = EXCLUDED.representative_title,
                           representative_image = EXCLUDED.representative_image,
                           source_count = EXCLUDED.source_count,
                           published = EXCLUDED.published""",
                    grp_rows,
                )
                grp_count = cur.rowcount
        else:
            if art_rows:
                cur = conn.executemany(
                    """INSERT INTO articles
                       (id, source, source_color, title, summary, link, image,
                        category, published, group_id, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (id) DO UPDATE SET group_id = excluded.group_id""",
                    art_rows,
                )
                art_count = cur.rowcount

            if grp_rows:
                cur = conn.executemany(
                    """INSERT INTO article_groups
                       (group_id, representative_title, representative_image,
                        category, published, source_count, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (group_id) DO UPDATE SET
                           representative_title = excluded.representative_title,
                           representative_image = excluded.representative_image,
                           source_count = excluded.source_count,
                           published = excluded.published""",
                    grp_rows,
                )
                grp_count = cur.rowcount

    logger.info(
        "News store: %d articles, %d groups persisted",
        art_count, grp_count,
    )
    return art_count, grp_count


# ── Read ─────────────────────────────────────────────────────────────────────


def load_groups_from_db(
    desde: str | None = None,
    hasta: str | None = None,
) -> tuple[list[Article], list[ArticleGroup]]:
    """
    Load articles and reconstruct ArticleGroup objects from DB.
    `desde`/`hasta` are ISO date strings (YYYY-MM-DD).
    Returns (articles, groups).
    """
    where_parts: list[str] = []
    params: list[str] = []

    if desde:
        where_parts.append("a.published >= ?")
        params.append(f"{desde}T00:00:00")
    if hasta:
        where_parts.append("a.published < ?")
        params.append(f"{hasta}T23:59:59")

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    with get_conn() as conn:
        rows = query(conn, f"""
            SELECT a.id, a.source, a.source_color, a.title, a.summary,
                   a.link, a.image, a.category, a.published, a.group_id
            FROM articles a
            {where_sql}
            ORDER BY a.published DESC
        """, params).fetchall()

    all_articles: list[Article] = []
    groups_map: dict[str, list[Article]] = {}

    for r in rows:
        pub = None
        if r["published"]:
            try:
                pub = datetime.fromisoformat(r["published"]).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        article = Article(
            id=r["id"],
            source=r["source"],
            source_color=r["source_color"] or "",
            title=r["title"],
            summary=r["summary"] or "",
            link=r["link"] or "",
            image=r["image"] or "",
            category=r["category"] or "portada",
            published=pub,
        )
        all_articles.append(article)

        gid = r["group_id"]
        if gid:
            groups_map.setdefault(gid, []).append(article)

    grp_where: list[str] = []
    grp_params: list[str] = []
    if desde:
        grp_where.append("published >= ?")
        grp_params.append(f"{desde}T00:00:00")
    if hasta:
        grp_where.append("published < ?")
        grp_params.append(f"{hasta}T23:59:59")

    grp_where_sql = (" WHERE " + " AND ".join(grp_where)) if grp_where else ""

    with get_conn() as conn:
        grp_rows = query(conn, f"""
            SELECT group_id, representative_title, representative_image,
                   category, published, source_count
            FROM article_groups
            {grp_where_sql}
            ORDER BY source_count DESC, published DESC
        """, grp_params).fetchall()

    all_groups: list[ArticleGroup] = []
    for gr in grp_rows:
        gid = gr["group_id"]
        members = groups_map.get(gid, [])
        if not members:
            continue

        pub = None
        if gr["published"]:
            try:
                pub = datetime.fromisoformat(gr["published"]).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        all_groups.append(ArticleGroup(
            group_id=gid,
            representative_title=gr["representative_title"],
            representative_image=gr["representative_image"] or "",
            category=gr["category"] or "portada",
            published=pub,
            articles=sorted(members, key=lambda a: a.source),
        ))

    sort_groups(all_groups)

    logger.info(
        "News store: loaded %d articles, %d groups from DB",
        len(all_articles), len(all_groups),
    )
    return all_articles, all_groups


# ── Text search ──────────────────────────────────────────────────────────────


def text_search_groups(
    search_text: str,
    limit: int = 20,
) -> list[ArticleGroup]:
    """Search articles/groups by title and summary using SQL LIKE.

    Returns reconstructed ArticleGroup objects for matches, sorted by
    source_count desc then published desc.
    """
    tokens = [t.strip() for t in search_text.split() if len(t.strip()) >= 2]
    if not tokens:
        return []

    like_clauses: list[str] = []
    params: list[str] = []
    placeholder = "?" if not is_postgres() else "%s"
    for token in tokens:
        like_clauses.append(
            f"(a.title LIKE {placeholder} OR a.summary LIKE {placeholder})"
        )
        pat = f"%{token}%"
        params.extend([pat, pat])

    where_sql = " AND ".join(like_clauses)

    with get_conn() as conn:
        rows = query(conn, f"""
            SELECT DISTINCT a.group_id
            FROM articles a
            WHERE {where_sql} AND a.group_id IS NOT NULL AND a.group_id != ''
        """, params).fetchall()

    group_ids = [r["group_id"] for r in rows]
    if not group_ids:
        return []

    placeholders = ", ".join([placeholder] * len(group_ids))

    with get_conn() as conn:
        art_rows = query(conn, f"""
            SELECT a.id, a.source, a.source_color, a.title, a.summary,
                   a.link, a.image, a.category, a.published, a.group_id
            FROM articles a
            WHERE a.group_id IN ({placeholders})
            ORDER BY a.published DESC
        """, group_ids).fetchall()

        grp_rows = query(conn, f"""
            SELECT group_id, representative_title, representative_image,
                   category, published, source_count
            FROM article_groups
            WHERE group_id IN ({placeholders})
        """, group_ids).fetchall()

    groups_map: dict[str, list[Article]] = {}
    for r in art_rows:
        pub = None
        if r["published"]:
            try:
                pub = datetime.fromisoformat(r["published"]).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        article = Article(
            id=r["id"],
            source=r["source"],
            source_color=r["source_color"] or "",
            title=r["title"],
            summary=r["summary"] or "",
            link=r["link"] or "",
            image=r["image"] or "",
            category=r["category"] or "portada",
            published=pub,
        )
        gid = r["group_id"]
        if gid:
            groups_map.setdefault(gid, []).append(article)

    grp_meta = {r["group_id"]: r for r in grp_rows}

    result: list[ArticleGroup] = []
    for gid in group_ids:
        members = groups_map.get(gid, [])
        meta = grp_meta.get(gid)
        if not members or not meta:
            continue
        pub = None
        if meta["published"]:
            try:
                pub = datetime.fromisoformat(meta["published"]).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        result.append(ArticleGroup(
            group_id=gid,
            representative_title=meta["representative_title"],
            representative_image=meta["representative_image"] or "",
            category=meta["category"] or "portada",
            published=pub,
            articles=sorted(members, key=lambda a: a.source),
        ))

    sort_groups(result)
    return result[:limit]


# ── Purge ────────────────────────────────────────────────────────────────────


def purge_old_news(days: int = NEWS_RETENTION_DAYS) -> int:
    """Delete articles and groups older than `days` days. Returns rows deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    with get_conn() as conn:
        cur_art = execute(conn, "DELETE FROM articles WHERE published < ?", (cutoff,))
        art_deleted = cur_art.rowcount

        cur_grp = execute(conn, "DELETE FROM article_groups WHERE published < ?", (cutoff,))
        grp_deleted = cur_grp.rowcount

    total = art_deleted + grp_deleted
    if total:
        logger.info(
            "Purge: deleted %d articles + %d groups older than %d days",
            art_deleted, grp_deleted, days,
        )
    return total
