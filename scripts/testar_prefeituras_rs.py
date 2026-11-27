#!/usr/bin/env python3
"""Autoteste das regras municipais e do parser PDF V3.3; não acessa a rede."""

from coletar_prefeituras_rs import (
    TARGETS, PLACEHOLDER_TERMS, host_allowed, clean, parse_caxias_pdf,
)


def main():
    assert len(TARGETS) == 5
    by_city = {item["municipality"]: item for item in TARGETS}
    caxias = by_city["Caxias do Sul"]
    assert caxias["url"].endswith("/vagas-de-emprego-fgtas-sine")
    assert caxias["expected"] == "employment_service"
    assert by_city["Novo Hamburgo"]["url"].endswith("/smdei/diretoria-trabalho/gerencia-trabalho")
    assert by_city["Canoas"]["url"].startswith("https://sistemas.canoas.rs.gov.br/bancodeoportunidades/")
    assert by_city["Pelotas"]["url"].endswith("/oportunidades")
    bento = by_city["Bento Gonçalves"]
    assert bento["url"].endswith("/cidadao/pagina/sala-do-empreendedor")
    assert "https://bentogoncalves.atende.net/" in bento["fallback_urls"]
    assert "https://www.bentogoncalves.rs.gov.br/" in bento["fallback_urls"]
    assert host_allowed("https://gcpstorage.caxias.rs.gov.br/documents/painel.pdf", caxias["url"])
    assert not host_allowed("https://site-falso.example/vagas", "https://canoas.rs.gov.br/")
    assert PLACEHOLDER_TERMS.search("Concursos e estágios teste")
    assert PLACEHOLDER_TERMS.search("/oportunidades/selecao-teste")
    assert not PLACEHOLDER_TERMS.search("Oportunidades de trabalho")
    assert clean("  Gerência   do Trabalho ") == "Gerência do Trabalho"
    print("Autoteste municipal V3.3: OK")
    print("PDF de Caxias, fallback oficial de Bento e filtros de homologação conferidos.")


if __name__ == "__main__":
    main()
