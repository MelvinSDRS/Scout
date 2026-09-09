import json
import time

import httpx


async def send(settings, text, client):
    if not settings.telegram_token or not settings.telegram_chat:
        raise RuntimeError("Telegram token/chat are missing")
    response = await client.post(
        f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
        json={
            "chat_id": settings.telegram_chat,
            **({"message_thread_id": settings.telegram_thread} if settings.telegram_thread else {}),
            "text": text[:4000],
            "link_preview_options": {"is_disabled": False},
        },
    )
    # Never raise HTTPStatusError: its URL contains the bot token.
    if response.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {response.status_code}")
    if not response.json().get("ok"):
        raise RuntimeError("Telegram rejected notification")
    return response.json().get("result", {})


def render(payload):
    label = "Initial match" if payload["initial"] else "Newly found match"
    price = (
        "Price unknown"
        if payload["price"] is None
        else f"{payload['price']} {payload['currency'] or ''}"
    )
    return (
        f"{label}: {payload['watch']}\n{payload['title'][:500]}\n"
        f"{price} ({payload.get('price_kind', 'price')}) · {payload['source']} · search region {payload['country']}\n"
        f"{payload['location'][:200]}\n{payload['url']}"
    )


async def deliver(store, settings, client=None):
    owned = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        with store.connect() as db:
            rows = db.execute(
                """SELECT outbox.* FROM outbox JOIN watches ON watches.id=watch_id
                WHERE sent_at IS NULL AND next_try<=? AND coalesce(json_extract(watches.spec,'$.enabled'),1)=1 ORDER BY outbox.id LIMIT 10""",
                (time.time(),),
            ).fetchall()
        for row in rows:
            try:
                await send(settings, render(json.loads(row["payload"])), client)
            except Exception as exc:
                # Transport exceptions can include sensitive request URLs; store only class.
                error = str(exc) if type(exc) is RuntimeError else type(exc).__name__
                with store.connect() as db:
                    db.execute(
                        "UPDATE outbox SET attempts=attempts+1,error=?,next_try=? WHERE id=?",
                        (
                            error,
                            time.time() + min(3600, 30 * 2 ** min(row["attempts"], 7)),
                            row["id"],
                        ),
                    )
                break
            else:
                with store.connect() as db:
                    db.execute(
                        "UPDATE outbox SET sent_at=?,error=NULL WHERE id=?",
                        (time.time(), row["id"]),
                    )
                import asyncio

                await asyncio.sleep(1.1)
    finally:
        if owned:
            await client.aclose()
