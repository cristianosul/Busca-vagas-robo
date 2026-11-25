import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cffi_requests

# Fontes públicas gratuitas de listas de proxies HTTP/HTTPS filtrados por Brasil (BR)
FONTES_PROXY_BR = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=BR&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
]

ALVOS_SINE = [
    ("FGTAS-SINE", "https://fgtas.rs.gov.br/"),
    ("Novo Hamburgo", "https://www.novohamburgo.rs.gov.br/sedec/agencia-municipal-empregos"),
    ("Bento Goncalves", "https://bentogoncalves.atende.net/cidadao/pagina/balcao-de-empregos"),
]


def coletar_proxies_br() -> list[str]:
    proxies = set()
    # 1. ProxyScrape filtrado especificamente para country=BR
    try:
        r = cffi_requests.get(FONTES_PROXY_BR[0], timeout=10)
        if r.status_code == 200:
            for linha in r.text.splitlines():
                linha = linha.strip()
                if ":" in linha:
                    proxies.add(f"http://{linha}")
    except Exception as exc:
        print(f"[AVISO] Falha ao buscar lista ProxyScrape BR: {exc}")

    return list(proxies)


def testar_proxy_vivo(proxy_url: str) -> str | None:
    """Verifica se o proxy responde rápido e confirma o país do IP."""
    try:
        r = cffi_requests.get(
            "http://ip-api.com/json/?fields=status,countryCode,isp,query",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=6,
        )
        data = r.json()
        if data.get("status") == "success" and data.get("countryCode") == "BR":
            print(f"[PROXY BR VIVO] {proxy_url} -> IP={data.get('query')} ({data.get('isp')})")
            return proxy_url
    except Exception:
        pass
    return None


def main() -> None:
    print("=== 1. BUSCANDO PROXIES GRATUITOS NO BRASIL ===")
    candidatos = coletar_proxies_br()
    print(f"Total de candidatos BR encontrados: {len(candidatos)}")

    if not candidatos:
        print("Nenhum proxy BR retornado agora nas listas públicas.")
        return

    # Testa até 40 candidatos em paralelo para achar os que estão realmente online
    vivos = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futuros = [pool.submit(testar_proxy_vivo, p) for p in candidatos[:40]]
        for fut in as_completed(futuros):
            res = fut.result()
            if res:
                vivos.append(res)
                if len(vivos) >= 3:  # Basta achar até 3 proxies BR vivos para o teste
                    break

    if not vivos:
        print("\nNenhum proxy gratuito BR respondeu dentro de 6s neste momento.")
        return

    print(f"\n=== 2. TESTANDO ALVOS SINE COM PROXY BR ({vivos[0]}) ===")
    proxy_escolhido = vivos[0]
    proxies_cfg = {"http": proxy_escolhido, "https": proxy_escolhido}

    for nome, url in ALVOS_SINE:
        t0 = time.perf_counter()
        try:
            r = cffi_requests.get(
                url,
                impersonate="chrome",
                proxies=proxies_cfg,
                timeout=20,
                allow_redirects=True,
            )
            dt = (time.perf_counter() - t0) * 1000
            snippet = " ".join(r.text[:140].split())
            print(f"[OK] {nome} -> HTTP {r.status_code} | {len(r.content)}B ({dt:.0f}ms) | {snippet[:65]}")
        except Exception as exc:
            dt = (time.perf_counter() - t0) * 1000
            print(f"[FALHA] {nome} ({dt:.0f}ms) -> {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
