from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping

import psycopg
import tempfile
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
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles

load_dotenv()

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "sources.json"
FGTAS_DATA_PATH = BASE / "fgtas_sine_caxias_2026-11-03.json"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
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
    "gupy_keywords": [
        "auxiliar", "analista", "assistente", "vendedor", "operador",
        "estágio", "jovem aprendiz", "atendente", "administrativo",
        "financeiro", "recursos humanos", "logística", "produção",
        "técnico", "engenheiro", "desenvolvedor", "marketing",
        "motorista", "estoquista", "recepcionista", " serviços gerais",
        "conferente", " ajudante", "consultor", "supervisor",
        "coordenador", "gerente", "mecânico", "eletricista",
        "vigilante", "farmacêutico", "enfermeiro", "vendedor externo",
        "promotor", "auxiliar de limpeza", "auxiliar de cozinha",
        "operador de caixa", "pessoa com deficiência", "aprendiz"
    ],
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


class HybridRow(Mapping):
    """Linha PostgreSQL compatível com acesso SQLite por índice e por nome."""
    def __init__(self, values):
        self._values = values
        self._keys = list(values.keys())

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self._values.values())[key]
        return self._values[key]

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)


def hybrid_row(cursor):
    names = [column.name for column in cursor.description]
    def make(values):
        return HybridRow(dict(zip(names, values)))
    return make


def translate_sql(statement: str) -> str:
    """Adapta placeholders e funções de data do SQLite para PostgreSQL."""
    statement = statement.replace("date('now')", "CURRENT_DATE")
    statement = statement.replace("datetime('now', '-45 days')", "(CURRENT_TIMESTAMP - INTERVAL '45 days')")
    statement = re.sub(r"\bdate\(", "safe_job_date(", statement, flags=re.IGNORECASE)
    # Preserve SQLite-like case-insensitive search behavior in PostgreSQL.
    statement = re.sub(r"\bLIKE\b", "ILIKE", statement, flags=re.IGNORECASE)
    statement = statement.replace("?", "%s")
    return statement


class DatabaseConnection:
    def __init__(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL não configurada no ambiente.")
        self.connection = psycopg.connect(
            DATABASE_URL,
            connect_timeout=20,
            row_factory=hybrid_row,
        )

    def execute(self, statement, params=None):
        return self.connection.execute(
            translate_sql(statement),
            params if params is not None else (),
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.connection.rollback()
        else:
            self.connection.commit()
        self.connection.close()
        return False


def db():
    return DatabaseConnection()


def init_db():        
    with db() as con:
        con.execute("""
            CREATE OR REPLACE FUNCTION safe_job_date(value TEXT)
            RETURNS DATE
            LANGUAGE plpgsql
            IMMUTABLE
            AS $$
            BEGIN
                IF value IS NULL
                   OR LEFT(BTRIM(value), 10) !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                THEN
                    RETURN NULL;
                END IF;

                RETURN LEFT(BTRIM(value), 10)::DATE;

            EXCEPTION
                WHEN OTHERS THEN
                    RETURN NULL;
            END;
            $$
        """)
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
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                started_at TEXT,
                finished_at TEXT,
                sources_checked INTEGER,
                jobs_seen INTEGER,
                jobs_added INTEGER,
                errors TEXT
            )
        """)
        # Migração segura para bases antigas importadas sem IDENTITY/default.
        # Se a coluna já usa IDENTITY ou uma sequence (SERIAL), não mexe nela.
        id_info = con.execute("""
            SELECT is_identity, column_default
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'crawl_runs'
              AND column_name = 'id'
        """).fetchone()
        if id_info and id_info["is_identity"] != "YES" and not id_info["column_default"]:
            con.execute("CREATE SEQUENCE IF NOT EXISTS crawl_runs_legacy_id_seq")
            con.execute("""
                SELECT setval(
                    'crawl_runs_legacy_id_seq',
                    GREATEST(COALESCE((SELECT MAX(id) FROM crawl_runs), 0) + 1, 1),
                    false
                )
            """)
            con.execute("""
                ALTER TABLE crawl_runs
                ALTER COLUMN id SET DEFAULT nextval('crawl_runs_legacy_id_seq')
            """)

        # Migrações compatíveis: preservam todos os registros já existentes.
        run_columns = {
            row["name"] for row in con.execute("""SELECT column_name AS name FROM information_schema.columns
                         WHERE table_schema=current_schema() AND table_name='crawl_runs'""")
        }
        for column in ("duplicates", "expired_jobs"):
            if column not in run_columns:
                con.execute(
                    f"ALTER TABLE crawl_runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )

        job_columns = {
            row["name"] for row in con.execute("""SELECT column_name AS name FROM information_schema.columns
                         WHERE table_schema=current_schema() AND table_name='jobs'""")
        }
        if "canonical_key" not in job_columns:
            con.execute(
                "ALTER TABLE jobs ADD COLUMN canonical_key TEXT NOT NULL DEFAULT ''"
            )

        # Estado da validação do link: não desativa vagas automaticamente.
        job_columns = {
            row["name"] for row in con.execute("""SELECT column_name AS name FROM information_schema.columns
                         WHERE table_schema=current_schema() AND table_name='jobs'""")
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
    return re.sub(r"\s+", " ", value).strip()


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
        title = clean(item.get("title"))

        if not url or not title:
            continue

        loc = clean((item.get("location") or {}).get("name"))
        desc = BeautifulSoup(
            clean(item.get("content")), "html.parser"
        ).get_text(" ", strip=True)

        total += 1

        added += upsert({
            "title": title,
            "company": slug,
            "location": loc,
            "mode": infer_mode(loc + " " + desc),
            "description": desc,
            "url": url,
            "source": "Greenhouse",
            "date_posted": clean(item.get("updated_at")),
        })

    return total, added


async def lever(client, slug: str) -> tuple[int, int]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = await fetch_json(client, url)

    if not isinstance(data, list):
        return 0, 0

    total = added = 0

    for item in data:
        url = clean(item.get("hostedUrl") or item.get("applyUrl"))
        title = clean(item.get("text"))

        if not url or not title:
            continue

        cats = item.get("categories") or {}
        loc = clean(cats.get("location"))

        desc = BeautifulSoup(
            clean(item.get("descriptionPlain") or item.get("description")),
            "html.parser",
        ).get_text(" ", strip=True)

        total += 1

        added += upsert({
            "title": title,
            "company": slug,
            "location": loc,
            "mode": infer_mode(loc + " " + desc),
            "employment_type": clean(cats.get("commitment")),
            "description": desc,
            "url": url,
            "source": "Lever",
            "date_posted": clean(item.get("createdAt")),
        })

    return total, added


async def ashby(client, slug: str) -> tuple[int, int]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    data = await fetch_json(client, url)

    if not isinstance(data, dict):
        return 0, 0

    total = added = 0

    for item in data.get("jobs", []):
        if item.get("isListed") is False:
            continue

        title = clean(item.get("title"))
        url = clean(item.get("jobUrl") or item.get("applyUrl"))

        if not title or not url or not url.startswith(("http://", "https://")):
            continue

        loc = clean(item.get("location"))
        workplace = clean(item.get("workplaceType"))

        if item.get("isRemote") is True:
            workplace += " Remote"

        desc = BeautifulSoup(
            clean(item.get("descriptionPlain") or item.get("descriptionHtml")),
            "html.parser",
        ).get_text(" ", strip=True)

        total += 1

        added += upsert({
            "title": title,
            "company": clean(item.get("department")) or slug,
            "location": loc,
            "mode": infer_mode(loc + " " + workplace),
            "employment_type": clean(item.get("employmentType")),
            "salary": "",
            "description": desc,
            "url": url,
            "source": "Ashby",
            "date_posted": clean(item.get("publishedAt")),
            "valid_through": "",
        })

    return total, added


async def smartrecruiters(
    client: httpx.AsyncClient,
    slug: str,
    max_pages: int = 5,
) -> tuple[int, int]:
    base_url = (
        f"https://api.smartrecruiters.com/v1/"
        f"companies/{slug}/postings"
    )

    total = 0
    added = 0
    limit = 100

    for page in range(max_pages):
        response = await client.get(
            base_url,
            params={
                "limit": limit,
                "offset": page * limit,
            },
        )
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            break

        items = data.get("content", [])

        if not isinstance(items, list) or not items:
            break

        for item in items:
            if not isinstance(item, dict):
                continue

            title = clean(item.get("name"))
            posting_id = clean(item.get("id"))

            if not title or not posting_id:
                continue

            company_data = item.get("company") or {}
            company = (
                clean(company_data.get("name"))
                if isinstance(company_data, dict)
                else ""
            ) or slug

            loc_data = item.get("location") or {}

            if isinstance(loc_data, dict):
                location = ", ".join(
                    clean(loc_data.get(key))
                    for key in ("city", "region", "country")
                    if clean(loc_data.get(key))
                )
                is_remote = loc_data.get("remote") is True
            else:
                location = clean(loc_data)
                is_remote = False

            employment = item.get("typeOfEmployment") or {}

            if isinstance(employment, dict):
                employment_type = clean(
                    employment.get("label")
                )
            else:
                employment_type = clean(employment)

            if is_remote:
                mode = "Remoto"
            else:
                mode = infer_mode(location)

            url = (
                clean(item.get("postingUrl"))
                or clean(item.get("applyUrl"))
                or f"https://jobs.smartrecruiters.com/{slug}/{posting_id}"
            )

            if not url.startswith(("http://", "https://")):
                continue

            total += 1

            added += upsert({
                "title": title,
                "company": company,
                "location": location,
                "mode": mode,
                "employment_type": employment_type,
                "description": "",
                "url": url,
                "source": "SmartRecruiters",
                "date_posted": clean(item.get("releasedDate")),
            })

        if len(items) < limit:
            break

        await asyncio.sleep(0.2)

    return total, added
    

async def crawl_page(client, url: str, source: str) -> tuple[int, int]:
    try:
        response = await client.get(url, follow_redirects=True)

        if response.status_code >= 400:
            return 0, 0

        soup = BeautifulSoup(response.text, "html.parser")
        found = []

        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                found.extend(
                    extract_jobposting(
                        json.loads(tag.string or tag.get_text()),
                        str(response.url),
                        source,
                    )
                )
            except Exception:
                continue

        return len(found), sum(upsert(job) for job in found)

    except Exception:
        return 0, 0


async def crawl_sitemap(
    client, sitemap_url: str, limit: int
) -> tuple[int, int]:
    try:
        response = await client.get(sitemap_url, follow_redirects=True)

        if response.status_code >= 400:
            return 0, 0

        soup = BeautifulSoup(response.text, "xml")
        urls = [x.get_text(strip=True) for x in soup.find_all("loc")]

        if soup.find("sitemapindex"):
            total = added = 0

            for child in urls[:min(limit, 10)]:
                t, a = await crawl_sitemap(client, child, limit)
                total += t
                added += a

            return total, added

        total = added = 0

        for page in urls[:limit]:
            t, a = await crawl_page(client, page, sitemap_url)
            total += t
            added += a
            await asyncio.sleep(0.15)

        return total, added

    except Exception:
        return 0, 0

async def gupy(
    client: httpx.AsyncClient,
    keyword: str,
    limit: int = 50,
) -> tuple[int, int]:
    base_url = "https://portal.gupy.io/api/job-search/jobs"
    total = 0
    added = 0
    seen_urls = set()

    # Busca até 5 páginas de resultados por palavra-chave.
    page_size = max(1, min(limit, 50))
    max_pages = 5

    try:
        for page in range(max_pages):
            response = await client.get(
                base_url,
                params={
                    "jobName": keyword,
                    "limit": page_size,
                    "offset": page * page_size,
                },
            )
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                break

            items = data.get("data", [])

            if not isinstance(items, list) or not items:
                break

            page_valid = 0

            for item in items:
                if not isinstance(item, dict):
                    continue

                title = clean(item.get("name"))
                url = clean(item.get("jobUrl")).split("#")[0]

                if (
                    not title
                    or not url.startswith(("http://", "https://"))
                    or url in seen_urls
                ):
                    continue

                seen_urls.add(url)

                company = (
                    clean(item.get("careerPageName")) or "Empresa"
                )

                city_value = item.get("city")
                state_value = item.get("state")

                city = clean(
                    city_value.get("name")
                    if isinstance(city_value, dict)
                    else city_value
                )
                state = clean(
                    state_value.get("name")
                    if isinstance(state_value, dict)
                    else state_value
                )

                location = ", ".join(
                    value for value in (city, state) if value
                )

                if not location:
                    location = location_text(item.get("address"))

                description = BeautifulSoup(
                    clean(item.get("description")),
                    "html.parser",
                ).get_text(" ", strip=True)

                workplace = clean(
                    item.get("workplaceType")
                ).lower()

                if workplace in (
                    "on-site", "onsite", "on_site", "presencial"
                ):
                    mode = "Presencial"
                elif workplace in ("hybrid", "híbrido", "hibrido"):
                    mode = "Híbrido"
                elif workplace in (
                    "remote", "remoto", "telecommute"
                ):
                    mode = "Remoto"
                else:
                    mode = infer_mode(
                        location + " " + description
                    )

                total += 1
                page_valid += 1

                added += upsert({
                    "title": title,
                    "company": company,
                    "location": location,
                    "mode": mode,
                    "description": description,
                    "url": url,
                    "source": "Gupy",
                    "date_posted": clean(
                        item.get("publishedDate")
                    ),
                    "valid_through": clean(
                        item.get("applicationDeadline")
                    ),
                })

            # Evita novas requisições quando a página não
            # contém anúncios válidos.
            if len(items) < page_size:
                break

            await asyncio.sleep(0.2)

    except Exception as e:
        raise RuntimeError(
            f"Gupy ({keyword}): {str(e)[:150]}"
        ) from e

    return total, added

def import_fgtas_jobs() -> dict:
    """Importa arquivos locais de vagas FGTAS/SINE de vários municípios.

    Cada arquivo fgtas_sine_*.json deve conter um objeto com "items" (lista
    de vagas). A cidade pode ser informada no campo "city" do arquivo ou no
    campo "location" de cada vaga. Nenhuma vaga/cidade é inventada aqui.
    """
    result = {"jobs_seen": 0, "jobs_added": 0, "errors": []}

    paths = []
    if FGTAS_DATA_PATH.exists():
        paths.append(FGTAS_DATA_PATH)
    paths.extend(path for path in BASE.glob("fgtas_sine_*.json") if path not in paths)

    if not paths:
        result["errors"].append(
            "Nenhum arquivo fgtas_sine_*.json encontrado para importar."
        )
        return result

    for data_path in paths:
        try:
            payload = json.loads(data_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Formato JSON inválido.")
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise ValueError("A lista de vagas é inválida.")

            default_city = clean(payload.get("city"))
            default_state = clean(payload.get("state")) or "RS"
            default_location = ", ".join(
                part for part in (default_city, default_state) if part
            )
            default_source = clean(payload.get("source")) or "FGTAS/SINE"

            for item in items:
                if not isinstance(item, dict):
                    continue
                code = clean(item.get("source_id"))
                title = clean(item.get("title"))
                if not code or not title:
                    continue

                details = [f"Código da vaga FGTAS/SINE: {code}"]
                description = clean(item.get("description"))
                instructions = clean(item.get("application_instructions"))
                if description:
                    details.append(description)
                if instructions:
                    details.append("COMO SE CANDIDATAR: " + instructions)
                if item.get("pcd_exclusive"):
                    details.append("Vaga exclusiva para pessoa com deficiência (PCD).")
                if item.get("no_experience_required"):
                    details.append("Não exige experiência, conforme publicação.")

                url = clean(item.get("url")) or (
                    "https://www.fgtas.rs.gov.br/?vaga=" + code
                )
                if not valid_http_url(url):
                    result["errors"].append(
                        f"{data_path.name}: URL inválida para vaga {code}."
                    )
                    continue

                result["jobs_seen"] += 1
                result["jobs_added"] += upsert({
                    "title": title,
                    "company": clean(item.get("company")) or "Confidencial",
                    "location": clean(item.get("location")) or default_location or "Rio Grande do Sul",
                    "mode": clean(item.get("mode")) or "Presencial",
                    "employment_type": clean(item.get("employment_type")),
                    "salary": clean(item.get("salary")),
                    "description": " | ".join(details),
                    "url": url,
                    "source": clean(item.get("source")) or default_source,
                    "date_posted": clean(item.get("publication_date")) or clean(payload.get("publication_date")),
                    "valid_through": clean(item.get("valid_through")),
                })
        except Exception as exc:
            result["errors"].append(
                f"{data_path.name}: {str(exc)[:180]}"
            )

    return result

async def run_crawl() -> dict:
    global CRAWL_METRICS
    CRAWL_METRICS = {"duplicates": 0}
    cfg = config()

    stats = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "sources_checked": 0,
        "jobs_seen": 0,
        "jobs_added": 0,
        "duplicates": 0,
        "expired_jobs": 0,
        "errors": [],
    }

    headers = {
        "User-Agent": cfg.get(
            "user_agent", DEFAULT_CONFIG["user_agent"]
        )
    }

    async with httpx.AsyncClient(
        timeout=float(cfg.get("request_timeout_seconds", 20)),
        headers=headers,
    ) as client:

        for slug in cfg.get("greenhouse_boards", []):
            try:
                t, a = await greenhouse(client, slug)
                stats["sources_checked"] += 1
                stats["jobs_seen"] += t
                stats["jobs_added"] += a

                if t == 0:
                    stats["errors"].append(
                        f"Greenhouse {slug}: nenhum registro retornado."
                    )

            except Exception as e:
                stats["errors"].append(
                    f"Greenhouse {slug}: {str(e)[:180]}"
                )

        for slug in cfg.get("lever_sites", []):
            try:
                t, a = await lever(client, slug)
                stats["sources_checked"] += 1
                stats["jobs_seen"] += t
                stats["jobs_added"] += a

                if t == 0:
                    stats["errors"].append(
                        f"Lever {slug}: nenhum registro retornado."
                    )

            except Exception as e:
                stats["errors"].append(
                    f"Lever {slug}: {str(e)[:180]}"
                )

        for slug in cfg.get("ashby_boards", []):
            try:
                t, a = await ashby(client, slug)
                stats["sources_checked"] += 1
                stats["jobs_seen"] += t
                stats["jobs_added"] += a

                if t == 0:
                    stats["errors"].append(
                        f"Ashby {slug}: nenhum registro retornado; "
                        "confira o identificador do quadro."
                    )

            except Exception as e:
                stats["errors"].append(
                    f"Ashby {slug}: {str(e)[:180]}"
                )
        
        for slug in cfg.get("smartrecruiters_companies", []):
            try:
                t, a = await smartrecruiters(
                    client,
                    slug,
                    max_pages=min(
                        5,
                        int(cfg.get("max_pages_per_source", 30))
                    ),
                )

                stats["sources_checked"] += 1
                stats["jobs_seen"] += t
                stats["jobs_added"] += a

                if t == 0:
                    stats["errors"].append(
                        f"SmartRecruiters {slug}: nenhum registro retornado; "
                        "confira o identificador da empresa."
                    )

            except Exception as e:
                stats["errors"].append(
                    f"SmartRecruiters {slug}: {str(e)[:180]}"
                )

        gupy_keywords = cfg.get("gupy_keywords", DEFAULT_CONFIG["gupy_keywords"])
        if not isinstance(gupy_keywords, list):
            gupy_keywords = DEFAULT_CONFIG["gupy_keywords"]

        for keyword in dict.fromkeys(
            clean(term) for term in gupy_keywords if clean(term)
        ):
            try:
                t, a = await gupy(client, keyword, 50)
                stats["sources_checked"] += 1
                stats["jobs_seen"] += t
                stats["jobs_added"] += a

            except Exception as e:
                stats["errors"].append(str(e)[:180])
        
        for url in cfg.get("jsonld_seed_urls", []):
            try:
                t, a = await crawl_page(client, url, "Página pública")
                stats["sources_checked"] += 1
                stats["jobs_seen"] += t
                stats["jobs_added"] += a

            except Exception as e:
                stats["errors"].append(
                    f"Página pública: {str(e)[:180]}"
                )

        for item in cfg.get("additional_sources", []):
            if isinstance(item, str):
                url, name = item, "Página pública"

            elif isinstance(item, dict):
                url = clean(item.get("url"))
                name = clean(
                    item.get("name") or item.get("source")
                ) or "Página pública"

            else:
                stats["errors"].append(
                    "Fonte adicional ignorada: formato inválido"
                )
                continue

            if not url.startswith(("http://", "https://")):
                stats["errors"].append(
                    "Fonte adicional inválida: URL ausente ou inválida."
                )
                continue

            try:
                t, a = await crawl_page(client, url, name)
                stats["sources_checked"] += 1
                stats["jobs_seen"] += t
                stats["jobs_added"] += a

            except Exception as e:
                stats["errors"].append(
                    f"Fonte adicional {name}: {str(e)[:180]}"
                )

        for url in cfg.get("sitemap_urls", []):
            try:
                t, a = await crawl_sitemap(
                    client,
                    url,
                    int(cfg.get("max_pages_per_source", 30)),
                )
                stats["sources_checked"] += 1
                stats["jobs_seen"] += t
                stats["jobs_added"] += a

            except Exception as e:
                stats["errors"].append(
                    f"Sitemap: {str(e)[:180]}"
                )

        expired = deactivate_expired_jobs()

    # Valida apenas um lote por execução para não sobrecarregar as fontes.
    # Respostas 404/410 são sinalizadas, mas nunca removem a vaga automaticamente.
    try:
        stats["link_check"] = await check_application_links(200)
    except Exception as exc:
        stats["link_check"] = {"checked": 0, "errors": 1}
        stats["errors"].append(
            f"Validação de links: {str(exc)[:160]}"
        )

    stats["expired_jobs"] = expired
    stats["duplicates"] = CRAWL_METRICS["duplicates"]
    CRAWL_METRICS = None
    stats["finished_at"] = datetime.now(timezone.utc).isoformat()

    with db() as con:
        con.execute("""
            INSERT INTO crawl_runs (
                started_at, finished_at, sources_checked,
                jobs_seen, jobs_added, errors, duplicates, expired_jobs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stats["started_at"],
            stats["finished_at"],
            stats["sources_checked"],
            stats["jobs_seen"],
            stats["jobs_added"],
            json.dumps(stats["errors"], ensure_ascii=False),
            stats["duplicates"],
            stats["expired_jobs"],
        ))

    return stats


async def scheduler():
    import logging

    logger = logging.getLogger("uvicorn.error")

    while True:
        try:
            logger.info("ROBÔ: iniciando coleta automática.")
            result = await run_crawl()

            logger.info(
                "ROBÔ: coleta concluída. "
                f"Fontes verificadas: {result['sources_checked']}; "
                f"Vagas encontradas: {result['jobs_seen']}; "
                f"Novas vagas: {result['jobs_added']}; "
                f"Duplicadas: {result['duplicates']}; "
                f"Expiradas desativadas: {result['expired_jobs']}; "
                f"Erros: {len(result['errors'])}"
            )

            for error in result["errors"]:
                logger.warning("ROBÔ - FONTE: %s", error)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("ROBÔ: falha na coleta automática.")

        await asyncio.sleep(
            max(
                15,
                int(config().get("scan_interval_minutes", 360)) * 60,
            )
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    imported = import_fgtas_jobs()

    import logging
    logger = logging.getLogger("uvicorn.error")

    logger.info(
        "FGTAS/SINE: registros lidos=%s; "
        "novas vagas=%s; erros=%s",
        imported["jobs_seen"],
        imported["jobs_added"],
        len(imported["errors"])
    )

    for error in imported["errors"]:
        logger.warning("FGTAS/SINE: %s", error)

    task = asyncio.create_task(scheduler())

    yield

    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
        

app = FastAPI(
    title="Busca Vagas API",
    version="0.4.0",
    lifespan=lifespan,
)


@app.get("/")
def home():
    return FileResponse(BASE / "index.html")


@app.get("/api/health")
def health():
    with db() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE active=1 AND (valid_through IS NULL OR TRIM(valid_through) = '' OR date(valid_through) >= date('now'))"
        ).fetchone()[0]

        last = con.execute("""
            SELECT finished_at, jobs_seen, jobs_added,
                   duplicates, expired_jobs, sources_checked, errors
            FROM crawl_runs
            ORDER BY id DESC LIMIT 1
        """).fetchone()

    return {
        "status": "ok",
        "active_jobs": count,
        "last_crawl": dict(last) if last else None,
    }


@app.get("/api/jobs")
def jobs(
    q: str = "",
    location: str = "",
    mode: str = "",
    state: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    terms, args = [
        "active=1",
        "(valid_through IS NULL OR TRIM(valid_through) = '' OR date(valid_through) >= date('now'))",
    ], []

    if q.strip():
        terms.append(
            "(title LIKE ? OR company LIKE ? OR description LIKE ?)"
        )
        like = "%" + q.strip() + "%"
        args.extend([like, like, like])

    
    if location.strip().lower() in ("brasil", "brazil"):
        brazil_patterns = [
            "%Brasil%", "%Brazil%",
            "%São Paulo%", "%Rio de Janeiro%",
            "%Belo Horizonte%", "%Brasília%",
            "%Curitiba%", "%Porto Alegre%",
            "%Florianópolis%", "%Caxias do Sul%",
            "%Salvador%", "%Recife%",
            "%Fortaleza%", "%Goiânia%",
            "%Manaus%", "%Campinas%",
            "%Minas Gerais%", "%Rio Grande do Sul%",
            "%Santa Catarina%", "%Paraná%",
            "%Bahia%", "%Pernambuco%",
            "%Ceará%", "%Espírito Santo%",
            "%Goiás%", "%Amazonas%",
            "%Mato Grosso%", "%Mato Grosso do Sul%",
            "%Pará%", "%Paraíba%", "%Sergipe%",
            "%Alagoas%", "%Amapá%", "%Acre%",
            "%Rondônia%", "%Roraima%", "%Tocantins%",
            "%Maranhão%", "%Piauí%",
            "%Rio Grande do Norte%"
        ]

        location_conditions = " OR ".join(
            ["location LIKE ?" for _ in brazil_patterns]
        )

        terms.append(
            "(" + location_conditions +
            " OR (mode = 'Remoto' AND ("
            "description LIKE '%Brazil%' OR "
            "description LIKE '%Brasil%' OR "
            "description LIKE '%worldwide%' OR "
            "description LIKE '%anywhere in the world%'"
            ")))"
        )
        args.extend(brazil_patterns)

    else:
        if location.strip():
            terms.append("location LIKE ?")
            args.append("%" + location.strip() + "%")

    # Modalidade e estado devem ser aplicados também quando a localização
    # selecionada for Brasil/Brazil.
    if mode.strip() and mode.strip().lower() not in ("todas", "todos"):
        if mode.strip().lower() in ("hibrido", "híbrido"):
            terms.append("LOWER(REPLACE(mode, 'í', 'i')) = ?")
            args.append("hibrido")
        else:
            terms.append("LOWER(mode)=LOWER(?)")
            args.append(mode.strip())

    if state.strip():
        terms.append("location LIKE ?")
        args.append("%" + state.strip() + "%")

    where = " AND ".join(terms)


    with db() as con:
        total = con.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {where}", args
        ).fetchone()[0]

        rows = con.execute(f"""
            SELECT id, title, company, location, mode,
                   employment_type, salary, description, url,
                   source, date_posted, valid_through, first_seen,
                   COALESCE(NULLIF(link_status, ''), 'pending') AS link_status
            FROM jobs
            WHERE {where}
            ORDER BY
                CASE WHEN TRIM(COALESCE(date_posted, '')) = '' THEN 1 ELSE 0 END,
                date(date_posted) DESC,
                CASE COALESCE(NULLIF(link_status, ''), 'pending')
                    WHEN 'valid' THEN 0
                    WHEN 'inconclusive' THEN 1
                    WHEN 'pending' THEN 2
                    WHEN 'broken' THEN 3
                    ELSE 2
                END,
                first_seen DESC
            LIMIT ? OFFSET ?
        """, args + [limit, offset]).fetchall()

    return {
        "total": total,
        "items": [dict(row) for row in rows],
    }


@app.get("/api/stats")
def stats():
    with db() as con:
        total = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE active=1 AND (valid_through IS NULL OR TRIM(valid_through) = '' OR date(valid_through) >= date('now'))"
        ).fetchone()[0]

        sources = con.execute(
            """SELECT COUNT(DISTINCT source) FROM jobs
               WHERE active=1
                 AND (valid_through IS NULL OR TRIM(valid_through)=''
                      OR date(valid_through) >= date('now'))"""
        ).fetchone()[0]

        link_rows = con.execute("""
            SELECT link_status, COUNT(*) AS amount FROM jobs
            WHERE active=1 AND (valid_through IS NULL OR TRIM(valid_through)=''
                                OR date(valid_through) >= date('now'))
            GROUP BY link_status
        """).fetchall()
        link_checks = {row["link_status"]: row["amount"] for row in link_rows}

        last = con.execute("""
            SELECT finished_at, jobs_seen, jobs_added,
                   sources_checked, duplicates, expired_jobs, errors
            FROM crawl_runs
            ORDER BY id DESC LIMIT 1
        """).fetchone()

    return {
        "active_jobs": total,
        "sources": sources,
        "application_links": {
            "pending": link_checks.get("pending", 0),
            "valid": link_checks.get("valid", 0),
            "broken": link_checks.get("broken", 0),
            "inconclusive": link_checks.get("inconclusive", 0),
        },
        "last_crawl": dict(last) if last else None,
    }


@app.get("/admin/backup", response_class=HTMLResponse)
def backup_page():
    return HTMLResponse("""<!doctype html>
<html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backup - Busca Vagas</title>
<body style="font-family:Arial,sans-serif;background:#101522;color:#fff;padding:24px;max-width:520px;margin:30px auto">
<h2>Backup do Busca Vagas</h2>
<p>Digite o valor da variável CRAWL_ADMIN_TOKEN configurada no Render. O token será enviado somente no cabeçalho da solicitação.</p>
<input id="token" type="password" autocomplete="current-password" placeholder="Token de segurança" style="box-sizing:border-box;width:100%;padding:14px;border-radius:8px;border:1px solid #555;margin:12px 0;background:#202a3b;color:white">
<button id="download" style="padding:14px 18px;border:0;border-radius:8px;background:#8b5cf6;color:white;font-weight:bold">Baixar backup completo</button>
<p id="status" role="status"></p>
<script>
document.getElementById('download').addEventListener('click', async () => {
  const token = document.getElementById('token').value;
  const status = document.getElementById('status');
  if (!token) { status.textContent = 'Digite o token.'; return; }
  status.textContent = 'Preparando o backup...';
  try {
    const response = await fetch('/api/admin/backup', {method:'POST', headers:{'X-Crawl-Token':token}});
    if (!response.ok) { status.textContent = response.status === 403 ? 'Token inválido.' : 'Não foi possível gerar o backup (HTTP ' + response.status + ').'; return; }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'busca_vagas_backup.sqlite3';
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    status.textContent = 'Backup gerado. Confira a pasta Downloads do celular.';
    document.getElementById('token').value = '';
  } catch (e) { status.textContent = 'Falha de conexão. Tente novamente.'; }
});
</script></body></html>""")


@app.post("/api/admin/backup")
def download_database_backup(x_crawl_token: str = Header(default="")):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="CRAWL_ADMIN_TOKEN não configurado")
    if x_crawl_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")
    backup_file = tempfile.NamedTemporaryFile(prefix="busca_vagas_", suffix=".sqlite3", delete=False)
    backup_path = backup_file.name
    backup_file.close()
    try:
        columns = {
            "jobs": ["id", "title", "company", "location", "mode", "employment_type", "salary", "description", "url", "source", "date_posted", "valid_through", "first_seen", "last_seen", "active", "link_status", "link_checked_at", "link_http_status", "canonical_key"],
            "job_aliases": ["alias_url", "job_id", "source", "first_seen", "last_seen"],
            "crawl_runs": ["id", "started_at", "finished_at", "sources_checked", "jobs_seen", "jobs_added", "errors", "duplicates", "expired_jobs"],
        }
        with db() as source, sqlite3.connect(backup_path) as destination:
            destination.execute("""CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'Não informado', employment_type TEXT NOT NULL DEFAULT '', salary TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', url TEXT NOT NULL UNIQUE, source TEXT NOT NULL DEFAULT '', date_posted TEXT NOT NULL DEFAULT '', valid_through TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, link_status TEXT NOT NULL DEFAULT 'pending', link_checked_at TEXT NOT NULL DEFAULT '', link_http_status INTEGER, canonical_key TEXT NOT NULL DEFAULT '')""")
            destination.execute("""CREATE TABLE job_aliases (alias_url TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)""")
            destination.execute("""CREATE TABLE crawl_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, sources_checked INTEGER, jobs_seen INTEGER, jobs_added INTEGER, errors TEXT, duplicates INTEGER NOT NULL DEFAULT 0, expired_jobs INTEGER NOT NULL DEFAULT 0)""")
            for table, names in columns.items():
                rows = source.execute(f"SELECT {', '.join(names)} FROM {table}").fetchall()
                placeholders = ",".join(["?"] * len(names))
                destination.executemany(
                    f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})",
                    [tuple(row[name] for name in names) for row in rows],
                )
            destination.commit()
    except Exception:
        try:
            os.unlink(backup_path)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail="Falha ao criar backup do banco PostgreSQL")

    return FileResponse(
        backup_path,
        media_type="application/vnd.sqlite3",
        filename="busca_vagas_backup.sqlite3",
        background=BackgroundTask(os.unlink, backup_path),
    )


@app.post("/api/admin/crawl")
async def manual_crawl(x_crawl_token: str = Header(default="")):
    if ADMIN_TOKEN and x_crawl_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")

    return await run_crawl()


app.mount("/static", StaticFiles(directory=BASE), name="static")
