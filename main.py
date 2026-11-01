from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "sources.json"
FGTAS_DATA_PATH = BASE / "fgtas_sine_caxias_2026-11-03.json"
DB_PATH = os.getenv("DATABASE_PATH", str(BASE / "busca_vagas.db"))
ADMIN_TOKEN = os.getenv("CRAWL_ADMIN_TOKEN", "")
CRAWL_METRICS: dict | None = None

DEFAULT_CONFIG = {
    "scan_interval_minutes": 360,
    "max_pages_per_source": 30,
    "request_timeout_seconds": 20,
    "user_agent": "BuscaVagasBot/0.1 (+respectful job listing indexing)",
    "greenhouse_boards": [],
    "lever_sites": [],
    "ashby_boards": [],
    "smartrecruiters_companies": [],
    "jsonld_seed_urls": [],
    "sitemap_urls": [],
    "additional_sources": [],
}


def config() -> dict:
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                return {**DEFAULT_CONFIG, **saved}
        except (OSError, json.JSONDecodeError):
            pass
    return DEFAULT_CONFIG.copy()


def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'Não informado',
                employment_type TEXT NOT NULL DEFAULT '',
                salary TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL DEFAULT '',
                date_posted TEXT NOT NULL DEFAULT '',
                valid_through TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                link_status TEXT NOT NULL DEFAULT 'pending',
                link_checked_at TEXT NOT NULL DEFAULT '',
                link_http_status INTEGER
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(active)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS crawl_runs (
                id INTEGER PRIMARY KEY,
                started_at TEXT,
                finished_at TEXT,
                sources_checked INTEGER,
                jobs_seen INTEGER,
                jobs_added INTEGER,
                errors TEXT
            )
        """)
        # Migrações compatíveis: preservam todos os registros já existentes.
        run_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(crawl_runs)")
        }
        for column in ("duplicates", "expired_jobs"):
            if column not in run_columns:
                con.execute(
                    f"ALTER TABLE crawl_runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )

        job_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(jobs)")
        }
        if "canonical_key" not in job_columns:
            con.execute(
                "ALTER TABLE jobs ADD COLUMN canonical_key TEXT NOT NULL DEFAULT ''"
            )

        # Estado da validação do link: não desativa vagas automaticamente.
        job_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(jobs)")
        }
        for column, declaration in (
            ("link_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("link_checked_at", "TEXT NOT NULL DEFAULT ''"),
            ("link_http_status", "INTEGER"),
        ):
            if column not in job_columns:
                con.execute(
                    f"ALTER TABLE jobs ADD COLUMN {column} {declaration}"
                )

        con.execute("""
            CREATE TABLE IF NOT EXISTS job_aliases (
                alias_url TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_canonical_key ON jobs(canonical_key)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_date_posted ON jobs(date_posted)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_aliases_job_id ON job_aliases(job_id)"
        )
        existing_jobs = con.execute("""
            SELECT id, title, company, location
            FROM jobs WHERE canonical_key=''
        """).fetchall()
        for row in existing_jobs:
            key = canonical_job_key(dict(row))
            if key:
                con.execute(
                    "UPDATE jobs SET canonical_key=? WHERE id=?",
                    (key, row["id"])
                )


def deactivate_expired_jobs() -> int:
    """Desativa apenas vagas com data de validade explícita já vencida."""
    today = datetime.now().date().isoformat()

    with db() as con:
        cursor = con.execute("""
            UPDATE jobs
            SET active = 0
            WHERE active = 1
              AND TRIM(valid_through) != ''
              AND date(valid_through) IS NOT NULL
              AND date(valid_through) < date(?)
        """, (today,))

        return cursor.rowcount


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return " ".join(str(x) for x in value)[:4000]
    return re.sub(r"\s+", " ", str(value)).strip()[:4000]


def job_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def location_text(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(filter(None, (location_text(x) for x in value)))
    if not isinstance(value, dict):
        return clean(value)

    address = value.get("address", value)

    if isinstance(address, list):
        return "; ".join(filter(None, (location_text(x) for x in address)))

    if not isinstance(address, dict):
        return clean(address)

    parts = [
        address.get("addressLocality"),
        address.get("addressRegion"),
        address.get("addressCountry"),
    ]
    return ", ".join(clean(x) for x in parts if x)


def infer_mode(text: str, job: dict | None = None) -> str:
    job = job or {}

    # Prioriza o campo estruturado do anúncio.
    job_type = clean(job.get("jobLocationType")).lower()
    workplace = clean(
        job.get("workplaceType") or job.get("workplace_type")
    ).lower()

    explicit = job_type + " " + workplace

    if any(x in explicit for x in (
        "telecommute", "remote", "remoto"
    )):
        return "Remoto"

    if any(x in explicit for x in (
        "hybrid", "híbrido", "hibrido"
    )):
        return "Híbrido"

    if any(x in explicit for x in (
        "onsite", "on-site", "on site", "presencial"
    )):
        return "Presencial"

    # Fallback para localização e descrição.
    t = (text or "").lower()

    if any(x in t for x in (
        "home office", "trabalho remoto", "100% remoto",
        "fully remote", "remote position", "remote work",
        "trabalho 100% remoto"
    )):
        return "Remoto"

    if any(x in t for x in (
        "hybrid", "híbrido", "hibrido", "modelo híbrido"
    )):
        return "Híbrido"

    if any(x in t for x in (
        "onsite", "on-site", "on site", "presencial"
    )):
        return "Presencial"

    return "Não informado"
    


def extract_jobposting(obj: Any, page_url: str, source: str) -> list[dict]:
    found = []

    if isinstance(obj, list):
        for item in obj:
            found.extend(extract_jobposting(item, page_url, source))

    elif isinstance(obj, dict):
        types = obj.get("@type", [])

        if isinstance(types, str):
            types = [types]

        if "JobPosting" in types:
            url = clean(obj.get("url")) or page_url
            title = clean(obj.get("title"))

            if title and url.startswith(("http://", "https://")):
                loc = location_text(obj.get("jobLocation"))

                if not loc:
                    loc = location_text(obj.get("address"))


                desc = BeautifulSoup(
                    clean(obj.get("description")), "html.parser"
                ).get_text(" ", strip=True)

                org = obj.get("hiringOrganization") or {}

                if isinstance(org, list):
                    org = org[0] if org else {}

                salary = obj.get("baseSalary") or {}
                salary_text = ""

                if isinstance(salary, dict):
                    value = salary.get("value", {})

                    if isinstance(value, dict):
                        salary_text = clean(
                            value.get("value") or value.get("minValue")
                        )
                    else:
                        salary_text = clean(value)

                    currency = clean(salary.get("currency"))

                    if salary_text and currency:
                        salary_text += " " + currency

                found.append({
                    "title": title,
                    "company": clean(org.get("name")),
                    "location": loc,
                    "mode": infer_mode(loc + " " + desc, obj),
                    "employment_type": clean(obj.get("employmentType")),
                    "salary": salary_text,
                    "description": desc,
                    "url": url,
                    "source": source,
                    "date_posted": clean(obj.get("datePosted")),
                    "valid_through": clean(obj.get("validThrough")),
                })

        for key, value in obj.items():
            if (
                key in ("@graph", "mainEntity", "itemListElement", "hasPart")
                or isinstance(value, (dict, list))
            ):
                found.extend(extract_jobposting(value, page_url, source))

    return found


def normalized_text(value: Any) -> str:
    """Normaliza texto para comparar anúncios sem depender de acentos."""
    value = unicodedata.normalize("NFKD", clean(value).lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\\s+", " ", value).strip()


def valid_http_url(value: Any) -> bool:
    """Valida formato HTTP(S); não garante que a página continue online."""
    try:
        parsed = urlsplit(clean(value))
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def canonical_job_key(job: dict) -> str:
    title = normalized_text(job.get("title"))
    company = normalized_text(job.get("company"))
    location = normalized_text(job.get("location"))

    # Deduplicação cruzada conservadora: não funde empresas desconhecidas
    # nem anúncios sem localidade, para reduzir falsos positivos.
    generic_companies = {"", "empresa", "confidencial", "unknown", "nao informado"}
    if not title or not company or company in generic_companies or not location:
        return ""

    raw = "|".join((title, company, location))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upsert(job: dict) -> bool:
    url = clean(job.get("url")).split("#")[0].strip()

    if not valid_http_url(url) or not clean(job.get("title")):
        return False

    now = datetime.now(timezone.utc).isoformat()
    title = clean(job.get("title"))
    company = clean(job.get("company"))
    location = clean(job.get("location"))
    incoming_valid_through = clean(job.get("valid_through"))
    canonical_key = canonical_job_key(job)

    with db() as con:
        old = con.execute(
            "SELECT id FROM jobs WHERE url=?", (url,)
        ).fetchone()

        if old:
            con.execute("""
                UPDATE jobs
                SET title=?,
                    company=CASE WHEN ?='' THEN company ELSE ? END,
                    location=CASE WHEN ?='' THEN location ELSE ? END,
                    mode=CASE WHEN ?='' THEN mode ELSE ? END,
                    employment_type=CASE WHEN ?='' THEN employment_type ELSE ? END,
                    salary=CASE WHEN ?='' THEN salary ELSE ? END,
                    description=CASE WHEN ?='' THEN description ELSE ? END,
                    source=CASE WHEN ?='' THEN source ELSE ? END,
                    date_posted=CASE WHEN ?='' THEN date_posted ELSE ? END,
                    valid_through=CASE WHEN ?='' THEN valid_through ELSE ? END,
                    canonical_key=CASE WHEN ?='' THEN canonical_key ELSE ? END,
                    last_seen=?,
                    active=CASE
                        WHEN date(COALESCE(NULLIF(?, ''), valid_through)) < date('now')
                             AND COALESCE(NULLIF(?, ''), valid_through) != ''
                        THEN 0 ELSE active END
                WHERE url=?
            """, (
                title, company, company, location, location,
                clean(job.get("mode")), clean(job.get("mode")),
                clean(job.get("employment_type")), clean(job.get("employment_type")),
                clean(job.get("salary")), clean(job.get("salary")),
                clean(job.get("description")), clean(job.get("description")),
                clean(job.get("source")), clean(job.get("source")),
                clean(job.get("date_posted")), clean(job.get("date_posted")),
                incoming_valid_through, incoming_valid_through,
                canonical_key, canonical_key, now,
                incoming_valid_through, incoming_valid_through, url
            ))
            if CRAWL_METRICS is not None:
                CRAWL_METRICS["duplicates"] += 1
            return False

        # Procura anúncio equivalente de outra fonte nos últimos 45 dias.
        if canonical_key:
            match = con.execute("""
                SELECT id FROM jobs
                WHERE canonical_key=?
                  AND first_seen >= datetime('now', '-45 days')
                ORDER BY first_seen DESC
                LIMIT 1
            """, (canonical_key,)).fetchone()

            if match:
                con.execute("""
                    INSERT INTO job_aliases (
                        alias_url, job_id, source, first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(alias_url) DO UPDATE SET
                        last_seen=excluded.last_seen,
                        source=excluded.source
                """, (url, match["id"], clean(job.get("source")), now, now))
                con.execute(
                    "UPDATE jobs SET last_seen=? WHERE id=?",
                    (now, match["id"])
                )
                if CRAWL_METRICS is not None:
                    CRAWL_METRICS["duplicates"] += 1
                return False

        expiry = incoming_valid_through
        expired = bool(
            expiry and con.execute(
                "SELECT date(?) < date('now')", (expiry,)
            ).fetchone()[0]
        )

        con.execute("""
            INSERT INTO jobs (
                id, title, company, location, mode, employment_type, salary,
                description, url, source, date_posted, valid_through,
                first_seen, last_seen, active, canonical_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job_id(url), title, company, location,
            clean(job.get("mode")) or "Não informado",
            clean(job.get("employment_type")),
            clean(job.get("salary")),
            clean(job.get("description")),
            url, clean(job.get("source")),
            clean(job.get("date_posted")), expiry,
            now, now, 0 if expired else 1, canonical_key
        ))

    return True


async def check_application_links(batch_size: int = 200) -> dict:
    """Verifica um lote de links sem desativar vagas por falhas de rede."""
    now = datetime.now(timezone.utc).isoformat()
    cutoff = datetime.now(timezone.utc).timestamp() - (14 * 24 * 60 * 60)
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()

    with db() as con:
        rows = con.execute("""
            SELECT id, url FROM jobs
            WHERE active=1 AND (
                link_status='pending' OR link_checked_at=''
                OR link_checked_at < ?
            )
            ORDER BY CASE WHEN link_status='pending' THEN 0 ELSE 1 END,
                     COALESCE(date_posted, first_seen) DESC
            LIMIT ?
        """, (cutoff_iso, max(1, min(int(batch_size), 500)))).fetchall()

    result = {
        "checked": 0, "valid": 0, "broken": 0,
        "inconclusive": 0, "errors": 0,
    }
    semaphore = asyncio.Semaphore(15)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BuscaVagasLinkCheck/1.0)",
        "Range": "bytes=0-0",
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12.0, connect=8.0),
        follow_redirects=True,
        max_redirects=5,
        headers=headers,
    ) as client:
        async def inspect(row):
            url = clean(row["url"])
            status_code = None
            classification = "inconclusive"
            try:
                async with semaphore:
                    try:
                        response = await client.head(url)
                        status_code = response.status_code
                        # Muitos sites de candidatura bloqueiam HEAD.
                        if status_code in (403, 405, 501):
                            async with client.stream("GET", url) as get_response:
                                status_code = get_response.status_code
                    except (httpx.TimeoutException, httpx.NetworkError):
                        status_code = None

                if status_code is not None and 200 <= status_code < 400:
                    classification = "valid"
                elif status_code in (404, 410):
                    classification = "broken"
            except Exception:
                status_code = None

            checked_at = datetime.now(timezone.utc).isoformat()
            with db() as con:
                con.execute("""
                    UPDATE jobs SET link_status=?, link_checked_at=?,
                                    link_http_status=? WHERE id=?
                """, (classification, checked_at, status_code, row["id"]))
            result["checked"] += 1
            result[classification] += 1

        await asyncio.gather(*(inspect(row) for row in rows))

    return result


async def fetch_json(client: httpx.AsyncClient, url: str):
    try:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


async def greenhouse(client, slug: str) -> tuple[int, int]:
    url = (
        f"https://boards-api.greenhouse.io/v1/boards/"
        f"{slug}/jobs?content=true"
    )
    data = await fetch_json(client, url)

    if not isinstance(data, dict):
        return 0, 0

    total = added = 0

    for item in data.get("jobs", []):
        url = clean(item.get("absolute_url"))
        title = clean(item.ge
