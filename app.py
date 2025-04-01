import json
import logging
import sys

import stripe
import petname
import asyncpg
from litestar import Litestar, Request, Response, get, post
from litestar.response import Redirect
from litestar.status_codes import HTTP_404_NOT_FOUND
from colorama import Fore

import config

stripe.api_key = config.stripe_key
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger('stripe_service')
log.setLevel(logging.DEBUG)

def handle_404(request: Request, exc: Exception) -> Response:
    return Response({'status_code': 404}, status_code=404)


async def create_pool(app: Litestar) -> asyncpg.Pool:
    def _encode_jsonb(value):
        return json.dumps(value)

    def _decode_jsonb(value):
        return json.loads(value)

    async def init(con):
        await con.set_type_codec(
            'jsonb',
            schema='pg_catalog',
            encoder=_encode_jsonb,
            decoder=_decode_jsonb,
            format='text',
        )

    pool = await asyncpg.create_pool(
        config.postgresql,
        init=init,
        command_timeout=60
    )
    assert pool is not None

    if not getattr(app.state, 'pool', None):
        app.state.pool = pool

    return app.state.pool


async def close_pool(app: Litestar):
    if getattr(app.state, 'pool', None) is not None:
        await app.state.pool.close()


@get("/")
async def ping() -> str:
    return "Hello, world!"


@get('/success')
async def success(request: Request, session_id: str) -> Response:
    return Redirect('https://overseer-bot.net/guilds')


@get('/cancel')
async def cancel(request: Request, session_id: str) -> Response:
    return Redirect('https://overseer-bot.net')


@post("/webhook")
async def webhook(request: Request) -> Response:
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, config.stripe_secret)
        log.debug(f"Processed webhook event: {event.type}")
    except ValueError:
        return Response("Invalid payload", status_code=400)
    except stripe.SignatureVerificationError:
        return Response("Invalid signature", status_code=400)

    if event.type == "checkout.session.completed":
        log.debug(f"Checkout session completed: {event.data.object['id']}")
        session = event["data"]["object"]
        session_id = session.get("client_reference_id")
        if session_id is None:
            log.debug('No client_reference_id; skipping')
            return Response('client_reference_id not set', status_code=200)

        data = await request.app.state.pool.fetchrow(
            'SELECT user_id, guild_id, stripe_price FROM stripe_states WHERE state=$1', session_id
        )
        if data is None:
            log.error('Unable to find a matching session ID in store for %s', session_id)
            return Response('Success', status_code=200)

        user_id, guild_id = data['user_id'], data['guild_id']
        await request.app.state.pool.execute(
            'INSERT INTO patrons (user_id, guild_id, tier, subscribed_at) VALUES ($1, $2, $3, now()) ON CONFLICT (user_id, guild_id) DO NOTHING',
            user_id, guild_id, {v: k for k, v in config.stripe_prices.items()}[data['stripe_price']]
        )
        log.info(f'{Fore.GREEN}Payment complete for Discord user {user_id}{Fore.RESET}')

    return Response('Success', status_code=200)


@post("/checkout")
async def checkout(request: Request) -> Response:
    body = await request.json()
    user_id = body.get('user_id')
    guild_id = body.get('guild_id')
    price = body.get('price')
    if None in (user_id, guild_id, price):
        log.debug('Request missing required parameter')
        return Response({'error': 'Missing user_id'}, status_code=400)

    session_id = petname.generate(2, "-")
    assert isinstance(session_id, str)
    log.debug('Creating checkout session for user ID %s with session ID %s', user_id, session_id)
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[
                {
                    'price': price,
                    'quantity': 1,
                }
            ],
            mode='subscription',
            success_url='https://overseer-bot.net/guilds',
            cancel_url='https://overseer-bot.net',
            client_reference_id=session_id,
        )
        await request.app.state.pool.execute(
            'INSERT INTO stripe_states (user_id, guild_id, state, stripe_price) VALUES ($1, $2, $3, $4)',
            user_id, guild_id, session_id, price
        )
        log.debug('Successfully stored state for session')
        return Response({'url': session.url})
    except Exception as e:
        log.error('Error occurred during checkout creation')
        return Response({'error': str(e)}, status_code=400)


app = Litestar(
    [ping, checkout, webhook, success],
    path='/shop',
    on_startup=[create_pool],
    on_shutdown=[close_pool],
    exception_handlers={HTTP_404_NOT_FOUND: handle_404}
)
