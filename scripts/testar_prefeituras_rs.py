#!/usr/bin/env python3
"""Autoteste das regras municipais e do parser PDF V3.4; não acessa a rede."""

from coletar_prefeituras_rs import (
    TARGETS, PLACEHOLDER_TERMS, host_allowed, clean, parse_job_records_from_text,
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
    # O parser precisa encontrar códigos mesmo quando a extração do PDF não os deixa no início da linha.
    sample = "Painel FGTAS\nCódigo 1234567 - Auxiliar de produção - salário R$ 1.800,00\n1234567 - duplicado\n7654321 – Operador de caixa"
    records = parse_job_records_from_text(sample, "Painel teste", "https://gcpstorage.caxias.rs.gov.br/painel.pdf", "2026-11-25")
    assert [item["source_id"] for item in records] == ["1234567", "7654321"]
    assert records[0]["availability_status"] == "needs_confirmation"
    assert records[0]["is_current_opportunity"] is False
    assert records[0]["publication_date"] == "2026-11-25"
    # O total agregado não participa desta função e nunca vira contagem de registros.
    assert len(records) == 2
    print("Autoteste municipal V3.4: OK")
    print("PDF, códigos únicos, confirmação de disponibilidade, fallback de Bento e filtros de homologação conferidos.")


if __name__ == "__main__":
    main()
