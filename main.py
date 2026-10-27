from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
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
DB_PATH = os.getenv("DATABASE_PATH", str(BASE / "busca_vagas.db"))
ADMIN_TOKEN = os.getenv("CRAWL_ADMIN_TOKEN", "")

DEFAULT_CONFIG = {
    "scan_interval_minutes": 360,
    "max_pages_per_source": 30,
    "request_timeout_seconds": 20,
    "user_agent": "BuscaVagasBot/0.1 (+respectful job listing indexing)",
    "greenhouse_boards": [],
    "lever_sites": [],
    "ashby_boards": [],
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
                active INTEGER NOT NULL DEFAULT 1
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
    t = (text + " " + clean((job or {}).get("jobLocationType"))).lower()

    if any(x in t for x in ("telecommute", "remote", "remoto", "home office")):
        return "Remoto"
    if any(x in t for x in ("hybrid", "híbrido", "hibrido")):
        return "Híbrido"
    if any(x in t for x in ("onsite", "on-site", "presencial")):
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
                loc = location_text(obj.get("jobLocation")) or location_text(
                    obj.get("applicantLocationRequirements")
                )

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


def upsert(job: dict) -> bool:
    url = clean(job.get("url")).split("#")[0]

    if not url.startswith(("http://", "https://")) or not clean(job.get("title")):
        return False

    now = datetime.now(timezone.utc).isoformat()

    with db() as con:
        old = con.execute(
            "SELECT id FROM jobs WHERE url=?", (url,)
        ).fetchone()

        con.execute("""
            INSERT INTO jobs (
                id, title, company, location, mode, employment_type, salary,
                description, url, source, date_posted, valid_through,
                first_seen, last_seen, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                company=excluded.company,
                location=excluded.location,
                mode=excluded.mode,
                employment_type=excluded.employment_type,
                salary=excluded.salary,
                description=excluded.description,
                source=excluded.source,
                date_posted=excluded.date_posted,
                valid_through=excluded.valid_through,
                last_seen=excluded.last_seen,
                active=1
        """, (
            job_id(url),
            clean(job.get("title")),
            clean(job.get("company")),
            clean(job.get("location")),
            clean(job.get("mode")) or "Não informado",
            clean(job.get("employment_type")),
            clean(job.get("salary")),
            clean(job.get("description")),
            url,
            clean(job.get("source")),
            clean(job.get("date_posted")),
            clean(job.get("valid_through")),
            now,
            now,
        ))

    return old is None


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

async def gupy(client, keyword: str, limit: int = 50) -> tuple[int, int]:
    base_url = "https://portal.gupy.io/api/job-search/jobs"
    total = added = 0

    try:
        response = await client.get(
            base_url,
            params={
                "jobName": keyword,
                "limit": limit,
                "offset": 0,
            },
        )
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            return 0, 0

        for item in data.get("data", []):
            title = clean(item.get("name"))
            url = clean(item.get("jobUrl"))

            if not title or not url:
                continue

            company = clean(item.get("careerPageName")) or "Empresa"
            city = clean(item.get("city"))
            state = clean(item.get("state"))
            location = ", ".join(x for x in [city, state] if x)

            description = BeautifulSoup(
                clean(item.get("description")),
                "html.parser",
            ).get_text(" ", strip=True)

            workplace = clean(item.get("workplaceType")).lower()
            if workplace == "on-site":
                mode = "Presencial"
            elif workplace == "hybrid":
                mode = "Híbrido"
            elif workplace == "remote":
                mode = "Remoto"
            else:
                mode = infer_mode(location + " " + description)

            total += 1
            added += upsert({
                "title": title,
                "company": company,
                "location": location,
                "mode": mode,
                "description": description,
                "url": url,
                "source": "Gupy",
                "date_posted": clean(item.get("publishedDate")),
                "valid_through": clean(item.get("applicationDeadline")),
            })

    except Exception as e:
        raise RuntimeError(f"Gupy ({keyword}): {str(e)[:150]}") from e

    return total, added
    
async def run_crawl() -> dict:
    cfg = config()

    stats = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "sources_checked": 0,
        "jobs_seen": 0,
        "jobs_added": 0,
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

        for keyword in [
            "auxiliar",
            "analista",
            "vendedor",
            "operador",
            "estágio",
        ]:
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

    stats["finished_at"] = datetime.now(timezone.utc).isoformat()

    with db() as con:
        con.execute("""
            INSERT INTO crawl_runs (
                started_at, finished_at, sources_checked,
                jobs_seen, jobs_added, errors
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            stats["started_at"],
            stats["finished_at"],
            stats["sources_checked"],
            stats["jobs_seen"],
            stats["jobs_added"],
            json.dumps(stats["errors"], ensure_ascii=False),
        ))

    return stats


async def scheduler():
    while True:
        try:
            await run_crawl()
        except Exception:
            pass

        await asyncio.sleep(
            max(
                15,
                int(config().get("scan_interval_minutes", 360)) * 60,
            )
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(scheduler())

    yield

    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Busca Vagas API",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/")
def home():
    return FileResponse(BASE / "index.html")


@app.get("/api/health")
def health():
    with db() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE active=1"
        ).fetchone()[0]

        last = con.execute("""
            SELECT finished_at, jobs_seen, jobs_added
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
    terms, args = ["active=1"], []

    if q.strip():
        terms.append(
            "(title LIKE ? OR company LIKE ? OR description LIKE ?)"
        )
        like = "%" + q.strip() + "%"
        args.extend([like, like, like])

    if location.strip():
        terms.append("location LIKE ?")
        args.append("%" + location.strip() + "%")

    if mode.strip() and mode != "Todas":
        terms.append("mode=?")
        args.append(mode)

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
                   source, date_posted, valid_through, first_seen
            FROM jobs
            WHERE {where}
            ORDER BY COALESCE(date_posted, first_seen) DESC
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
            "SELECT COUNT(*) FROM jobs WHERE active=1"
        ).fetchone()[0]

        sources = con.execute(
            "SELECT COUNT(DISTINCT source) FROM jobs WHERE active=1"
        ).fetchone()[0]

        last = con.execute("""
            SELECT finished_at, jobs_seen, jobs_added,
                   sources_checked, errors
            FROM crawl_runs
            ORDER BY id DESC LIMIT 1
        """).fetchone()

    return {
        "active_jobs": total,
        "sources": sources,
        "last_crawl": dict(last) if last else None,
    }


@app.post("/api/admin/crawl")
async def manual_crawl(x_crawl_token: str = Header(default="")):
    if ADMIN_TOKEN and x_crawl_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")

    return await run_crawl()


app.mount("/static", StaticFiles(directory=BASE), name="static")
