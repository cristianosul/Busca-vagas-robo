import socket
import time
from urllib.parse import urlparse
import httpx
from curl_cffi import requests as cffi_requests

TARGETS = [
    ("FGTAS-SINE (Home)", "https://fgtas.rs.gov.br/"),
    ("FGTAS-SINE (Agencias)", "https://fgtas.rs.gov.br/agencias-fgtas-sine"),
    ("Novo Hamburgo - Agencia Empregos", "https://www.novohamburgo.rs.gov.br/sedec/agencia-municipal-empregos"),
    ("Bento Goncalves - Balcao Empregos", "https://bentogoncalves.atende.net/cidadao/pagina/balcao-de-empregos"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

TIMEOUT_SEC = 12


def check_tcp_443(host: str) -> str:
    t0 = time.perf_counter()
    try:
        ip = socket.gethostbyname(host)
        with socket.create_connection((ip, 443), timeout=6):
            dt = (time.perf_counter() - t0) * 1000
            return f"TCP OK ({ip}) em {dt:.0f}ms"
    except Exception as exc:
        dt = (time.perf_counter() - t0) * 1000
        return f"TCP FALHA ({dt:.0f}ms) -> {type(exc).__name__}: {exc}"


def run_httpx(url: str, http2: bool) -> str:
    t0 = time.perf_counter()
    try:
        with httpx.Client(
            http2=http2,
            headers=HEADERS,
            timeout=TIMEOUT_SEC,
            follow_redirects=True,
        ) as client:
            r = client.get(url)
            dt = (time.perf_counter() - t0) * 1000
            snippet = " ".join(r.text[:160].split())
            return f"HTTP {r.status_code} [{r.http_version}] {len(r.content)}B ({dt:.0f}ms) | {snippet[:70]}"
    except Exception as exc:
        dt = (time.perf_counter() - t0) * 1000
        return f"FALHA ({dt:.0f}ms) -> {type(exc).__name__}: {exc}"


def run_curl_cffi(url: str) -> str:
    t0 = time.perf_counter()
    try:
        r = cffi_requests.get(
            url,
            impersonate="chrome",
            headers={
                "Accept": HEADERS["Accept"],
                "Accept-Language": HEADERS["Accept-Language"],
            },
            timeout=TIMEOUT_SEC,
            allow_redirects=True,
        )
        dt = (time.perf_counter() - t0) * 1000
        snippet = " ".join(r.text[:160].split())
        return f"HTTP {r.status_code} [cffi-chrome] {len(r.content)}B ({dt:.0f}ms) | {snippet[:70]}"
    except Exception as exc:
        dt = (time.perf_counter() - t0) * 1000
        return f"FALHA ({dt:.0f}ms) -> {type(exc).__name__}: {exc}"


def main() -> None:
    print("=== DIAGNOSTICO TLS / HTTP2 / FINGERPRINT / IP ===")
    for label, url in TARGETS:
        host = urlparse(url).hostname or ""
        print(f"\n--- {label} ({url}) ---")
        print(f"  0. Socket - {check_tcp_443(host)}")
        print(f"  1. h1.1   - {run_httpx(url, http2=False)}")
        print(f"  2. h2     - {run_httpx(url, http2=True)}")
        print(f"  3. cffi   - {run_curl_cffi(url)}")


if __name__ == "__main__":
    main()
