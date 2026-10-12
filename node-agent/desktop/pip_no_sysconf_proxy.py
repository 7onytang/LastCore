"""pip driver that skips only the macOS SystemConfiguration proxy fallback.

On macOS ``urllib.request.getproxies()`` consults SystemConfiguration even
when no proxy environment variable is set. On hosts where the configured
system proxy is broken for python TLS (pip fails with
``SSL: UNEXPECTED_EOF_WHILE_READING`` while direct egress works), this
driver disables that system fallback for the pip invocation only. No system
settings are read, written or modified.

Standard environment proxy behavior is preserved unchanged: ``HTTP_PROXY`` /
``HTTPS_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY`` (uppercase or lowercase, the
stdlib ``getproxies_environment()`` semantics) keep working exactly as with
plain pip; only the macOS SystemConfiguration fallback is skipped. On
platforms other than macOS nothing is patched at all.
"""
import sys
import urllib.request

if sys.platform == "darwin":
    urllib.request.getproxies = urllib.request.getproxies_environment

from pip._internal.cli.main import main  # noqa: E402  (after proxy adjustment)

if __name__ == "__main__":
    sys.exit(main())
