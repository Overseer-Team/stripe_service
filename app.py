import json
import logging
import secrets
import sys

import stripe
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
        event = stripe.Webhook.construct_event(payload, sig_header, config.signing_secret)
        log.debug(f"Processed webhook event: {event.type}")
        log.debug(str(event))
    except ValueError:
        return Response("Invalid payload", status_code=400)
    except stripe.SignatureVerificationError:
        return Response("Invalid signature", status_code=400)

    rev_prices = {v: k for k, v in config.stripe_prices.items()}

    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            match event.type:
                case 'checkout.session.completed':
                    sess = event.data.object
                    if sess.get('payment_status') != 'paid':
                        return Response('Unpaid session', status_code=200)

                    ref = sess.get('client_reference_id')
                    if not ref:
                        return Response('Missing state', status_code=200)

                    state = await conn.fetchrow(
                        'SELECT user_id, guild_id, stripe_price FROM stripe_states WHERE state=$1',
                        ref,
                    )
                    if not state:
                        return Response('Orphan state', status_code=200)

                    tier = rev_prices.get(state['stripe_price'])
                    if not tier:
                        return Response('Unknown price', status_code=200)

                    log.info(f'{Fore.GREEN}Payment complete for Discord user {state['user_id']}{Fore.RESET}')
                    await conn.execute(
                        """INSERT INTO patrons (user_id, guild_id, customer_id, tier, subscribed_at)
                           VALUES($1, $2, $3, $4, now())
                           ON CONFLICT (user_id, guild_id)
                           DO UPDATE SET customer_id=EXCLUDED.customer_id, tier=EXCLUDED.tier
                        """, state['user_id'], state['guild_id'], sess['customer'], tier
                    )

                case 'customer.subscription.updated' | 'customer.subscription.created':
                    sub = event.data.object
                    if not sub['items']['data']:
                        return Response('No items', status_code=200)
                    price_id = sub['items']['data'][0]['price']['id']
                    tier = rev_prices.get(price_id)
                    if tier:
                        log.info(f'{Fore.GREEN}Payment complete for customer {sub['customer']}{Fore.RESET}')
                        await conn.execute('UPDATE patrons SET tier=$1 WHERE customer_id=$2', tier, sub['customer'])

                case 'customer.subscription.deleted':
                    sub = event.data.object
                    await conn.execute('DELETE FROM patrons WHERE customer_id=$1', sub['customer'])

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
    if price not in config.stripe_prices.values():
        return Response({'error': 'Invalid price'}, status_code=400)

    session_id = secrets.token_urlsafe(16)
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
