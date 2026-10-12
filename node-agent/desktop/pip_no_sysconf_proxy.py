"""pip driver that ignores macOS SystemConfiguration proxies for this process only.

macOS hands proxy settings to Python via ``urllib.request.getproxies()`` even
when no environment variable is set. On hosts where the configured system
proxy is broken for python TLS (pip fails with ``SSL: UNEXPECTED_EOF_WHILE_
READING`` while direct egress works), this driver neutralizes the system
proxy lookup for the pip invocation only. No system settings are read,
written or modified; on hosts with a healthy proxy this behaves like plain pip.
"""
import sys
import urllib.request

urllib.request.getproxies = lambda: {}

from pip._internal.cli.main import main  # noqa: E402  (after proxy neutralization)

if __name__ == "__main__":
    sys.exit(main())
