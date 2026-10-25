# Busca Vagas — Robô inicial de coleta

Este pacote transforma o protótipo HTML em um projeto com backend e um primeiro robô de coleta. Ele NÃO afirma coletar todos os sites da internet: cada fonte precisa ser configurada e respeitada conforme suas regras de acesso.

## O que já está implementado

- API em FastAPI: `/api/jobs`, `/api/stats`, `/api/health`.
- Banco SQLite local com deduplicação por URL e atualização de registros existentes.
- Conectores para quadros públicos Greenhouse e Lever, configurados por slug.
- Leitura de páginas/sitemaps configurados que publiquem `JobPosting` em JSON-LD.
- Varredura automática ao iniciar e repetição no intervalo configurado.
- Endpoint de varredura manual: `POST /api/admin/crawl`.
- O HTML original foi mantido como `index.html`; a interface ainda contém anúncios demonstrativos e precisa da etapa seguinte para consumir `/api/jobs` na tela.

## Executar no computador

Requer Python 3.11 ou superior.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp sources.example.json sources.json
cp .env.example .env
uvicorn main:app --reload
```

Abra `http://127.0.0.1:8000`. A documentação interativa da API fica em `http://127.0.0.1:8000/docs`.

## Configurar fontes

Edite `sources.json` (criado copiando `sources.example.json`):

```json
{
  "scan_interval_minutes": 360,
  "max_pages_per_source": 30,
  "request_timeout_seconds": 20,
  "user_agent": "BuscaVagasBot/0.1 (+contact: contato@seudominio.com)",
  "greenhouse_boards": ["slug-do-quadro"],
  "lever_sites": ["slug-do-site"],
  "jsonld_seed_urls": ["https://empresa.example/carreiras/vaga-publica"],
  "sitemap_urls": ["https://empresa.example/sitemap.xml"]
}
```

Os slugs Greenhouse/Lever são identificadores públicos específicos de cada empresa, não nomes genéricos. Use apenas fontes que permitam esse acesso. Sitemaps são limitados ao número configurado de URLs por fonte e só serão úteis quando as páginas expuserem dados estruturados `JobPosting`.

## Testar a API

```bash
curl http://127.0.0.1:8000/api/health
curl 'http://127.0.0.1:8000/api/jobs?q=analista&limit=20'
curl -X POST -H 'x-crawl-token: change-this-token' http://127.0.0.1:8000/api/admin/crawl
```

O endpoint manual aceita requisições sem token apenas se `CRAWL_ADMIN_TOKEN` estiver vazio; em qualquer implantação pública configure um token forte e proteja o endpoint por autenticação. Não publique `.env`, chaves ou tokens.

## Limitações importantes antes de produção

- A lista de fontes começa vazia de propósito; não inventamos vagas nem presumimos que um slug esteja ativo. É preciso cadastrar fontes reais e permitidas.
- A marcação de modalidade é heurística e pode ficar como “Não informado”.
- A validade (`validThrough`) é registrada, mas a desativação automática de vagas vencidas e verificação de links ainda precisam ser implementadas antes de anunciar que anúncios expirados são removidos.
- Para execução contínua, hospede o backend num serviço com processo persistente e armazenamento persistente. O SQLite é adequado para protótipo; para maior volume, migre para PostgreSQL e uma fila de tarefas.
- Respeite `robots.txt`, termos de uso, limites de requisição, privacidade e direitos dos sites. Não contorne login, CAPTCHA ou bloqueios.
