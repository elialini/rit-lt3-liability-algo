"""LT3 strategy based on the supplied RIT Python Support file.

Install once, using the same Python that runs this file:
    python -m pip install requests
Run with the Windows RIT Client connected to LT3 and its API Orders enabled.
The connection settings below match the supplied working ALGO1 REST template;
if your client's API icon shows a different key or port, copy those here.

Run in Jupyter: %run strategy.py
Run in a terminal: python strategy.py
Running this file starts the trading strategy immediately; no mode argument.

Grading: target 50,000 bought + sold shares per round, then protect profit.
Volume counts this process's confirmed tenders and fills, never outstanding
orders or automatic liquidation. Start once per round with zero inventory.
Smaller positive margins are allowed before the volume minimum; these are
heuristics, not a guarantee of qualification, profit, or a particular grade.

Execution: small liquidity-aware clips, budgeted passive exits with deadlines,
volatility/depth stress on new tenders, and earlier closeout before the crowd.
Stop new tenders at 240, stop passive quotes at 250, aim to be flat by 270.
If liquidity prevents completion, continue controlled exits until the case ends.
Fill-price VWAP is printed only when the API supplies actual fill VWAP.
Order lookup 404s are reconciled against OPEN/TRANSACTED/CANCELLED lists;
an absent order is never assumed filled or cancelled. Diagnostics stay in the
console, including execution reasons and per-tender exposure/quote statistics.
No extra configuration, packages beyond requests, or output files are required.
"""
import requests
from collections import deque
from itertools import product
import math
import signal
import sys
from time import monotonic, sleep

# CONNECTION: matches the working RSM434 ALGO1 Client REST template.
# These are the local client's API settings, not the trading server login.
BASE_URL = 'http://localhost:9999/v1'
API_KEY = 'Rotman'


# LT3 case rules and strategy assumptions. All settings are in this file.
TICKERS = ('CRZY', 'TAME')
MAX_ORDER = {'CRZY': 25000, 'TAME': 10000}
NET_LIMIT = 100000
GROSS_LIMIT = 250000
FEE = 0.02
TENDER_FEE = 0.0  # Separate tender-leg fee is not specified in the case brief.
USABLE_DEPTH = 0.80
MIN_PROFIT = 50.0
BUFFER_PER_SHARE = 0.005
# Before qualifying, prefer earning some spread to missing the volume minimum.
# Keep the depth/fee/position safeguards even when lowering the profit buffer.
MIN_TRADED_VOLUME = 50000
QUALIFY_MIN_PROFIT = 10.0
QUALIFY_BUFFER_PER_SHARE = 0.0025
URGENT_BUFFER_PER_SHARE = 0.001
QUALIFY_URGENT_TICK = 180
EXPECTED_TENDERS = 5
STATUS_INTERVAL = 10.0
# Avoid the crowded final minute. These are conservative, tunable defaults.
STOP_ACCEPTING = 240
PASSIVE_END = 250
FLATTEN_TICK = 270
NORMAL_CLIP = {'CRZY': 5000, 'TAME': 2500}
URGENT_CLIP = {'CRZY': 10000, 'TAME': 5000}
PASSIVE_CLIP = 2000
PASSIVE_SHARE = 0.20  # At most this fraction may fill on resting-intent orders.
PASSIVE_TIME_SHARE = 0.30  # Total waiting budget, within the original deadline.
INITIAL_DERISK_SHARE = 0.15
TICK_SIZE = 0.01
PASSIVE_MAX_AGE = 3.5
AGGRESSIVE_MAX_AGE = 0.6
MIN_RISK_PER_SHARE = 0.01
MAX_WORK_SECONDS = 30.0
POLL_SECONDS = 0.15
ORDER_INTERVAL = 0.25
REQUEST_TIMEOUT = 3.0
MAX_SNAPSHOT_AGE = 0.5
ORDER_VISIBILITY_GRACE = 3.0
DECISION_INTERVAL = 5.0
shutdown = False


class ApiException(Exception):
    pass


class AuthenticationError(ApiException):
    pass


class HttpError(ApiException):
    def __init__(self, method, endpoint, status):
        self.status = status
        super().__init__('RIT rejected {} {}: HTTP {}. Check the RIT client for the reason.'.format(method, endpoint, status))


class UnknownWrite(ApiException):
    """A write may have happened. Never repeat it automatically."""


class RateLimited(ApiException):
    def __init__(self, wait):
        self.wait = max(0.05, min(float(wait), 5.0))
        super().__init__('RIT rate limit; waiting {:.2f}s'.format(self.wait))


def signal_handler(signum, frame):
    global shutdown
    if shutdown:
        raise KeyboardInterrupt
    shutdown = True
    print('\nStopping new tenders. The bot will close confirmed inventory while the case is active.')


def api(s, method, endpoint, **params):
    """Send requests through the same requests.Session used by the ALGO1 template."""
    try:
        resp = s.request(method, BASE_URL.rstrip('/') + '/' + endpoint.lstrip('/'),
                               params=params, timeout=REQUEST_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        if method != 'GET':
            raise UnknownWrite('Connection lost during {} {}. Its outcome is unknown; inspect RIT orders/positions before restarting.'.format(method, endpoint)) from exc
        raise ApiException('Cannot reach {}. Keep Windows RIT open and logged in, enable its API, and match BASE_URL to the local API port shown by its API icon. Run Python on the same Windows computer. Details: {}'.format(BASE_URL, type(exc).__name__)) from exc
    if resp.status_code in (401, 403):
        raise AuthenticationError('HTTP {}: match API_KEY at the top of strategy.py to the key shown by RIT\'s API icon; check API permissions. The client API key is separate from your student login.'.format(resp.status_code))
    if method != 'GET' and resp.status_code >= 500:
        raise UnknownWrite('RIT returned HTTP {} for {} {}. Inspect RIT before restarting; this write will not be repeated.'.format(resp.status_code, method, endpoint))
    if 300 <= resp.status_code < 400:
        raise ApiException('Unexpected redirect. Use the local API address shown by the RIT client.')
    # Some client builds return a non-JSON 404 body. Keep its HTTP status so
    # order reconciliation can recover without interpreting it as a fill.
    if not resp.ok and resp.status_code != 429:
        raise HttpError(method, endpoint, resp.status_code)
    try:
        data = resp.json()
    except ValueError as exc:
        error = UnknownWrite if method != 'GET' else ApiException
        raise error('Non-JSON response to {} {} (HTTP {}). Check the API port; a write with an unreadable response must not be repeated.'.format(method, endpoint, resp.status_code)) from exc
    if resp.status_code == 429:
        try:
            wait = float(resp.headers.get('Retry-After') or data.get('wait', 0.5))
            if not math.isfinite(wait):
                wait = 0.5
        except (ValueError, TypeError, AttributeError):
            wait = 0.5
        raise RateLimited(wait)
    return data


def integer(value):
    number = float(value)
    if not math.isfinite(number) or number != int(number):
        raise ValueError('RIT returned an invalid share quantity')
    return int(number)


def get_positions(session):
    rows = api(session, 'GET', '/securities')
    positions = {row['ticker']: integer(row['position']) for row in rows}
    if len(positions) != len(rows) or not set(TICKERS).issubset(positions):
        raise ValueError('The API responded, but this bot needs LT3 securities CRZY and TAME. Received: {}. Connect the RIT client to LT3, not ALGO1.'.format(', '.join(sorted(positions))))
    if any(q for t, q in positions.items() if t not in TICKERS):
        raise ValueError('Unexpected inventory outside CRZY/TAME')
    return {ticker: positions[ticker] for ticker in TICKERS}


def get_book(session, ticker, own_ids=()):
    raw = api(session, 'GET', '/securities/book', ticker=ticker, limit=1000)
    own_ids = {str(i) for i in own_ids}
    book = {}
    for plural, singular in (('bids', 'bid'), ('asks', 'ask')):
        if plural not in raw and singular not in raw:
            raise ValueError('RIT order book missing ' + plural)
        levels = {}
        for row in raw.get(plural, raw.get(singular, [])):
            if str(row.get('order_id')) in own_ids:
                continue
            if row.get('status', 'OPEN') != 'OPEN':
                continue
            qty = integer(row['quantity']) - integer(row.get('quantity_filled', 0))
            price = float(row['price'])
            if qty < 0 or not math.isfinite(price) or price <= 0:
                raise ValueError('Invalid RIT book quantity or price')
            if qty:
                levels[price] = levels.get(price, 0) + qty
        book[plural] = [{'price': price, 'quantity': levels[price], 'quantity_filled': 0}
                        for price in sorted(levels, reverse=(plural == 'bids'))]
    return book


def sweep(book, action, quantity, fraction=1.0):
    filled, notional, price = 0, 0.0, None
    for level in book['asks' if action == 'BUY' else 'bids']:
        available = math.floor((level['quantity'] - level.get('quantity_filled', 0)) * fraction)
        take = min(quantity - filled, available)
        if take:
            filled += take
            notional += take * level['price']
            price = level['price']
        if filled == quantity:
            break
    return filled, notional, price


def liquidation_value(book, position, fee, fraction=USABLE_DEPTH):
    if not position:
        return 0.0
    filled, notional, _ = sweep(book, 'SELL' if position > 0 else 'BUY', abs(position), fraction)
    if filled != abs(position):
        return None
    return (notional if position > 0 else -notional) - abs(position) * fee


def signed_tender(tender):
    qty = integer(tender['quantity'])
    if qty <= 0 or tender['action'] not in ('BUY', 'SELL'):
        raise ValueError('Invalid tender action/quantity')
    return qty if tender['action'] == 'BUY' else -qty


def within_limits(positions, net_limit=NET_LIMIT, gross_limit=GROSS_LIMIT):
    return abs(sum(positions.values())) <= net_limit and sum(abs(q) for q in positions.values()) <= gross_limit


def acceptance_policy(positions, tick, traded_volume, tenders_seen):
    if traded_volume >= MIN_TRADED_VOLUME:
        return MIN_PROFIT, BUFFER_PER_SHARE, 'Profit protection'
    if traded_volume + sum(abs(q) for q in positions.values()) >= MIN_TRADED_VOLUME:
        return MIN_PROFIT, BUFFER_PER_SHARE, 'Finish existing unwind'
    if tick >= QUALIFY_URGENT_TICK or tenders_seen >= EXPECTED_TENDERS:
        return QUALIFY_MIN_PROFIT, URGENT_BUFFER_PER_SHARE, 'Urgent volume target'
    return QUALIFY_MIN_PROFIT, QUALIFY_BUFFER_PER_SHARE, 'Build qualifying volume'


def evaluate_tender(tender, book, positions, tick, fee=FEE,
                    net_limit=NET_LIMIT, gross_limit=GROSS_LIMIT,
                    traded_volume=0, tenders_seen=0, depth_fraction=USABLE_DEPTH,
                    risk_per_share=MIN_RISK_PER_SHARE):
    """Incremental liquidation profit; never forecasts market direction."""
    if tender['ticker'] not in TICKERS or not tender.get('is_fixed_bid', True):
        return False, 0.0, 'Unsupported tender'
    if tick >= STOP_ACCEPTING or float(tender['expires']) - tick <= 2:
        return False, 0.0, 'Too late to accept'
    delta = signed_tender(tender)
    price = float(tender['price'])
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Invalid tender price')
    old = positions[tender['ticker']]
    new = old + delta
    projected = dict(positions)
    projected[tender['ticker']] = new
    if not within_limits(projected, net_limit, gross_limit):
        return False, 0.0, 'Portfolio limit'
    before, after = liquidation_value(book, old, fee, depth_fraction), liquidation_value(book, new, fee, depth_fraction)
    if before is None or after is None:
        return False, 0.0, 'Insufficient depth after liquidity allowance'
    profit = -delta * price - abs(delta) * TENDER_FEE + after - before
    minimum, buffer, phase = acceptance_policy(positions, tick, traded_volume, tenders_seen)
    required = minimum + max(0, abs(new) - abs(old)) * (buffer + risk_per_share)
    reason = '{}: {} (need ${:,.2f} after fees, including execution risk)'.format(
        phase, 'Enough spread' if profit >= required else 'Spread too small', required)
    return profit >= required, profit, reason


def pending_limits(positions, pending, net_limit=NET_LIMIT, gross_limit=GROSS_LIMIT):
    """Every subset of outstanding fills must respect portfolio limits."""
    orders = list(pending.values())
    for filled in product((False, True), repeat=len(orders)):
        projected = dict(positions)
        for apply, order in zip(filled, orders):
            if apply:
                leaves = order['quantity'] - order['filled']
                projected[order['ticker']] += leaves if order['action'] == 'BUY' else -leaves
        if not within_limits(projected, net_limit, gross_limit):
            return False
    return True


class MarketState:
    """Time-based observations: repeated reads do not count as elapsed time."""
    def __init__(self):
        self.history = deque(maxlen=80)
        self.latest = None

    def observe(self, book, now):
        if not book['bids'] or not book['asks']:
            self.latest = None
            return
        bid, ask = book['bids'][0]['price'], book['asks'][0]['price']
        if ask < bid:
            self.latest = None
            return
        sample = (now, (bid + ask) / 2, ask - bid,
                  sum(r['quantity'] for r in book['bids'][:3]),
                  sum(r['quantity'] for r in book['asks'][:3]))
        self.latest = sample
        if not self.history or now - self.history[-1][0] >= 0.2:
            self.history.append(sample)
        while self.history and now - self.history[0][0] > 10:
            self.history.popleft()

    def metrics(self):
        rows = list(self.history)
        warm = len(rows) >= 4 and rows[-1][0] - rows[0][0] >= 0.6
        sigma, churn = 0.01, 0.0  # Positive fallback until observations are credible.
        if warm:
            elapsed = rows[-1][0] - rows[0][0]
            sigma = max(0.003, math.sqrt(sum((c[1] - p[1]) ** 2 for p, c in zip(rows, rows[1:])) / elapsed))
            recent = [r for r in rows if r[0] >= rows[-1][0] - 3]
            churn = max([max(0, 1 - c[k] / max(1, p[k]))
                         for p, c in zip(recent, recent[1:]) for k in (3, 4)] or [0])
        return {'warm': warm, 'sigma': sigma, 'churn': churn,
                'mid': self.latest[1] if self.latest else None,
                'spread': self.latest[2] if self.latest else TICK_SIZE}


class Strategy:
    def __init__(self, session):
        self.session = session
        self.expected = {ticker: 0 for ticker in TICKERS}
        self.pending, self.plans = {}, {}
        self.markets = {ticker: MarketState() for ticker in TICKERS}
        self.handled, self.seen_tenders = set(), set()
        self.decisions = {}
        self.decision_times, self.tender_details = {}, {}
        self.tender_volume, self.fill_volume = 0, 0
        self.last_status, self.last_submit = -math.inf, -math.inf
        self.last_change = {ticker: -math.inf for ticker in TICKERS}
        self.last_tick, self.round_key, self.active_seen = -1, None, False
        self.round_changed = False
        self.reported_minimum, self.mismatch_since = False, None
        self.last_reporting_phase = None
        self.fee, self.net_limit, self.gross_limit = FEE, NET_LIMIT, GROSS_LIMIT
        self.caps, self.order_interval = dict(MAX_ORDER), ORDER_INTERVAL
        self.api_latency = 0.03
        self.completed = []
        self.use_order_lists = False
        self.order_404s, self.order_list_recoveries = 0, 0
        self.last_case = {'tick': 0, 'status': 'STOPPED'}

    @property
    def traded_volume(self):
        return self.tender_volume + self.fill_volume

    def report_status(self, case, force=False):
        reached = self.traded_volume >= MIN_TRADED_VOLUME
        now = monotonic()
        tick = float(case['tick'])
        phase_key = (case['status'], tick >= STOP_ACCEPTING, tick >= PASSIVE_END, tick >= FLATTEN_TICK, shutdown)
        if (not force and now - self.last_status < STATUS_INTERVAL and
                reached == self.reported_minimum and phase_key == self.last_reporting_phase):
            return
        self.last_status, self.reported_minimum = now, reached
        self.last_reporting_phase = phase_key
        state = 'MINIMUM REACHED' if reached else '{:,} MORE NEEDED'.format(MIN_TRADED_VOLUME - self.traded_volume)
        phase = 'Small clips / selective passive exits'
        if tick >= STOP_ACCEPTING or shutdown:
            phase = 'No new tenders'
        if tick >= PASSIVE_END or shutdown:
            phase += '; aggressive exits only'
        print('Tick {:g} {} | Volume {:,}/{:,} ({}) | Inventory {} | {} open orders | {}'.format(
            tick, case['status'], self.traded_volume, MIN_TRADED_VOLUME, state,
            self.expected, len(self.pending), phase))
        if not reached and tick >= QUALIFY_URGENT_TICK:
            print('VOLUME WARNING: minimum not reached. Only confirmed trades count; no speculative volume trades.')
        if tick >= FLATTEN_TICK and any(self.expected.values()):
            print('CLOSEOUT WARNING: past target tick {}; continuing controlled exits while the case is active.'.format(FLATTEN_TICK))

    def start(self):
        case = api(self.session, 'GET', '/case')
        if case.get('ticks_per_period', 300) != 300 or case.get('total_periods', 1) != 1:
            raise ValueError('This strategy expects one 300-tick LT3 period')
        if any(get_positions(self.session).values()) or api(self.session, 'GET', '/orders', status='OPEN'):
            raise ValueError('Start with zero positions and no open orders. This process only manages its accepted tenders.')
        for row in api(self.session, 'GET', '/securities'):
            ticker = row['ticker']
            if ticker in TICKERS:
                self.caps[ticker] = min(self.caps[ticker], integer(row.get('max_trade_size') or self.caps[ticker]))
                if self.caps[ticker] <= 0 or (row.get('min_trade_size') or 1) > 1:
                    raise ValueError('Unsupported server order-size settings')
                self.fee = max(self.fee, float(row.get('trading_fee') or 0))
                rate = float(row.get('api_orders_per_second') or 0)
                delay = float(row.get('execution_delay_ms') or 0) / 1000
                self.order_interval = max(self.order_interval, 1 / rate if rate > 0 else 0, delay)
        for limit in api(self.session, 'GET', '/limits'):
            self.net_limit = min(self.net_limit, integer(limit['net_limit']))
            self.gross_limit = min(self.gross_limit, integer(limit['gross_limit']))
        if self.net_limit <= 0 or self.gross_limit <= 0:
            raise ValueError('Invalid server portfolio limits')
        self.round_key = (case.get('name'), case.get('period', 1))
        self.last_case = case
        print('Trading LT3. Ctrl+C requests liquidation; press again to exit immediately.')
        print('No new tenders after {}; passive orders end at {}; target flat by {}.'.format(STOP_ACCEPTING, PASSIVE_END, FLATTEN_TICK))
        print('Volume counts only this process. Start once per round with zero inventory; earlier trades are not included.')
        self.report_status(case, force=True)

    def current_case(self):
        case = api(self.session, 'GET', '/case')
        tick = float(case['tick'])
        if (case.get('name'), case.get('period', 1)) != self.round_key or tick < self.last_tick:
            self.round_changed = True
            raise ValueError('Case reset detected. Start a fresh process for the new round.')
        if case.get('status') not in ('ACTIVE', 'PAUSED', 'STOPPED'):
            raise ValueError('Unknown case status')
        self.last_tick, self.last_case = tick, case
        return case

    def read_book(self, ticker):
        started = monotonic()
        book = get_book(self.session, ticker, [o['id'] for o in self.pending.values()])
        received = monotonic()
        self.api_latency = 0.8 * self.api_latency + 0.2 * (received - started)
        book['observed_at'] = started  # Include the request time in freshness.
        self.markets[ticker].observe(book, received)
        return book

    def fresh(self, book):
        return monotonic() - book['observed_at'] <= MAX_SNAPSHOT_AGE

    def dispatch_seconds(self):
        return max(0.75, self.order_interval + 0.3, 8 * self.api_latency)

    def work_seconds(self, ticker, quantity):
        return min(MAX_WORK_SECONDS, max(12, math.ceil(quantity / min(NORMAL_CLIP[ticker], self.caps[ticker])) * self.dispatch_seconds() + 3))

    def evaluate(self, tender, book, positions, tick):
        ticker = tender['ticker']
        self.tender_details.pop(tender['tender_id'], None)
        if ticker not in TICKERS:
            return False, 0.0, 'Unsupported ticker'
        if positions[ticker] or ticker in self.pending:
            return False, 0.0, 'Finish this stock\'s existing tender first'
        metrics = self.markets[ticker].metrics()
        if not metrics['warm'] or metrics['mid'] is None:
            return False, 0.0, 'Collecting recent liquidity/volatility observations'
        projected = dict(positions)
        projected[ticker] += signed_tender(tender)
        if not pending_limits(projected, self.pending, self.net_limit, self.gross_limit):
            return False, 0.0, 'Portfolio limits including possible pending fills'
        chunks = sum(math.ceil(abs(q) / min(NORMAL_CLIP[t], self.caps[t])) for t, q in projected.items())
        time_needed = chunks * self.dispatch_seconds() + 4
        if tick + time_needed >= FLATTEN_TICK:
            return False, 0.0, 'Insufficient execution capacity before early closeout'
        horizon = self.work_seconds(ticker, abs(projected[ticker]))
        risk = max(MIN_RISK_PER_SHARE, 1.5 * metrics['sigma'] * math.sqrt(horizon) + metrics['spread'] * 0.1)
        fraction = max(0.35, USABLE_DEPTH - 0.4 * metrics['churn'])
        self.tender_details[tender['tender_id']] = 'risk ${:.4f}/share; usable depth {:.0%}; horizon {:.1f}s; sigma {:.4f}'.format(risk, fraction, horizon, metrics['sigma'])
        return evaluate_tender(tender, book, positions, tick, self.fee,
                               self.net_limit, self.gross_limit, self.traded_volume,
                               len(self.seen_tenders), fraction, risk)

    def cancel_order(self, ticker, reason='requested cancellation'):
        order = self.pending.get(ticker)
        if not order or order.get('cancel_attempted'):
            return
        order['cancel_attempted'] = True
        order['cancel_time'] = monotonic()
        plan = self.plans.get(ticker)
        if plan:
            plan['cancels'] = plan.get('cancels', 0) + 1
        print('Cancel {} {}: {} (age {:.2f}s; confirmed fill {:,}/{:,}).'.format(
            ticker, order['id'], reason, monotonic() - order['time'], order['filled'], order['quantity']))
        try:
            result = api(self.session, 'DELETE', '/orders/{}'.format(order['id']))
        except (AuthenticationError, RateLimited):
            order['cancel_attempted'] = False  # A definite rejection can be retried later.
            raise
        except HttpError as exc:
            if exc.status != 404:
                raise
            # It may have finished between lookup and cancellation. A 404 is
            # not a successful cancel; require an authoritative terminal row.
            self.reconcile_orders()
            return
        if result.get('success') is not True:
            # The order may have finished between our poll and cancellation.
            # Final status/fills are authoritative; never infer an unfilled cancel.
            self.reconcile_orders()
            if ticker not in self.pending:
                return
            return  # Keep the reservation until final status or bounded timeout.
        order['cancelled'] = True
        # No replacement until a later GET confirms final cumulative fills.

    def apply_order_row(self, ticker, order, row):
        if (integer(row['order_id']) != order['id'] or row['ticker'] != ticker or
                row['action'] != order['action'] or integer(row['quantity']) != order['quantity'] or
                (self.round_key and row.get('period', self.round_key[1]) != self.round_key[1])):
            raise ValueError('RIT order does not match submitted order')
        filled = integer(row['quantity_filled'])
        if not order['filled'] <= filled <= order['quantity']:
            raise ValueError('Inconsistent order fill count')
        if row['status'] not in ('OPEN', 'TRANSACTED', 'CANCELLED'):
            raise ValueError('Unknown order status')
        if row['status'] == 'TRANSACTED' and filled != order['quantity']:
            raise ValueError('A transacted order has incomplete fills')
        try:
            vwap = float(row.get('vwap'))
            if not math.isfinite(vwap) or vwap <= 0:
                vwap = None
        except (ValueError, TypeError):
            vwap = None
        delta = filled - order['filled']
        self.expected[ticker] += delta if order['action'] == 'BUY' else -delta
        self.fill_volume += delta
        plan = self.plans.get(ticker)
        if delta:
            self.last_change[ticker] = monotonic()
            if plan:
                plan['filled'] += delta
                if order['mode'] == 'PASSIVE':
                    plan['resting_fills'] += delta
                if vwap is None:
                    plan['prices_known'] = False
                else:
                    total = filled * vwap
                    plan['notional'] += total - order.get('notional', 0)
                    order['notional'] = total
        order['filled'] = filled
        order.pop('missing_since', None)
        if row['status'] in ('TRANSACTED', 'CANCELLED'):
            if plan and order['mode'] == 'PASSIVE':
                plan['passive_seconds'] = plan.get('passive_seconds', 0.0) + max(0, monotonic() - order['time'])
            del self.pending[ticker]
            return
        if order.get('cancel_attempted') and monotonic() - order.get('cancel_time', order['time']) > max(10, self.order_interval + 3 * REQUEST_TIMEOUT):
            raise ValueError('Cancellation remains unsettled. Inspect RIT orders/positions.')

    def reconcile_orders(self):
        """Count cumulative fills once; missing status always keeps the reservation."""
        lists = {}
        complete = True
        for ticker, order in list(self.pending.items()):
            row = order.pop('terminal_response', None)
            if row is None and not self.use_order_lists:
                try:
                    row = api(self.session, 'GET', '/orders/{}'.format(order['id']))
                except HttpError as exc:
                    if exc.status != 404:
                        raise
                    self.order_404s += 1
                    self.use_order_lists = True
                    print('Order lookup returned 404. Switching to RIT order lists for this round; fills still require confirmation.')
            if row is None:
                # /orders defaults to OPEN. Explicitly request final states too;
                # absence from OPEN alone proves neither a fill nor a cancellation.
                for status in ('OPEN', 'TRANSACTED', 'CANCELLED'):
                    if status not in lists:
                        rows = api(self.session, 'GET', '/orders', status=status)
                        lists[status] = {integer(r['order_id']): r for r in rows}
                    row = lists[status].get(order['id'])
                    if row is not None:
                        self.order_list_recoveries += 1
                        break
            if row is None:
                if 'missing_since' not in order:
                    order['missing_since'] = monotonic()
                    print('Waiting for order {} {} to appear in RIT; holding its share reservation.'.format(order['id'], ticker))
                if monotonic() - order['missing_since'] >= max(ORDER_VISIBILITY_GRACE, self.order_interval + 1):
                    raise ValueError('Order {} remains missing from all RIT order lists. Its fills are unknown; inspect RIT.'.format(order['id']))
                complete = False
                continue
            self.apply_order_row(ticker, order, row)
        return complete

    def finish_plans(self, tick):
        for ticker, plan in list(self.plans.items()):
            if self.expected[ticker] or ticker in self.pending:
                continue
            if plan['filled'] != plan['initial']:
                raise ValueError('Closed inventory does not match tender unwind volume')
            result = dict(plan, ticker=ticker, seconds=tick - plan['started_tick'])
            detail = 'actual fill VWAP unavailable from API'
            if plan['prices_known']:
                vwap = plan['notional'] / plan['filled']
                gross = (vwap - plan['tender_price']) if plan['action'] == 'SELL' else (plan['tender_price'] - vwap)
                result['vwap'], result['pnl'] = vwap, (gross - self.fee - TENDER_FEE) * plan['filled']
                shortfall = (plan['benchmark'] - vwap) if plan['action'] == 'SELL' else (vwap - plan['benchmark'])
                detail = 'fill VWAP {:.4f}; estimated net P&L ${:,.2f}; shortfall vs arrival sweep {:.4f}/share'.format(vwap, result['pnl'], shortfall)
            print('Closed tender {} {}: {:,} shares in {:g} ticks; {:,} fills on resting-intent orders; {}.'.format(
                plan['id'], ticker, plan['filled'], result['seconds'], plan['resting_fills'], detail))
            print('  Execution: {} orders; {} cancels; {:,} shares posted passively; {:.2f}s passive wait; reasons {}.'.format(
                plan.get('orders', 0), plan.get('cancels', 0), plan.get('passive_posted', 0),
                plan.get('passive_seconds', 0), plan.get('mode_reasons', {})))
            print('  Exposure: sampled average {:,.0f} shares; worst observed adverse midpoint move {:.4f}/share. Resting-intent fills are not an exchange maker classification.'.format(
                plan.get('inventory_ticks', 0) / max(1, result['seconds']), plan.get('worst_adverse', 0)))
            self.completed.append(result)
            del self.plans[ticker]

    def passive_wait(self, ticker):
        plan, order = self.plans[ticker], self.pending.get(ticker)
        live = max(0, monotonic() - order['time']) if order and order['mode'] == 'PASSIVE' else 0
        return plan.get('passive_seconds', 0.0) + live

    def execution_decision(self, ticker, tick):
        plan = self.plans[ticker]
        metrics = self.markets[ticker].metrics()
        remaining = abs(self.expected[ticker])
        duration = max(1, plan['deadline'] - plan['started_tick'])
        progress = max(0, min(1, (tick - plan['started_tick']) / duration))
        direction = 1 if self.expected[ticker] > 0 else -1
        adverse = 0 if metrics['mid'] is None else direction * (plan['arrival_mid'] - metrics['mid'])
        threshold = max(0.02, min(plan['expected_profit'] / plan['initial'] * 0.25, plan['risk']))
        fragile = (not metrics['warm'] or metrics['mid'] is None or metrics['churn'] >= 0.5 or
                   metrics['sigma'] > max(0.015, metrics['spread'] * 0.75))
        emergency = shutdown or tick >= PASSIVE_END or tick >= plan['deadline'] - 2
        passive_left = max(0, math.floor(plan['initial'] * PASSIVE_SHARE) - plan['resting_fills'])
        budget = plan.get('passive_budget_seconds', min(8.0, duration * PASSIVE_TIME_SHARE))
        eligible = (passive_left > 0 and self.passive_wait(ticker) < budget and
                    progress < 0.65 and tick < PASSIVE_END)
        # The active portion has its own schedule. The bounded passive reserve
        # does not count as falling behind until its opportunity window closes.
        reserve = passive_left if eligible else 0
        target = (plan['initial'] - reserve) * (1 - progress) + reserve
        behind = remaining > target + min(NORMAL_CLIP[ticker], self.caps[ticker])
        derisk = plan['filled'] < plan['initial'] * INITIAL_DERISK_SHARE
        if emergency:
            reason = 'shutdown' if shutdown else 'closeout clock' if tick >= PASSIVE_END else 'tender deadline'
        elif adverse >= threshold:
            reason = 'adverse price move'
        elif fragile:
            reason = 'unstable book'
        elif derisk:
            reason = 'initial risk reduction'
        elif behind:
            reason = 'active schedule catching up'
        elif not eligible:
            reason = 'passive budget/window finished'
        else:
            reason = 'bounded passive opportunity'
        return reason != 'bounded passive opportunity', emergency, fragile, reason

    def aggressive(self, ticker, book, tick):
        return self.execution_decision(ticker, tick)[:3]

    def passive_lifetime(self, ticker):
        metrics = self.markets[ticker].metrics()
        # Shorter quote life as price noise increases relative to the spread.
        return min(PASSIVE_MAX_AGE, max(1.0, (max(TICK_SIZE, metrics['spread'] * 0.5) /
                                             (1.5 * metrics['sigma'])) ** 2))

    def observe_execution(self, tick):
        for ticker, plan in self.plans.items():
            previous = plan.get('exposure_tick', plan['started_tick'])
            plan['inventory_ticks'] = plan.get('inventory_ticks', 0.0) + abs(self.expected[ticker]) * max(0, tick - previous)
            plan['exposure_tick'] = tick

    def manage_pending(self, books, tick):
        for ticker, order in list(self.pending.items()):
            if order.get('cancel_attempted'):
                continue
            age = monotonic() - order['time']
            book = books[ticker]
            aggressive, emergency, fragile, mode_reason = self.execution_decision(ticker, tick)
            metrics = self.markets[ticker].metrics()
            reason = None
            if order['mode'] == 'PASSIVE':
                # Keep queue position while the quote remains competitive. A
                # two-cent move by itself no longer destroys a useful quote.
                if aggressive:
                    reason = mode_reason
                elif book['bids'] and book['asks']:
                    bid, ask = book['bids'][0]['price'], book['asks'][0]['price']
                    crossed = order['price'] <= bid if order['action'] == 'SELL' else order['price'] >= ask
                    gap = order['price'] - ask if order['action'] == 'SELL' else bid - order['price']
                    if crossed:
                        reason = 'quote overtaken by market'
                    elif gap > 2 * TICK_SIZE + 1e-8 and age >= 1.0:
                        reason = 'quote no longer competitive'
                if reason is None and age >= self.passive_lifetime(ticker):
                    reason = 'quote age limit'
            else:
                if age >= max(AGGRESSIVE_MAX_AGE, self.order_interval + 0.1):
                    reason = 'unfilled active clip refresh'
            if not self.fresh(book) or metrics['mid'] is None:
                reason = 'missing or stale book'
            if reason:
                self.cancel_order(ticker, reason)

    def submit_exit(self, ticker, book, case):
        if ticker in self.pending or not self.expected[ticker] or not self.fresh(book):
            return False
        if monotonic() - self.last_submit < self.order_interval:
            return False
        tick = float(case['tick'])
        if case['status'] != 'ACTIVE' or tick >= 300:
            return False
        position, net = self.expected[ticker], sum(self.expected.values())
        action = 'SELL' if position > 0 else 'BUY'
        reserved = sum(o['quantity'] - o['filled'] for o in self.pending.values() if o['action'] == action)
        room = self.net_limit + net - reserved if action == 'SELL' else self.net_limit - net - reserved
        quantity = max(0, min(abs(position), self.caps[ticker], room))
        if not quantity:
            return False
        aggressive, emergency, fragile, reason = self.execution_decision(ticker, tick)
        mode = 'AGGRESSIVE' if aggressive else 'PASSIVE'
        opposing = book['bids' if action == 'SELL' else 'asks']
        if not opposing:
            return False
        if mode == 'PASSIVE':
            if not book['bids'] or not book['asks']:
                return False
            bid, ask = book['bids'][0]['price'], book['asks'][0]['price']
            if ask - bid < TICK_SIZE - 1e-8:
                return False
            passive_left = math.floor(self.plans[ticker]['initial'] * PASSIVE_SHARE) - self.plans[ticker]['resting_fills']
            quantity = min(quantity, PASSIVE_CLIP, passive_left, max(1, int(opposing[0]['quantity'] * 0.1)))
            improvement = TICK_SIZE if ask - bid >= 3 * TICK_SIZE - 1e-8 else 0
            price = ask - improvement if action == 'SELL' else bid + improvement
        else:
            cap = URGENT_CLIP[ticker] if emergency else NORMAL_CLIP[ticker]
            quantity = min(quantity, cap)
            # Restrict ordinary clips to nearby liquidity. Urgent exits may walk
            # further, always at explicit prices read from the current book.
            collar = max(2 * TICK_SIZE, self.markets[ticker].metrics()['spread'])
            near = [r for r in opposing if abs(r['price'] - opposing[0]['price']) <= collar + 1e-8]
            fraction = 0.1 if fragile else 0.2
            if emergency:
                fraction = 0.5
                near = opposing
            depth = sum(r['quantity'] for r in near)
            quantity = min(quantity, max(1, int(depth * fraction)))
            chosen = dict(book)
            chosen['bids' if action == 'SELL' else 'asks'] = near
            quantity, _, price = sweep(chosen, action, quantity)
            if not quantity:
                return False
        price = round(price, 2)
        order = {'id': None, 'ticker': ticker, 'action': action, 'quantity': integer(quantity),
                 'filled': 0, 'price': price, 'time': monotonic(), 'cancelled': False,
                 'cancel_attempted': False, 'mode': mode,
                 'arrival_mid': self.markets[ticker].metrics()['mid'] or price, 'notional': 0}
        prospective = dict(self.pending, **{ticker: order})
        if not pending_limits(self.expected, prospective, self.net_limit, self.gross_limit):
            return False
        # Refresh case/positions after the book read; other pending orders may
        # have filled. A mismatch defers submission until the next reconciliation.
        fresh_case = self.current_case()
        if fresh_case['status'] != 'ACTIVE' or float(fresh_case['tick']) >= 300:
            return False
        if mode == 'PASSIVE' and (shutdown or float(fresh_case['tick']) >= PASSIVE_END):
            return False
        if mode == 'PASSIVE' and self.execution_decision(ticker, float(fresh_case['tick']))[0]:
            return False
        if get_positions(self.session) != self.expected or not self.fresh(book):
            return False
        row = api(self.session, 'POST', '/orders', ticker=ticker, action=action,
                  type='LIMIT', quantity=order['quantity'], price=price)
        if 'order_id' not in row:
            raise UnknownWrite('Submitted order returned no ID. Inspect RIT before restarting.')
        order['id'] = integer(row['order_id'])
        # A complete final POST response is already authoritative. Avoid a
        # redundant lookup for an already confirmed final result.
        if row.get('status') in ('TRANSACTED', 'CANCELLED') and all(k in row for k in ('ticker', 'action', 'quantity', 'quantity_filled')):
            order['terminal_response'] = dict(row)
        self.pending[ticker] = order
        plan = self.plans[ticker]
        plan['orders'] = plan.get('orders', 0) + 1
        reasons = plan.setdefault('mode_reasons', {})
        reasons[reason] = reasons.get(reason, 0) + 1
        if mode == 'PASSIVE':
            plan['passive_posted'] = plan.get('passive_posted', 0) + quantity
        self.last_submit = self.last_change[ticker] = monotonic()
        print('{} {} {:,} {} at {:.2f} | {}'.format(mode, action, quantity, ticker, price, reason))
        return True

    def accept_fresh(self, tender):
        ticker = tender['ticker']
        if ticker in self.pending or self.expected[ticker]:
            return False
        current = next((t for t in api(self.session, 'GET', '/tenders') if t['tender_id'] == tender['tender_id']), None)
        if current != tender:
            return False
        if not self.reconcile_orders():
            return False
        if get_positions(self.session) != self.expected:
            return False
        self.validate_open_orders()
        book = self.read_book(ticker)
        case = self.current_case()
        tick = float(case['tick'])
        if case['status'] != 'ACTIVE' or shutdown or tick >= STOP_ACCEPTING:
            return False
        accept, profit, reason = self.evaluate(tender, book, self.expected, tick)
        if not accept or not self.fresh(book):
            print('Tender {} deferred at final check: {}.'.format(tender['tender_id'], reason if not accept else 'book became stale'))
            return False
        delta = signed_tender(tender)
        action = 'SELL' if delta > 0 else 'BUY'
        _, notional, _ = sweep(book, action, abs(delta))
        result = api(self.session, 'POST', '/tenders/{}'.format(tender['tender_id']), price=tender['price'])
        if result.get('success') is not True:
            raise UnknownWrite('Tender acceptance was not confirmed. Inspect RIT before restarting.')
        metrics = self.markets[ticker].metrics()
        horizon = self.work_seconds(ticker, abs(delta))
        self.expected[ticker] += delta
        self.tender_volume += abs(delta)
        self.handled.add(tender['tender_id'])
        self.last_change[ticker] = monotonic()
        self.plans[ticker] = {'id': tender['tender_id'], 'initial': abs(delta), 'filled': 0,
            'started_tick': tick, 'deadline': min(FLATTEN_TICK, tick + horizon),
            'arrival_mid': metrics['mid'], 'tender_price': float(tender['price']), 'action': action,
            'expected_profit': profit, 'risk': max(MIN_RISK_PER_SHARE, 1.5 * metrics['sigma'] * math.sqrt(horizon)),
            'benchmark': notional / abs(delta), 'resting_fills': 0, 'notional': 0, 'prices_known': True,
            'passive_budget_seconds': min(8.0, horizon * PASSIVE_TIME_SHARE),
            'passive_seconds': 0.0, 'inventory_ticks': 0.0, 'exposure_tick': tick, 'worst_adverse': 0.0}
        print('Accepted tender {}: estimated ${:,.0f} after fees; target exit by tick {:g}.'.format(
            tender['tender_id'], profit, self.plans[ticker]['deadline']))
        self.report_status(case, force=True)
        return True

    def validate_open_orders(self):
        known = {o['id'] for o in self.pending.values()}
        if any(integer(o['order_id']) not in known for o in api(self.session, 'GET', '/orders', status='OPEN')):
            raise ValueError('Untracked open order. Run only one bot and avoid manual trades.')

    def step(self):
        case = self.current_case()
        tick = float(case['tick'])
        self.report_status(case)
        self.observe_execution(tick)
        if not self.reconcile_orders():
            return True  # No new exposure or replacement while fills are unknown.
        if (case['status'] == 'STOPPED' and self.active_seen) or tick >= 300:
            self.finish_plans(tick)
            self.report_status(case, force=True)
            if self.pending or any(self.expected.values()) or any(get_positions(self.session).values()):
                raise ValueError('Case ended with unfinished inventory/orders. Automatic closeout does not count as bot volume.')
            print('Order tracking: {} lookup 404s; {} status confirmations from order lists.'.format(self.order_404s, self.order_list_recoveries))
            print('Finished flat. Restart the process for the next round.')
            return False
        if case['status'] != 'ACTIVE':
            return not (shutdown and not self.pending and not any(self.expected.values()))
        self.active_seen = True
        if get_positions(self.session) != self.expected:
            if self.mismatch_since is None:
                self.mismatch_since = monotonic()
            if monotonic() - self.mismatch_since > max(4, self.order_interval + REQUEST_TIMEOUT):
                raise ValueError('Positions do not match confirmed tenders/fills. Inspect RIT.')
            return True
        self.mismatch_since = None
        self.validate_open_orders()
        if not pending_limits(self.expected, self.pending, self.net_limit, self.gross_limit):
            raise ValueError('Portfolio or possible pending fills breach limits')
        self.finish_plans(tick)
        if shutdown and not self.pending and not any(self.expected.values()):
            self.report_status(case, force=True)
            print('Stopped with zero positions.')
            return False
        books = {t: self.read_book(t) for t in TICKERS}
        for ticker, plan in self.plans.items():
            mid = self.markets[ticker].metrics()['mid']
            if mid is not None:
                direction = 1 if plan['action'] == 'SELL' else -1
                plan['worst_adverse'] = max(plan['worst_adverse'], direction * (plan['arrival_mid'] - mid))
        self.manage_pending(books, tick)
        # Per-stock states let one passive order rest while the other stock exits.
        order_of_work = sorted(TICKERS, key=lambda t: (self.plans.get(t, {}).get('deadline', math.inf), -abs(self.expected[t])))
        for ticker in order_of_work:
            if self.expected[ticker] and ticker not in self.pending:
                self.submit_exit(ticker, books[ticker], case)
        # Do not pile on exposure if existing inventory can finish qualification.
        working_to_qualify = (self.traded_volume < MIN_TRADED_VOLUME <=
                             self.traded_volume + sum(abs(q) for q in self.expected.values()))
        if not shutdown and tick < STOP_ACCEPTING and not working_to_qualify:
            tenders = api(self.session, 'GET', '/tenders')
            self.seen_tenders.update(t['tender_id'] for t in tenders if t['ticker'] in TICKERS)
            candidates = []
            for tender in tenders:
                ticker = tender['ticker']
                if ticker not in TICKERS or tender['tender_id'] in self.handled:
                    continue
                book = books[ticker]
                if not self.fresh(book):
                    continue
                accept, profit, reason = self.evaluate(tender, book, self.expected, tick)
                # Avoid flooding output as the continuously estimated risk changes.
                key = (accept, reason.split(' (need')[0])
                if (self.decisions.get(tender['tender_id']) != key or
                        monotonic() - self.decision_times.get(tender['tender_id'], -math.inf) >= DECISION_INTERVAL):
                    print('Tender {}: {} | estimate ${:,.0f} | {} | {}'.format(tender['tender_id'], ticker, profit, reason,
                        self.tender_details.get(tender['tender_id'], 'no executable estimate yet')))
                    self.decisions[tender['tender_id']] = key
                    self.decision_times[tender['tender_id']] = monotonic()
                if accept:
                    candidates.append((profit, tender))
            if candidates:
                self.accept_fresh(max(candidates, key=lambda item: item[0])[1])
        return True

    def cleanup_orders(self):
        """Best-effort cancellation of known orders on any exit; never replay an uncertain write."""
        if self.round_changed:
            print('Case changed: old order IDs are not used in the new round. Inspect RIT.')
            return
        for ticker in list(self.pending):
            try:
                self.cancel_order(ticker)
            except (ApiException, ValueError, KeyError, TypeError) as exc:
                print('Could not confirm cancellation of {}: {}'.format(ticker, exc))
        if self.pending:
            try:
                self.reconcile_orders()
            except (ApiException, ValueError, KeyError, TypeError) as exc:
                print('Could not reconcile final orders: {}'.format(exc))
        if self.pending or any(self.expected.values()):
            print('Inspect RIT: remaining inventory {} and {} unconfirmed orders.'.format(self.expected, len(self.pending)))

def main():
    global shutdown
    shutdown = False
    previous_handler = signal.signal(signal.SIGINT, signal_handler)
    try:
        # Same URL and authentication header as the working ALGO1 template.
        with requests.Session() as s:
            s.headers.update({'X-API-Key': API_KEY})
            print('Connecting to {} ...'.format(BASE_URL))
            strategy = Strategy(s)
            try:
                strategy.start()
                errors = 0
                while True:
                    try:
                        if not strategy.step():
                            return 0
                        errors = 0
                    except RateLimited as exc:
                        print(exc)
                        sleep(exc.wait)
                        continue  # Make a fresh decision, never replay a stale request.
                    except (AuthenticationError, UnknownWrite):
                        raise
                    except ApiException as exc:
                        errors += 1
                        print(exc)
                        if errors >= 5:
                            raise ApiException('Repeated API failures. Inspect and manage any open orders/positions in RIT.')
                        sleep(min(2.0, errors * 0.25))
                    sleep(POLL_SECONDS)
            finally:
                strategy.cleanup_orders()
    except (ApiException, ValueError, KeyError, TypeError) as exc:
        print('STOPPED: {}'.format(exc), file=sys.stderr)
        print('Inspect RIT for open orders or remaining positions before restarting.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Exited. Inspect RIT for any orders or positions still needing attention.')
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_handler)


if __name__ == '__main__':
    result = main()
    # A completed notebook cell should return normally, without SystemExit.
    if 'ipykernel' not in sys.modules:
        sys.exit(result)
