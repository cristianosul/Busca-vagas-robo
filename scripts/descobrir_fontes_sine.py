import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

KEYWORDS = re.compile(
    r"(vaga|emprego|sine|boletim|painel|intermedia|oportunidade|pdf|relatorio|balcao)",
    re.IGNORECASE,
)

PORTAIS_NACIONAIS_ESTADUAIS = [
    ("FGTAS - Home", "https://fgtas.rs.gov.br/"),
    ("FGTAS - Agencias SINE", "https://fgtas.rs.gov.br/agencias-fgtas-sine"),
    ("FGTAS - Busca Vagas", "https://fgtas.rs.gov.br/busca?q=vagas"),
    ("Emprega Brasil - Home", "https://empregabrasil.mte.gov.br/"),
    ("Servicos MTE", "https://servicos.mte.gov.br/"),
    ("Gov.br - Trabalhador / SINE", "https://www.gov.br/trabalho-e-emprego/pt-br/servicos/trabalhador/mais-empregos"),
    ("Dados Abertos - Busca SINE", "https://dados.gov.br/dados/api/publico/conjuntos-dados?isPrivado=false&nomeConjuntoDados=sine"),
]


def fetch(url: str, timeout: int = 20):
    return cffi_requests.get(
        url,
        impersonate="chrome",
        headers=HEADERS,
        timeout=timeout,
        allow_redirects=True,
    )


def inspecionar_fgtas() -> None:
    print("=== 1. VARREDURA DE LINKS DE VAGAS / BOLETINS NA FGTAS ===")
    seeds = [
        "https://fgtas.rs.gov.br/",
        "https://fgtas.rs.gov.br/agencias-fgtas-sine",
    ]
    encontrados = {}

    for seed in seeds:
        try:
            r = fetch(seed)
            print(f"[SEED] {seed} -> HTTP {r.status_code} ({len(r.content)}B)")
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(seed, a["href"].strip())
                texto = " ".join(a.get_text(" ", strip=True).split())
                if KEYWORDS.search(href) or KEYWORDS.search(texto):
                    encontrados[href] = texto[:90] or "(sem texto)"
        except Exception as exc:
            print(f"[ERRO SEED] {seed} -> {type(exc).__name__}: {exc}")

    print(f"\nTotal de links relevantes encontrados na FGTAS: {len(encontrados)}")
    for href, texto in sorted(encontrados.items()):
        print(f"  - {texto} -> {href}")


def testar_portais_centralizados() -> None:
    print("\n=== 2. TESTE DE PORTAIS CENTRALIZADOS (MTE / EMPREGA BRASIL / DADOS ABERTOS) ===")
    for label, url in PORTAIS_NACIONAIS_ESTADUAIS:
        try:
            r = fetch(url)
            ctype = r.headers.get("content-type", "").split(";")[0]
            snippet = " ".join(r.text[:140].split())
            print(f"[OK] {label} | HTTP {r.status_code} | {ctype} | {len(r.content)}B | {snippet[:65]}")
        except Exception as exc:
            print(f"[FALHA] {label} ({urlparse(url).hostname}) -> {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    inspecionar_fgtas()
    testar_portais_centralizados()
              
