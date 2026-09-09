import argparse
import asyncio
import json
import os

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
    login_parser.add_argument("--port", type=int, default=6080)
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

            serve(store, settings, args.port)
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

            if not 1024 <= args.port <= 65535:
                parser.error("Login port must be between 1024 and 65535")
            login(settings, args.remote, args.port)
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
    except Exception as exc:
        parser.exit(1, f"{safe_error(exc)}\n")


if __name__ == "__main__":
    main()
