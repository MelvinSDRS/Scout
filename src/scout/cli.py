import argparse
import asyncio
import json
import os
import sys

from .models import Watch
from .settings import Settings
from .store import Store


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Scout - Facebook Marketplace watches")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Create a persistent watch")
    add.add_argument("query")
    add.add_argument("--name")
    add.add_argument("--countries", nargs="+", choices=["US", "CA", "FR"], required=True)
    add.add_argument("--sources", nargs="+", choices=["facebook"], default=["facebook"])
    add.add_argument("--interval", type=int, default=60)
    add.add_argument("--exclude", action="append", default=[])
    add.add_argument("--max-price", action="append", default=[], metavar="USD=100")
    commands.add_parser("list")
    delete = commands.add_parser("delete")
    delete.add_argument("id", type=int)
    serve = commands.add_parser("serve", help="Start local web controls and the worker")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-open", action="store_true", help="Do not open the desktop browser")
    dashboard = commands.add_parser("open", help="Open the dashboard and sign in automatically")
    dashboard.add_argument("--url", default="http://127.0.0.1:8765", help="Dashboard root URL")
    dashboard.add_argument("--handoff", action="store_true", help=argparse.SUPPRESS)
    commands.add_parser("worker")
    commands.add_parser("once", help="Scan currently due regions and send pending alerts")
    commands.add_parser("status")
    commands.add_parser("token", help="Print the local dashboard access token")
    login_parser = commands.add_parser(
        "login", help="Manual Facebook login, including headless SSH"
    )
    login_parser.add_argument(
        "--remote", action="store_true", help="Use a temporary browser over SSH"
    )
    login_parser.add_argument(
        "--port", type=int, help="Viewer port (default: 6080, or a free port if busy)"
    )
    setup = login_parser.add_mutually_exclusive_group()
    setup.add_argument(
        "--setup", action="store_true", help="Install browser/viewer prerequisites, then log in"
    )
    setup.add_argument(
        "--setup-only", action="store_true", help="Install prerequisites without opening Facebook"
    )
    login_parser.add_argument(
        "--check", action="store_true", help="Verify the saved session headlessly, then exit"
    )
    login_parser.add_argument(
        "--ssh-host",
        metavar="USER@HOST",
        help="Your SSH alias or destination for the tunnel command",
    )
    login_parser.add_argument("--handoff", help=argparse.SUPPRESS)
    probe = commands.add_parser(
        "probe", help="Search one region without saving listings or sending alerts"
    )
    probe.add_argument("query")
    probe.add_argument("--country", choices=["US", "CA", "FR"], default="CA")
    probe.add_argument("--source", choices=["facebook"], default="facebook")
    commands.add_parser(
        "test-notification", help="Send one setup confirmation to your configured Telegram chat"
    )
    args = parser.parse_args()
    settings = Settings.load()
    settings.prepare()
    store = Store(settings.data_dir / "scout.sqlite3")
    from .providers.facebook import REGIONS, Facebook
    from .worker import run, safe_error, worker_lock

    try:
        if args.command == "add":
            prices = dict(p.split("=", 1) for p in args.max_price)
            watch = Watch(
                name=args.name or args.query,
                query=args.query,
                countries=args.countries,
                sources=args.sources,
                interval_minutes=args.interval,
                exclude=args.exclude,
                max_prices=prices,
            )
            print(f"Created watch {store.add(watch, REGIONS)}")
        elif args.command == "list":
            print(json.dumps(store.watches(), indent=2))
        elif args.command == "delete":
            if not store.remove(args.id):
                parser.error("Watch not found")
            print("Deleted")
        elif args.command == "token":
            print(settings.api_token)
        elif args.command == "status":
            print(json.dumps(store.health(), indent=2))
        elif args.command == "serve":
            from .server import serve

            serve(
                store,
                settings,
                args.port,
                open_browser=not args.no_open
                and sys.stdout.isatty()
                and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
                and not os.environ.get("SSH_CONNECTION"),
            )
        elif args.command == "open":
            from .dashboard import open_dashboard

            open_dashboard(store, settings, args.url, handoff=args.handoff)
        elif args.command in ("worker", "once"):
            asyncio.run(run(store, settings, once=args.command == "once"))
        elif args.command == "test-notification":
            import httpx

            from .notify import send

            async def test():
                async with httpx.AsyncClient(timeout=20) as client:
                    await send(
                        settings,
                        "Scout is connected. New watch matches will arrive here.",
                        client,
                    )

            asyncio.run(test())
            print("Telegram setup notification delivered")
        elif args.command == "login":
            from .login import login

            if args.handoff:
                from .login_handoff import login_from_client

                if args.remote or args.port or args.check or args.setup_only or args.ssh_host:
                    parser.error("--handoff cannot be combined with other login modes")
                login_from_client(settings, args.handoff, setup=args.setup)
                return
            if args.port is not None and not 1024 <= args.port <= 65535:
                parser.error("Login port must be between 1024 and 65535")
            if args.check and (args.remote or args.setup_only or args.ssh_host or args.port):
                parser.error(
                    "--check cannot be combined with remote viewer options or --setup-only"
                )
            login(
                settings,
                args.remote or bool(args.ssh_host),
                args.port,
                setup=args.setup,
                setup_only=args.setup_only,
                check=args.check,
                ssh_host=args.ssh_host,
            )
        elif args.command == "probe":

            async def probe():
                provider = Facebook(settings)
                with worker_lock(settings.data_dir):
                    try:
                        result = await provider.search(
                            args.query, args.country, REGIONS[args.country][0]["city"]
                        )
                        print(
                            json.dumps(
                                {
                                    "source": args.source,
                                    "country": args.country,
                                    "count": len(result.listings),
                                    "saturated": result.saturated,
                                }
                            )
                        )
                    finally:
                        await provider.close()

            asyncio.run(probe())
    except KeyboardInterrupt:
        parser.exit(130, "Cancelled. You can rerun the command when ready.\n")
    except Exception as exc:
        parser.exit(1, f"{safe_error(exc)}\n")


if __name__ == "__main__":
    main()
