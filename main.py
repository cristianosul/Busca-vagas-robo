from __future__ import annotations
import asyncio, hashlib, json, os, re, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import sqlite3

load_dotenv()
BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / 'sources.json'
DB_PATH = os.getenv('DATABASE_PATH', str(BASE / 'busca_vagas.db'))
ADMIN_TOKEN = os.getenv('CRAWL_ADMIN_TOKEN', '')
DEFAULT_CONFIG = {
    'scan_interval_minutes': 360, 'max_pages_per_source': 30,
    'request_timeout_seconds': 20,
    'user_agent': 'BuscaVagasBot/0.1 (+respectful job listing indexing)',
    'greenhouse_boards': [], 'lever_sites': [], 'jsonld_seed_urls': [], 'sitemap_urls': []
}


def config() -> dict:
    if CONFIG_PATH.exists():
        try: return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text(encoding='utf-8'))}
        except Exception: pass
    return DEFAULT_CONFIG.copy()


def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL DEFAULT '',
          location TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'Não informado',
          employment_type TEXT NOT NULL DEFAULT '', salary TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '', url TEXT NOT NULL UNIQUE, source TEXT NOT NULL DEFAULT '',
          date_posted TEXT NOT NULL DEFAULT '', valid_through TEXT NOT NULL DEFAULT '',
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
        )''')
        con.execute('CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(active)')


def clean(value: Any) -> str:
    if value is None: return ''
    if isinstance(value, (dict, list)): return ' '.join(str(x) for x in value)[:4000]
    return re.sub(r'\s+', ' ', str(value)).strip()[:4000]


def job_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]

def location_text(value: Any) -> str:
    if isinstance(value, list): return '; '.join(filter(None, (location_text(x) for x in value)))
    if not isinstance(value, dict): return clean(value)
    address = value.get('address', value)
    if isinstance(address, list): return '; '.join(filter(None, (location_text(x) for x in address)))
    if not isinstance(address, dict): return clean(address)
    parts = [address.get(k) for k in ('addressLocality','addressRegion','addressCountry')]
    return ', '.join(clean(p) for p in parts if p)


def infer_mode(text: str, job: dict | None = None) -> str:
    t = (text + ' ' + clean((job or {}).get('jobLocationType'))).lower()
    if 'telecommute' in t or 'remote' in t or 'remoto' in t or 'home office' in t: return 'Remoto'
    if 'hybrid' in t or 'híbrido' in t or 'hibrido' in t: return 'Híbrido'
    if 'onsite' in t or 'on-site' in t or 'presencial' in t: return 'Presencial'
    return 'Não informado'


def extract_jobposting(obj: Any, page_url: str, source: str) -> list[dict]:
    out = []
    if isinstance(obj, list):
        for item in obj: out.extend(extract_jobposting(item, page_url, source))
    elif isinstance(obj, dict):
        typ = obj.get('@type', [])
        if isinstance(typ, str): typ = [typ]
        if 'JobPosting' in typ:
            url = clean(obj.get('url')) or page_url
            title = clean(obj.get('title'))
            if title and url.startswith(('http://','https://')):
                loc = location_text(obj.get('jobLocation')) or location_text(obj.get('applicantLocationRequirements'))
                desc = BeautifulSoup(clean(obj.get('description')), 'html.parser').get_text(' ', strip=True)
                org = obj.get('hiringOrganization') or {}
                if isinstance(org, list): org = org[0] if org else {}
                salary = obj.get('baseSalary') or {}
                salary_text = ''
                if isinstance(salary, dict):
                    val = salary.get('value', {})
                    if isinstance(val, dict): salary_text = clean(val.get('value') or val.get('minValue'))
                    else: salary_text = clean(val)
                    currency = clean(salary.get('currency'))
                    if salary_text and currency: salary_text += ' ' + currency
                out.append({'title':title,'company':clean(org.get('name')),'location':loc,'mode':infer_mode(loc+' '+desc, obj),
                  'employment_type':clean(obj.get('employmentType')),'salary':salary_text,'description':desc,
                  'url':url,'source':source,'date_posted':clean(obj.get('datePosted')),
                  'valid_through':clean(obj.get('validThrough'))})
        for key, value in obj.items():
            if key in ('@graph','mainEntity','itemListElement','hasPart') or isinstance(value, (dict,list)):
                out.extend(extract_jobposting(value, page_url, source))
    return out


def upsert(job: dict) -> bool:
    url = job['url'].split('#')[0]
    now = datetime.now(timezone.utc).isoformat()
    ident = job_id(url)
    with db() as con:
        old = con.execute('SELECT id FROM jobs WHERE url=?', (url,)).fetchone()
        con.execute('''INSERT INTO jobs(id,title,company,location,mode,employment_type,salary,description,url,source,date_posted,valid_through,first_seen,last_seen,active)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(url) DO UPDATE SET
        title=excluded.title,company=excluded.company,location=excluded.location,mode=excluded.mode,
        employment_type=excluded.employment_type,salary=excluded.salary,description=excluded.description,
        source=excluded.source,date_posted=excluded.date_posted,valid_through=excluded.valid_through,last_seen=excluded.last_seen,active=1''',
        (ident,job['title'],job.get('company',''),job.get('location',''),job.get('mode','Não informado'),job.get('employment_type',''),job.get('salary',''),job.get('description',''),url,job.get('source',''),job.get('date_posted',''),job.get('valid_through',''),now,now))
    return old is None


async def fetch_json(client: httpx.AsyncClient, url: str):
    try:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
        return r.json()
    except Exception: return None


async def greenhouse(client, slug: str) -> tuple[int,int]:
    data = await fetch_json(client, f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true')
    if not isinstance(data, dict): return 0,0
    added=total=0
    for item in data.get('jobs', []):
        url=clean(item.get('absolute_url')); title=clean(item.get('title'))
        if not url or not title: continue
        loc=clean((item.get('location') or {}).get('name'))
        desc=BeautifulSoup(clean(item.get('content')), 'html.parser').get_text(' ',strip=True)
        total+=1; added+=upsert({'title':title,'company':slug,'location':loc,'mode':infer_mode(loc+' '+desc), 'description':desc,'url':url,'source':'Greenhouse','date_posted':clean(item.get('updated_at'))})
    return total,added


async def lever(client, slug: str) -> tuple[int,int]:
    data=await fetch_json(client, f'https://api.lever.co/v0/postings/{slug}?mode=json')
    if not isinstance(data,list): return 0,0
    added=total=0
    for item in data:
        url=clean(item.get('hostedUrl') or item.get('applyUrl')); title=clean(item.get('text'))
        if not url or not title: continue
        cats=item.get('categories') or {}; loc=clean(cats.get('location'))
        desc=BeautifulSoup(clean(item.get('descriptionPlain') or item.get('description')), 'html.parser').get_text(' ',strip=True)
        total+=1; added+=upsert({'title':title,'company':slug,'location':loc,'mode':infer_mode(loc+' '+desc), 'employment_type':clean(cats.get('commitment')),'description':desc,'url':url,'source':'Lever','date_posted':clean(item.get('createdAt'))})
    return total,added


async def crawl_page(client, url: str, source: str) -> tuple[int,int]:
    try:
        response=await client.get(url, follow_redirects=True)
        if response.status_code >= 400: return 0,0
        soup=BeautifulSoup(response.text,'html.parser'); found=[]
        for tag in soup.find_all('script',type='application/ld+json'):
            try: found.extend(extract_jobposting(json.loads(tag.string or tag.get_text()), str(response.url), source))
            except Exception: continue
        added=sum(upsert(j) for j in found)
        return len(found),added
    except Exception: return 0,0


async def crawl_sitemap(client, sitemap_url: str, limit: int) -> tuple[int,int]:
    # Sitemap crawling is limited to URLs explicitly supplied by the operator.
    try:
        r=await client.get(sitemap_url, follow_redirects=True)
        if r.status_code >= 400: return 0,0
        soup=BeautifulSoup(r.text,'xml')
        urls=[x.get_text(strip=True) for x in soup.find_all('loc')]
        if soup.find('sitemapindex'):
            total=added=0
            for child in urls[:min(limit,10)]:
                t,a=await crawl_sitemap(client,child,limit); total+=t; added+=a
            return total,added
        total=added=0
        for page in urls[:limit]:
            t,a=await crawl_page(client,page,sitemap_url); total+=t; added+=a
            await asyncio.sleep(0.15)
        return total,added
    except Exception: return 0,0


async def run_crawl() -> dict:
    cfg=config(); timeout=float(cfg.get('request_timeout_seconds',20))
    headers={'User-Agent':cfg.get('user_agent',DEFAULT_CONFIG['user_agent'])}
    stats={'started_at':datetime.now(timezone.utc).isoformat(),'sources_checked':0,'jobs_seen':0,'jobs_added':0,'errors':[]}
    async with httpx.AsyncClient(timeout=timeout,headers=headers) as client:
        for slug in cfg.get('greenhouse_boards',[]):
            try:
                t,a=await greenhouse(client,slug); stats['sources_checked']+=1; stats['jobs_seen']+=t; stats['jobs_added']+=a
            except Exception as e: stats['errors'].append(f'Greenhouse {slug}: {str(e)[:180]}')
        for slug in cfg.get('lever_sites',[]):
            try:
                t,a=await lever(client,slug); stats['sources_checked']+=1; stats['jobs_seen']+=t; stats['jobs_added']+=a
            except Exception as e: stats['errors'].append(f'Lever {slug}: {str(e)[:180]}')
        for url in cfg.get('jsonld_seed_urls',[]):
            t,a=await crawl_page(client,url,'Página pública'); stats['sources_checked']+=1; stats['jobs_seen']+=t; stats['jobs_added']+=a
        for url in cfg.get('sitemap_urls',[]):
            t,a=await crawl_sitemap(client,url,int(cfg.get('max_pages_per_source',30))); stats['sources_checked']+=1; stats['jobs_seen']+=t; stats['jobs_added']+=a
    stats['finished_at']=datetime.now(timezone.utc).isoformat()
    with db() as con:
        con.execute('CREATE TABLE IF NOT EXISTS crawl_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, sources_checked INTEGER, jobs_seen INTEGER, jobs_added INTEGER, errors TEXT)')
        con.execute('INSERT INTO crawl_runs(started_at,finished_at,sources_checked,jobs_seen,jobs_added,errors) VALUES(?,?,?,?,?,?)', (stats['started_at'],stats['finished_at'],stats['sources_checked'],stats['jobs_seen'],stats['jobs_added'],json.dumps(stats['errors'],ensure_ascii=False)))
    return stats


async def scheduler():
    while True:
        try: await run_crawl()
        except Exception: pass
        await asyncio.sleep(max(15,int(config().get('scan_interval_minutes',360))*60))

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task=asyncio.create_task(scheduler())
    yield
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass

app=FastAPI(title='Busca Vagas API',version='0.1.0',lifespan=lifespan)

@app.get('/')
def home(): return FileResponse(BASE/'index.html')

@app.get('/api/health')
def health():
    with db() as con:
        count=con.execute('SELECT COUNT(*) FROM jobs WHERE active=1').fetchone()[0]
        last=con.execute('SELECT finished_at,jobs_seen,jobs_added FROM crawl_runs ORDER BY id DESC LIMIT 1').fetchone() if con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crawl_runs'").fetchone() else None
    return {'status':'ok','active_jobs':count,'last_crawl':dict(last) if last else None}

@app.get('/api/jobs')
def jobs(q: str='', location: str='', mode: str='', state: str='', limit: int=Query(50,ge=1,le=200), offset: int=Query(0,ge=0)):
    terms=['active=1']; args=[]
    if q.strip():
        terms.append('(title LIKE ? OR company LIKE ? OR description LIKE ?)'); like='%'+q.strip()+'%'; args += [like,like,like]
    if location.strip(): terms.append('location LIKE ?'); args.append('%'+location.strip()+'%')
    if mode.strip() and mode!='Todas': terms.append('mode=?'); args.append(mode)
    if state.strip(): terms.append('location LIKE ?'); args.append('%'+state.strip()+'%')
    where=' AND '.join(terms)
    with db() as con:
        total=con.execute(f'SELECT COUNT(*) FROM jobs WHERE {where}',args).fetchone()[0]
        rows=con.execute(f'SELECT id,title,company,location,mode,employment_type,salary,description,url,source,date_posted,valid_through,first_seen FROM jobs WHERE {where} ORDER BY COALESCE(date_posted,first_seen) DESC LIMIT ? OFFSET ?',args+[limit,offset]).fetchall()
    return {'total':total,'items':[dict(x) for x in rows]}

@app.get('/api/stats')
def stats():
    with db() as con:
        total=con.execute('SELECT COUNT(*) FROM jobs WHERE active=1').fetchone()[0]
        sources=con.execute('SELECT COUNT(DISTINCT source) FROM jobs WHERE active=1').fetchone()[0]
        last=con.execute('SELECT finished_at,jobs_seen,jobs_added,sources_checked,errors FROM crawl_runs ORDER BY id DESC LIMIT 1').fetchone() if con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crawl_runs'").fetchone() else None
    return {'active_jobs':total,'sources':sources,'last_crawl':dict(last) if last else None}

@app.post('/api/admin/crawl')
async def manual_crawl(x_crawl_token: str=Header(default='')):
    if ADMIN_TOKEN and x_crawl_token != ADMIN_TOKEN: raise HTTPException(status_code=403,detail='Token inválido')
    return await run_crawl()

app.mount('/static',StaticFiles(directory=BASE),name='static')
