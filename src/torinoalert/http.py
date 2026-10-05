import urllib.request

USER_AGENT = "TorinoAlertBot/2.0 (+https://github.com/InitialDeu/torinoalert)"


def fetch_bytes(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(url: str, timeout: float) -> str:
    return fetch_bytes(url, timeout).decode("utf-8", errors="ignore")
