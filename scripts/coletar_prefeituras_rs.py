#!/usr/bin/env python3
"""Coleta oportunidades municipais do RS e gera JSON padronizado para o BuscaVagas."""

import tempfile
from unittest.mock import MagicMock, patch

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

TZ_POA = ZoneInfo("America/Sao_Paulo")

# URLs oficiais (podem ser sobrescritas por variáveis de ambiente no GitHub Actions)
URL_CAXIAS_FGTAS_SINE = os.getenv(
    "URL_CAXIAS_FGTAS_SINE",
    "https://caxias.rs.gov.br/",
)
URL_NOVO_HAMBURGO_FGTAS_SINE = os.getenv(
    "URL_NOVO_HAMBURGO_FGTAS_SINE",
    "https://www.novohamburgo.rs.gov.br/",
)
URL_BENTO_GONCALVES = os.getenv(
    "URL_BENTO_GONCALVES",
    "https://bentogoncalves.atende.net/cidadao/pagina/balcao-de-empregos",
)
URL_CANOAS_BANCO = os.getenv(
    "URL_CANOAS_BANCO",
    "https://canoas.rs.gov.br/servicos/banco-de-oportunidades/",
)
URL_PELOTAS_OPORTUNIDADES = os.getenv(
    "URL_PELOTAS_OPORTUNIDADES",
    "https://www.pelotas.com.br/oportunidades",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

REGEX_SELECAO_PUBLICA = re.compile(
    r"(concurso|sele[cç][aã]o\s+p[uú]blica|processo\s+seletivo|\bpss\b|edital|est[aá]gio)",
    re.I,
)
REGEX_VAGA_PRIVADA = re.compile(
    r"(vaga|emprego|balc[aã]o|sine|trabalhador|curr[ií]culo|desenvolvimento\s+econ[oô]mico|sala\s+do\s+empreendedor)",
    re.I,
)
REGEX_CAMPOS_ANUNCIO = re.compile(
    r"(sal[aá]rio|carga\s+hor[aá]ria|requisitos?|escolaridade|benef[ií]cios?|como\s+se\s+candidatar)",
    re.I,
)


def clean(txt: str) -> str:
    return re.sub(r"\s+", " ", txt or "").strip()


def limpar_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup


def coletar_agencia_municipal(
    client: httpx.Client,
    municipio: str,
    url: str,
    links_extras_conhecidos: list[tuple[str, str]] | None = None,
) -> dict:
    resultado = {
        "municipio": municipio,
        "fonte": "Agência FGTAS/SINE Municipal",
        "url": url,
        "status": "erro",
        "http_status": None,
        "resumo_vagas": [],
        "oportunidades": [],
        "pdfs": [],
    }
    try:
        resp = client.get(url)
        resultado["http_status"] = resp.status_code
        resultado["url_final"] = str(resp.url)
        if resp.status_code != 200:
            resultado["status"] = f"http_{resp.status_code}"
            return resultado

        soup = limpar_soup(resp.text)
        texto = clean(soup.get_text(" ", strip=True))
        resultado["status"] = "ok"
        resultado["resumo_vagas"] = re.findall(
            r".{0,45}\b\d{1,4}\s+vagas?\b.{0,45}", texto, re.I
        )[:5]

        vistos = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(str(resp.url), a["href"].strip())
            label = clean(a.get_text(" ", strip=True))
            if not href.startswith("http") or href in vistos:
                continue
            vistos.add(href)

            if href.lower().endswith(".pdf") or "/upload/" in href.lower():
                resultado["pdfs"].append({"titulo": label or "Boletim PDF", "url": href})
            elif REGEX_VAGA_PRIVADA.search(label) or REGEX_VAGA_PRIVADA.search(href):
                resultado["oportunidades"].append(
                    {
                        "municipio": municipio,
                        "titulo": label or "Atendimento / Vagas FGTAS-SINE",
                        "categoria": "emprego_sine_municipal",
                        "url": href,
                    }
                )

        if links_extras_conhecidos:
            for titulo_extra, url_extra in links_extras_conhecidos:
                if url_extra not in vistos:
                    resultado["oportunidades"].append(
                        {
                            "municipio": municipio,
                            "titulo": titulo_extra,
                            "categoria": "emprego_sine_municipal",
                            "url": url_extra,
                        }
                    )
    except Exception as exc:
        resultado["erro"] = f"{type(exc).__name__}: {exc}"

    return resultado


def coletar_bento_goncalves(url: str, tentativas: int = 3, pausa_seg: float = 2.0) -> dict:
    resultado = {
        "municipio": "Bento Gonçalves",
        "fonte": "Balcão de Empregos (Atende.Net)",
        "url": url,
        "status": "indisponivel_timeout",
        "tentativas": [],
        "oportunidades": [],
    }
    for i in range(1, tentativas + 1):
        inicio = time.monotonic()
        try:
            with httpx.Client(headers=HEADERS, timeout=15.0, follow_redirects=True) as client:
                resp = client.get(url)
            duracao = round(time.monotonic() - inicio, 2)
            resultado["tentativas"].append(
                {"tentativa": i, "http_status": resp.status_code, "tempo_seg": duracao}
            )
            if resp.status_code == 200:
                resultado["status"] = "ok"
                soup = limpar_soup(resp.text)
                for a in soup.find_all("a", href=True):
                    href = urljoin(str(resp.url), a["href"].strip())
                    label = clean(a.get_text(" ", strip=True))
                    if href.startswith("http") and REGEX_VAGA_PRIVADA.search(f"{label} {href}"):
                        resultado["oportunidades"].append(
                            {
                                "municipio": "Bento Gonçalves",
                                "titulo": label or "Balcão de Empregos",
                                "categoria": "emprego_municipal",
                                "url": href,
                            }
                        )
                return resultado
        except Exception as exc:
            duracao = round(time.monotonic() - inicio, 2)
            resultado["tentativas"].append(
                {"tentativa": i, "erro": type(exc).__name__, "tempo_seg": duracao}
            )
        if i < tentativas:
            time.sleep(pausa_seg)

    return resultado


def coletar_canoas(client: httpx.Client, url: str) -> dict:
    resultado = {
        "municipio": "Canoas",
        "fonte": "Banco de Oportunidades",
        "url": url,
        "status": "erro",
        "possui_anuncios_individuais": False,
        "indicios_campos_anuncio": 0,
        "oportunidades": [],
    }
    try:
        resp = client.get(url)
        resultado["http_status"] = resp.status_code
        if resp.status_code != 200:
            resultado["status"] = f"http_{resp.status_code}"
            return resultado

        soup = limpar_soup(resp.text)
        texto = clean(soup.get_text(" ", strip=True))
        indicios = REGEX_CAMPOS_ANUNCIO.findall(texto)
        resultado["indicios_campos_anuncio"] = len(indicios)
        # Só classifica como mural de anúncios individuais se houver múltiplos indícios reais
        resultado["possui_anuncios_individuais"] = len(indicios) >= 5
        resultado["status"] = (
            "ok_anuncios_individuais"
            if resultado["possui_anuncios_individuais"]
            else "ok_portal_institucional_sem_anuncios"
        )

        if resultado["possui_anuncios_individuais"]:
            vistos = set()
            for a in soup.find_all("a", href=True):
                href = urljoin(str(resp.url), a["href"].strip())
                label = clean(a.get_text(" ", strip=True))
                if href not in vistos and href.startswith("http") and REGEX_VAGA_PRIVADA.search(label):
                    vistos.add(href)
                    resultado["oportunidades"].append(
                        {
                            "municipio": "Canoas",
                            "titulo": label,
                            "categoria": "emprego_municipal",
                            "url": href,
                        }
                    )
    except Exception as exc:
        resultado["erro"] = f"{type(exc).__name__}: {exc}"

    return resultado


def coletar_pelotas(client: httpx.Client, url: str) -> dict:
    resultado = {
        "municipio": "Pelotas",
        "fonte": "Portal de Oportunidades",
        "url": url,
        "status": "erro",
        "vagas_privadas": [],
        "selecoes_publicas": [],
    }
    try:
        resp = client.get(url)
        resultado["http_status"] = resp.status_code
        if resp.status_code != 200:
            resultado["status"] = f"http_{resp.status_code}"
            return resultado

        soup = limpar_soup(resp.text)
        resultado["status"] = "ok"
        vistos = set()

        for a in soup.find_all("a", href=True):
            href = urljoin(str(resp.url), a["href"].strip())
            label = clean(a.get_text(" ", strip=True))
            alvo = f"{label} {href}"
            if href in vistos or not href.startswith("http"):
                continue
            vistos.add(href)

            item = {
                "municipio": "Pelotas",
                "titulo": label or "Oportunidade Pelotas",
                "url": href,
            }
            if REGEX_SELECAO_PUBLICA.search(alvo):
                item["categoria"] = "selecao_publica"
                resultado["selecoes_publicas"].append(item)
            elif REGEX_VAGA_PRIVADA.search(alvo):
                item["categoria"] = "emprego_privado_ou_fomento"
                resultado["vagas_privadas"].append(item)
    except Exception as exc:
        resultado["erro"] = f"{type(exc).__name__}: {exc}"

    return resultado


def main() -> None:
    parser = argparse.ArgumentParser(description="Coletor municipal RS - BuscaVagas")
    parser.add_argument(
        "--output",
        default="resultado-fgtas-rs/prefeituras_rs.json",
        help="Caminho do JSON de saída",
    )
    args = parser.parse_args()

    agora_poa = datetime.now(TZ_POA)

    with httpx.Client(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        caxias = coletar_agencia_municipal(client, "Caxias do Sul", URL_CAXIAS_FGTAS_SINE)
        novo_hamburgo = coletar_agencia_municipal(
            client,
            "Novo Hamburgo",
            URL_NOVO_HAMBURGO_FGTAS_SINE,
            links_extras_conhecidos=[
                ("Sala do Empreendedor - Novo Hamburgo", "https://www.novohamburgo.rs.gov.br/index.php/node/40424")
            ],
        )
        canoas = coletar_canoas(client, URL_CANOAS_BANCO)
        pelotas = coletar_pelotas(client, URL_PELOTAS_OPORTUNIDADES)

    bento = coletar_bento_goncalves(URL_BENTO_GONCALVES, tentativas=3, pausa_seg=2.0)

    cidades = [caxias, novo_hamburgo, bento, canoas, pelotas]

    payload = {
        "gerado_em_poa": agora_poa.isoformat(),
        "fuso_horario": "America/Sao_Paulo",
        "resumo": {
            "total_municipios_avaliados": len(cidades),
            "caxias_oportunidades": len(caxias["oportunidades"]),
            "novo_hamburgo_oportunidades": len(novo_hamburgo["oportunidades"]),
            "bento_goncalves_status": bento["status"],
            "canoas_possui_anuncios_individuais": canoas["possui_anuncios_individuais"],
            "pelotas_vagas_privadas": len(pelotas["vagas_privadas"]),
            "pelotas_selecoes_publicas": len(pelotas["selecoes_publicas"]),
        },
        "municipios": cidades,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"JSON municipal gerado: {out_path}")
    for k, v in payload["resumo"].items():
        print(f"  - {k}: {v}")


if __name__ == "__main__":
    main()
      
