#!/usr/bin/env python3
"""Autoteste local das regras do diagnóstico municipal V3.2; não acessa a rede."""

from coletar_prefeituras_rs import TARGETS, PLACEHOLDER_TERMS, host_allowed, clean


def main():
    assert len(TARGETS) == 5
    by_city = {item["municipality"]: item for item in TARGETS}
    caxias = by_city["Caxias do Sul"]
    assert caxias["url"] == (
        "https://caxias.rs.gov.br/servicos/desenvolvimento-economico/"
        "vagas-de-emprego-fgtas-sine"
    )
    assert caxias["expected"] == "employment_service"
    assert by_city["Novo Hamburgo"]["url"].endswith(
        "/smdei/diretoria-trabalho/gerencia-trabalho"
    )
    assert by_city["Canoas"]["url"].startswith(
        "https://sistemas.canoas.rs.gov.br/bancodeoportunidades/"
    )
    assert by_city["Pelotas"]["url"].endswith("/oportunidades")
    bento = by_city["Bento Gonçalves"]
    assert bento["url"].endswith("/cidadao/pagina/sala-do-empreendedor")
    assert bento["fallback_urls"] == ["https://bentogoncalves.atende.net/"]
    assert host_allowed(
        "https://sistemas.canoas.rs.gov.br/bancodeoportunidades/",
        "https://sistemas.canoas.rs.gov.br/bancodeoportunidades/",
    )
    assert not host_allowed(
        "https://site-falso.example/vagas", "https://canoas.rs.gov.br/"
    )
    assert PLACEHOLDER_TERMS.search("Concursos e estágios teste")
    assert PLACEHOLDER_TERMS.search("/oportunidades/selecao-teste")
    assert not PLACEHOLDER_TERMS.search("Oportunidades de trabalho")
    assert clean("  Gerência   do Trabalho ") == "Gerência do Trabalho"
    print("Autoteste municipal V3.2: OK")
    print("Caxias com URL oficial; Bento com URL de serviço e fallback; regras preservadas.")


if __name__ == "__main__":
    main()
