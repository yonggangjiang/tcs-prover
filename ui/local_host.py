"""Which browser addresses a TCS Prover server answers.

The server listens on 127.0.0.1 only. A browser on this computer opens it at
127.0.0.1 or localhost; one on another computer reaches it through a forwarded
port (VS Code Remote, ``ssh -L``), often as ``localhost`` and on another port
number. Every request must therefore name a loopback host, with any port, in its
Host header: a web page whose own domain is made to resolve to 127.0.0.1 (DNS
rebinding) still sends that domain and is refused. An Origin, when the browser
sends one, must be the address the request went to, so a page served from
anywhere else cannot use the server. The access key is checked as well.
"""

import re

# Never a domain name, which DNS outside this computer could point here.
_LOOPBACK_HOST = re.compile(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::([1-9][0-9]{0,4}))?")


def trusted_host(host):
    """True when a Host header names this computer: 127.0.0.1, localhost or [::1], with any port."""

    match = _LOOPBACK_HOST.fullmatch(host) if isinstance(host, str) else None
    return bool(match) and int(match.group(1) or 80) <= 65535


def same_origin(origin, host):
    """True when no Origin was sent, or it is the page at the address the request went to."""

    return not origin or origin == f"http://{host}"
