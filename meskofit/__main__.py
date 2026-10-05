"""MeskoFit: a self-hosted workout, nutrition and progress tracker you run on your own PC and use from
your iPhone over your home network. Port of main.go (flags keep their single-dash Go names; ``--name`` works too)."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
import ssl
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from . import __version__, certs, qr

log = logging.getLogger("meskofit")


class _Fatal(Exception):
    """Startup failure: printed as 'MeskoFit could not start: ...' and exit code 1."""


# ───────────────────────── CLI ─────────────────────────

def default_data_dir() -> Path:
    """MESKOFIT_DATA, else ./meskofit-data in the working directory."""
    env = os.environ.get("MESKOFIT_DATA")
    return Path(env) if env else Path.cwd() / "meskofit-data"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="meskofit", allow_abbrev=False,
                                description="Self-hosted workout, nutrition and progress tracker.")

    def opt(name: str, **kw):
        p.add_argument(f"-{name}", f"--{name}", **kw)

    opt("data", default=str(default_data_dir()), metavar="DIR", help="folder for the database, certificates and photos")
    opt("port", type=int, default=8080, help="HTTP port (0 = off) (default 8080)")
    opt("https-port", type=int, default=8443, dest="https_port",
        help="HTTPS port, needed for live camera barcode scanning (0 = off) (default 8443)")
    opt("host", default="", help="extra host names or IPs for the HTTPS certificate, comma-separated")
    opt("reset-ca", action="store_true", dest="reset_ca",
        help="create a new local certificate authority (reinstall the iPhone profile afterwards)")
    opt("ca-unconstrained", action="store_true", dest="ca_unconstrained",
        help="with -reset-ca: don't limit the CA to home-network names")
    opt("reset-password", action="store_true", dest="reset_password", help="remove the app password, then exit")
    opt("no-browser", action="store_true", dest="no_browser", help="don't open the setup page in your browser on first run")
    opt("no-qr", action="store_true", dest="no_qr", help="don't print the QR code at startup")
    opt("dev", default="", help="development: proxy the web app to a Vite dev server, e.g. http://localhost:5173")
    opt("v", action="store_true", dest="verbose", help="log every API request")
    opt("version", action="store_true", dest="show_version", help="print the version and exit")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.show_version:
        print("MeskoFit", __version__)
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    try:
        return _run(args)
    except _Fatal as e:
        print("\n  MeskoFit could not start:", e, file=sys.stderr)
        if sys.platform == "win32":  # keep a double-clicked console window readable
            print("\n  Press Enter to close.", file=sys.stderr)
            with contextlib.suppress(Exception):
                sys.stdin.readline()
        return 1
    except KeyboardInterrupt:
        return 0


def _run(args: argparse.Namespace) -> int:
    from .app import App, Options

    data_dir = Path(args.data).expanduser().resolve()
    extra = [h.strip() for h in args.host.split(",") if h.strip()]

    cm: certs.Manager | None = None
    if args.https_port > 0:
        try:
            cm = certs.Manager.open(data_dir / "certs", extra, args.ca_unconstrained, args.reset_ca)
        except Exception as e:  # noqa: BLE001
            raise _Fatal(f"setting up HTTPS certificates: {e}") from e

    try:
        app = App(Options(data_dir=data_dir, version=__version__, http_port=args.port, https_port=args.https_port,
                          certs=cm, dev_proxy=args.dev, verbose=args.verbose))
    except Exception as e:  # noqa: BLE001
        raise _Fatal(f"opening data folder {data_dir}: {e}") from e
    try:
        if args.reset_password:
            from .auth import reset_password

            reset_password(app)
            print("App password removed.")
            return 0
        if args.port <= 0 and cm is None:
            raise _Fatal("both -port and -https-port are 0; nothing to serve")

        from .defaults import seed_profile

        # Optional: drop a profile_defaults.json in the data folder and a fresh install starts already set up.
        try:
            if seed_profile(app, data_dir / "profile_defaults.json"):
                print("Created your profile from profile_defaults.json.")
        except (ValueError, OSError) as e:
            print(f"profile_defaults.json: {e}")

        from .server import build

        asgi = build(app)
        socks = {}
        if args.port > 0:
            socks["http"] = _listen(args.port, "-port")
        if cm is not None:
            socks["https"] = _listen(args.https_port, "-https-port")

        banner(app, cm, args.port, args.https_port, str(data_dir), not args.no_qr)
        if not args.no_browser and app.first_run() and args.port > 0:
            open_browser(f"http://localhost:{args.port}/connect")
        try:
            asyncio.run(_serve(asgi, socks, cm, args))
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        app.close()


# ───────────────────────── serving ─────────────────────────

def _listen(port: int, flag: str) -> list[socket.socket]:
    """Bind the port on all IPv4 interfaces (and IPv6 where available), like Go's ':port'."""
    socks: list[socket.socket] = []
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        socks.append(s)
    except OSError as e:
        if s is not None:
            s.close()
        raise _Fatal(port_error(port, flag, e)) from e
    if socket.has_ipv6:
        s6 = None
        try:
            s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            if sys.platform != "win32":
                s6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s6.bind(("::", port))
            socks.append(s6)
        except OSError:
            if s6 is not None:
                s6.close()  # no IPv6: fine
    return socks


def port_error(port: int, flag: str, err: OSError) -> str:
    msg = str(err)
    if "in use" in msg or "Only one usage" in msg or getattr(err, "errno", 0) in (98, 48, 10048):
        return f"port {port} is already in use — is MeskoFit already running? Pick another with {flag}"
    return msg


class _QuietTLS(logging.Filter):
    """Hides the noisy TLS handshake errors Safari causes before the certificate profile is trusted."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and isinstance(record.exc_info[1], ssl.SSLError):
            return False
        msg = record.getMessage().lower()
        return not ("ssl" in msg or "tls" in msg or "handshake" in msg)


def _make_server(asgi, cm: certs.Manager | None, args: argparse.Namespace, tls: bool):
    import uvicorn

    class Server(uvicorn.Server):
        def capture_signals(self):  # we handle SIGINT/SIGTERM once for both servers
            return contextlib.nullcontext()

    kw: dict = {}
    if tls:
        kw["ssl_certfile"], kw["ssl_keyfile"] = cm.leaf_paths()
    cfg = uvicorn.Config(asgi, log_level="warning", access_log=False, proxy_headers=False, server_header=False,
                         timeout_keep_alive=120, timeout_graceful_shutdown=5, **kw)
    if args.verbose:
        logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    if tls:
        cfg.load()  # build the SSLContext now so refreshed certificates can be loaded into it later
        cfg.ssl.minimum_version = ssl.TLSVersion.TLSv1_2
    return Server(cfg)


async def _serve(asgi, socks: dict[str, list[socket.socket]], cm: certs.Manager | None, args: argparse.Namespace) -> None:
    quiet = _QuietTLS()
    for name in ("uvicorn.error", "asyncio"):
        logging.getLogger(name).addFilter(quiet)

    servers = {k: _make_server(asgi, cm, args, k == "https") for k in socks}
    stop = asyncio.Event()

    def on_signal(*_):
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
        except (NotImplementedError, RuntimeError, ValueError):
            with contextlib.suppress(Exception):
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    async def run(name: str) -> None:
        try:
            await servers[name].serve(sockets=socks[name])
        except SystemExit as e:  # uvicorn exits on startup failure
            raise RuntimeError(f"{name} server failed to start (exit {e.code})") from e

    async def refresh_loop() -> None:
        while True:
            await asyncio.sleep(10 * 60)
            try:
                if await asyncio.to_thread(cm.refresh):
                    # a new handshake picks up the certificate loaded into the live context
                    servers["https"].config.ssl.load_cert_chain(*cm.leaf_paths())
            except Exception as e:  # noqa: BLE001
                log.warning("certificate refresh: %s", e)

    tasks = {asyncio.create_task(run(n)) for n in servers}
    aux = [asyncio.create_task(refresh_loop())] if cm is not None and "https" in servers else []
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({waiter, *tasks}, return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            print("\nStopping MeskoFit…")
    finally:
        for s in servers.values():
            s.should_exit = True
        for t in aux + [waiter]:
            t.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
            raise _Fatal(str(r))


# ───────────────────────── banner ─────────────────────────

def _port_suffix(p: int, default: int) -> str:
    return "" if p == default else f":{p}"


def urls(http_port: int, https_port: int, have_certs: bool) -> dict[str, list[str]]:
    """Addresses an iPhone can use to reach this machine (same shape as the Go ServerURLs)."""
    u: dict[str, list[str]] = {"http": [], "https": [], "tailscale": []}
    for ip in certs.local_ips():
        host = str(ip)
        if http_port > 0:
            u["http"].append(f"http://{host}{_port_suffix(http_port, 80)}")
        if https_port > 0 and have_certs:
            u["https"].append(f"https://{host}{_port_suffix(https_port, 443)}")
        if certs.is_tailscale(ip):
            u["tailscale"].append(host)
    return u


def banner(app, cm: certs.Manager | None, http_port: int, https_port: int, data_dir: str, show_qr: bool) -> None:
    u = urls(http_port, https_port, cm is not None)
    out = ["", f"  MeskoFit {__version__} is running", ""]
    if not u["http"] and not u["https"]:
        out.append(f"  Open http://localhost:{http_port} on this computer.")
    else:
        out.append("  On your iPhone (same Wi-Fi), open:")
        out += ["    " + s for s in u["http"][:3]]
        if u["https"]:
            out += ["", "  Secure address for live camera scanning (set up via the Connect page):", "    " + u["https"][0]]
        if u["tailscale"]:
            out += ["", f"  Tailscale detected ({u['tailscale'][0]}): use that address from the gym."]
        if show_qr and u["http"]:
            try:
                code = qr.terminal(u["http"][0] + "/connect")
                out += ["", "  Point your iPhone camera here:"] + ["  " + line for line in code.rstrip("\n").split("\n")]
            except ValueError:
                pass
    if cm is not None:
        out += ["  note: " + w for w in cm.warnings if "public address" not in w]
    out += ["", f"  Data folder: {data_dir}", f"  Setup page on this PC: http://localhost:{http_port}/connect",
            "  Keep this window open while you use the app. Ctrl+C stops it.", ""]
    print("\n".join(out), flush=True)


def open_browser(url: str) -> None:
    """Open url in the default browser (on Linux only when a display is available)."""
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
