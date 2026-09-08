from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context, send_from_directory
import argparse
import json
import os
import random
import secrets
from functools import wraps
import redis

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# Initialize Redis client
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_DB = int(os.environ.get('REDIS_DB', 0))
ROOM_ID = os.environ.get('ROOM_ID', 'dataset_a')  # Distinguishes Data Set A vs Data Set B

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

# --- GM password resolution ---
# Precedence: --gm-password command-line flag > gm_password in a credentials
# file (--credentials-file, or ./credentials.json next to this script) >
# hardcoded default. This runs at import time (not just under
# `if __name__ == '__main__'`) so GM_PASSWORD is set correctly whether the
# app is launched directly with `python savageinit-new.py` or imported by a
# WSGI server like gunicorn.
DEFAULT_CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')

def load_gm_password():
    # parse_known_args() so this doesn't choke on unrelated args a WSGI
    # server (gunicorn, flask run, etc.) may have been invoked with.
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--gm-password', dest='gm_password', default=None)
    parser.add_argument('--credentials-file', dest='credentials_file', default=None)
    args, _ = parser.parse_known_args()

    if args.gm_password:
        return args.gm_password

    credentials_path = args.credentials_file or DEFAULT_CREDENTIALS_FILE
    if os.path.isfile(credentials_path):
        try:
            with open(credentials_path, 'r') as f:
                creds = json.load(f)
            password = creds.get('gm_password')
            if password:
                return password
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read GM password from {credentials_path}: {e}")

    return 'gamemaster'

GM_PASSWORD = load_gm_password()

# --- Redis Storage Keys ---
KEY_DECK = f"session:{ROOM_ID}:deck"
KEY_PARTICIPANTS = f"session:{ROOM_ID}:participants"
KEY_JOKER = f"session:{ROOM_ID}:joker_drawn"
CHANNEL_UPDATES = f"channel:{ROOM_ID}:updates"
LOCK_KEY = f"lock:{ROOM_ID}:state"

# How long a request may hold the state lock before it's force-expired
# (protects against a crashed worker leaving the lock held forever).
LOCK_TIMEOUT = 10
# How long a request will wait to acquire the lock before giving up.
LOCK_BLOCKING_TIMEOUT = 5

class Card:
    SUITS = ['Spades', 'Hearts', 'Diamonds', 'Clubs']
    RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    
    def __init__(self, suit, rank):
        self.suit = suit
        self.rank = rank
        
    def value(self):
        if self.rank == 'Joker':
            return 15
        elif self.rank == 'A':
            return 14
        elif self.rank == 'K':
            return 13
        elif self.rank == 'Q':
            return 12
        elif self.rank == 'J':
            return 11
        else:
            return int(self.rank)
    
    def suit_value(self):
        if self.rank == 'Joker':
            return 4
        suit_order = {'Spades': 3, 'Hearts': 2, 'Diamonds': 1, 'Clubs': 0}
        return suit_order.get(self.suit, -1)
    
    def __repr__(self):
        if self.rank == 'Joker':
            return "Joker"
        return f"{self.rank} of {self.suit}"
    
    def to_dict(self):
        return {
            'rank': self.rank,
            'suit': self.suit,
            'display': str(self),
            'value': self.value(),
            'suit_value': self.suit_value()
        }

def generate_full_deck():
    cards = []
    for suit in Card.SUITS:
        for rank in Card.RANKS:
            cards.append(Card(suit, rank).to_dict())
    cards.append(Card('', 'Joker').to_dict())
    cards.append(Card('', 'Joker').to_dict())
    random.shuffle(cards)
    return cards

# --- Redis Helpers ---
def get_state():
    deck_json = r.get(KEY_DECK)
    participants_json = r.get(KEY_PARTICIPANTS)
    joker_val = r.get(KEY_JOKER)

    if deck_json is None:
        deck = generate_full_deck()
        r.set(KEY_DECK, json.dumps(deck))
    else:
        deck = json.loads(deck_json)

    if participants_json is None:
        participants = []
        r.set(KEY_PARTICIPANTS, json.dumps(participants))
    else:
        participants = json.loads(participants_json)

    joker_drawn = joker_val == 'true' if joker_val else False

    return deck, participants, joker_drawn

def save_state(deck, participants, joker_drawn):
    r.set(KEY_DECK, json.dumps(deck))
    r.set(KEY_PARTICIPANTS, json.dumps(participants))
    r.set(KEY_JOKER, 'true' if joker_drawn else 'false')

def broadcast_update(deck, participants):
    """Broadcast a state snapshot. Callers must pass the exact deck/participants
    they just wrote via save_state(), so the broadcast can't race ahead of or
    behind the write and doesn't need an extra Redis round trip to refetch it."""
    data = {
        'participants': serialize_participants(participants),
        'deck_remaining': len(deck)
    }
    r.publish(CHANNEL_UPDATES, json.dumps(data))

def serialize_participants(participants):
    serialized = []
    for p in participants:
        serialized.append({
            'name': p['name'],
            'traits': p.get('traits', []),
            'trait_display': p.get('trait_display'),
            'has_drawn': p.get('has_drawn'),
            'cards': p.get('cards', []),
            'additional_cards': p.get('additional_cards', []),        
            'active_card': p.get('active_card'),
            'on_hold': p.get('on_hold', False),
            'held_joker': p.get('held_joker', False),
            'is_hidden': p.get('is_hidden', False)
        })
    return serialized

def gm_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_gm'):
            return jsonify({'error': 'GM authentication required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def with_state_lock(f):
    """Serialize read-modify-write access to this room's state.

    Every mutating route does get_state() -> mutate in Python -> save_state(),
    which is not atomic on its own: two concurrent requests for the same room
    could both read the same state, and whichever saves last would silently
    clobber the other's change. This wraps the whole route body in a
    Redis-backed lock scoped to ROOM_ID so only one request per room can be
    in that read-modify-write section at a time.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        lock = r.lock(LOCK_KEY, timeout=LOCK_TIMEOUT, blocking_timeout=LOCK_BLOCKING_TIMEOUT)
        acquired = lock.acquire(blocking=True)
        if not acquired:
            return jsonify({'error': 'Server is busy processing another update for this room. Please try again.'}), 503
        try:
            return f(*args, **kwargs)
        finally:
            try:
                lock.release()
            except redis.exceptions.LockError:
                # Lock already expired (e.g. request ran longer than LOCK_TIMEOUT)
                # or was released elsewhere; nothing more to do.
                pass
    return decorated_function

# --- Routes ---
@app.route('/')
def index():
    return render_template('initiative.html')

@app.route('/stream')
def stream():
    def event_stream():
        pubsub = r.pubsub()
        pubsub.subscribe(CHANNEL_UPDATES)
        try:
            # Initial state payload
            deck, participants, _ = get_state()
            initial_data = {
                'participants': serialize_participants(participants),
                'deck_remaining': len(deck)
            }
            yield f"data: {json.dumps(initial_data)}\n\n"

            # Poll with a timeout instead of pubsub.listen(), which blocks
            # indefinitely with no output. Without a periodic heartbeat,
            # reverse proxies/load balancers will silently kill "idle"
            # connections during quiet stretches between updates, and a
            # disconnected client's subscription won't get cleaned up until
            # the next published message wakes the generator back up.
            while True:
                message = pubsub.get_message(timeout=15)
                if message is None:
                    # No update within the timeout window - send a heartbeat
                    # to keep the connection alive and let the WSGI server
                    # notice a dropped client.
                    yield ": ping\n\n"
                elif message['type'] == 'message':
                    yield f"data: {message['data']}\n\n"
                # Other message types (e.g. the 'subscribe' confirmation)
                # are ignored and we just loop back around.
        except GeneratorExit:
            pass
        finally:
            try:
                pubsub.unsubscribe(CHANNEL_UPDATES)
                pubsub.close()
            except Exception:
                pass

    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')

@app.route('/check_auth')
def check_auth():
    return jsonify({'is_gm': session.get('is_gm', False)})

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    if data.get('password') == GM_PASSWORD:
        session['is_gm'] = True
        return jsonify({'success': True})
    return jsonify({'success': False})

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('is_gm', None)
    return jsonify({'success': True})

@app.route('/get_participants')
@gm_required
def get_participants():
    _, participants, _ = get_state()
    return jsonify({'participants': [p.copy() for p in participants]})

@app.route('/update_name', methods=['POST'])
@gm_required
@with_state_lock
def update_participant_name():
    deck, participants, joker_drawn = get_state()
    data = request.json
    index = data.get('index')
    new_name = data.get('name')

    if 0 <= index < len(participants):
        if any(p['name'] == new_name for i, p in enumerate(participants) if i != index):
            return jsonify({'error': 'That name is already in use.'}), 400
        
        participants[index]['name'] = new_name
        save_state(deck, participants, joker_drawn)
        broadcast_update(deck, participants)
        return jsonify({'success': True})

    return jsonify({'error': 'Invalid participant index'}), 400

@app.route('/update_traits', methods=['POST'])
@gm_required
@with_state_lock
def update_participant_traits():
    deck, participants, joker_drawn = get_state()
    data = request.json
    index = data.get('index')
    new_traits = data.get('traits', [])
    
    if 0 <= index < len(participants):
        participants[index]['traits'] = new_traits
        participants[index]['trait_display'] = get_traits_display(new_traits)
        
        if participants[index]['cards']:
            cards = participants[index]['cards']
            additional_cards = participants[index]['additional_cards']
            participants[index]['active_card'] = determine_active_card(cards, new_traits, additional_cards)
            
            def initiative_sort_key(p):
                if p.get('on_hold'):
                    return (1, 0, 0)
                if p.get('active_card'):
                    return (0, -p['active_card']['value'], -p['active_card']['suit_value'])
                return (2, 0, 0)

            participants.sort(key=initiative_sort_key)
        
        save_state(deck, participants, joker_drawn)
        broadcast_update(deck, participants)
        return jsonify({'success': True})

    return jsonify({'error': 'Invalid participant index'}), 400

@app.route('/next_round', methods=['POST'])
@gm_required
@with_state_lock
def next_round():
    deck, participants, joker_drawn = get_state()

    if joker_drawn:
        deck = generate_full_deck()
        joker_drawn = False 
    
    total_needed = sum(
        cards_needed_for_traits(p['traits'])
        for p in participants
        if p.get('name') and not p.get('on_hold')
    )
    
    deck, ok = replenish_deck_if_needed(deck, participants, total_needed)
    if not ok:
        return jsonify({'error': 'Not enough cards available. Too many cards are currently active.'}), 400

    for p in participants:
        if not p.get('name'):
            continue

        if p.get('on_hold'):
            p['held_joker'] = False
            continue
            
        p['cards'] = []
        p['active_card'] = None
        p['additional_cards'] = []

        deck, cards_drawn = draw_for_participant(deck, participants, p['traits'])

        if cards_drawn is None:
            save_state(deck, participants, joker_drawn)
            broadcast_update(deck, participants)
            return jsonify({'error': 'Not enough cards available. Too many cards are currently active.'}), 400

        if cards_drawn:
            p['has_drawn'] = True
            if any(c['rank'] == 'Joker' for c in cards_drawn):
                joker_drawn = True
            p['cards'] = cards_drawn
            p['active_card'] = determine_active_card(p['cards'], p['traits'], p['additional_cards'])

    def next_round_sort_key(p):
        if p.get('on_hold'):
            return (1, 0, 0)
        if p.get('active_card'):
            return (0, -p['active_card']['value'], -p['active_card']['suit_value'])
        return (2, 0, 0)

    participants.sort(key=next_round_sort_key)
    save_state(deck, participants, joker_drawn)
    broadcast_update(deck, participants)
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/reset_deck', methods=['POST'])
@gm_required
@with_state_lock
def reset_deck():
    _, participants, _ = get_state()
    deck = generate_full_deck()
    joker_drawn = False
    
    for p in participants:
        p['cards'] = []
        p['active_card'] = None
        p['additional_cards'] = []
        p['has_drawn'] = False
        p['held_joker'] = False
        p['on_hold'] = False

    save_state(deck, participants, joker_drawn)
    broadcast_update(deck, participants)
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/clear_initiative', methods=['POST'])
@gm_required
@with_state_lock
def clear_initiative():
    deck = generate_full_deck()
    participants = []
    joker_drawn = False
    save_state(deck, participants, joker_drawn)
    broadcast_update(deck, participants)
    return jsonify({'participants': []})

@app.route('/remove_participant', methods=['POST'])
@gm_required
@with_state_lock
def remove_participant():
    deck, participants, joker_drawn = get_state()
    data = request.json
    index = data.get('index')
    if 0 <= index < len(participants):
        participants.pop(index)
    save_state(deck, participants, joker_drawn)
    broadcast_update(deck, participants)
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/draw_additional', methods=['POST'])
@gm_required
@with_state_lock
def draw_additional():
    deck, participants, joker_drawn = get_state()
    data = request.json
    index = data.get('index')
    
    if 0 <= index < len(participants):
        if participants[index].get('on_hold'):
            return jsonify({'error': 'Participant is on Hold'}), 400
        if count_active_cards(participants) >= 54:
            return jsonify({'error': 'Not enough cards available. Too many cards are currently active.'}), 400
        
        if len(deck) == 0:
            return jsonify({'error': 'Not enough cards available. Too many cards are currently active.'}), 400
            
        additional_card = deck.pop()
        card_dict = additional_card
        participants[index]['cards'].append(card_dict)
        
        if card_dict['rank'] == 'Joker':
            joker_drawn = True
        
        if 'additional_cards' not in participants[index]:
            participants[index]['additional_cards'] = []
        participants[index]['additional_cards'].append(card_dict)
        
        p = participants[index]
        p['active_card'] = determine_active_card(p['cards'], p['traits'], p['additional_cards'])
        participants[index]['has_drawn'] = True
    
    def initiative_sort_key(p):
        if p.get('on_hold'):
            return (1, 0, 0)
        if p.get('active_card'):
            return (0, -p['active_card']['value'], -p['active_card']['suit_value'])
        return (2, 0, 0)

    participants.sort(key=initiative_sort_key)
    save_state(deck, participants, joker_drawn)
    broadcast_update(deck, participants)
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/deal_in', methods=['POST'])
@gm_required
@with_state_lock
def deal_in():
    deck, participants, joker_drawn = get_state()
    data = request.json
    name = data.get('name')
    traits = data.get('traits', [])

    if not name:
        return jsonify({'error': 'Participant name required'}), 400
    
    existing = next((p for p in participants if p['name'] == name), None)

    if existing:
        if existing.get('has_drawn'):
            return jsonify({'error': 'Participant already dealt in'}), 400
        if existing.get('on_hold'):
            return jsonify({'error': 'Participant is on Hold'}), 400
        
        existing['traits'] = traits
        existing['trait_display'] = get_traits_display(traits)
        deck, cards = draw_for_participant(deck, participants, traits)
        if cards is None:
            return jsonify({'error': 'Not enough cards available. Too many cards are currently active.'}), 400
        existing['cards'] = cards
        existing['active_card'] = determine_active_card(cards, traits, [])
        existing['has_drawn'] = True

        if any(card['rank'] == 'Joker' for card in cards):
            joker_drawn = True

    else:
        deck, cards = draw_for_participant(deck, participants, traits)
        if cards is None:
            return jsonify({'error': 'Not enough cards available. Too many cards are currently active.'}), 400
        participant = {
            'name': name,
            'traits': traits,
            'cards': cards,
            'active_card': determine_active_card(cards, traits, []),
            'trait_display': get_traits_display(traits),
            'additional_cards': [],
            'has_drawn': True,
            'on_hold': False,
            'is_hidden': False
        }

        if any(card['rank'] == 'Joker' for card in cards):
            joker_drawn = True

        participants.append(participant)

    def initiative_sort_key(p):
        if p.get('on_hold'):
            return (1, 0, 0)
        if p.get('active_card'):
            return (0, -p['active_card']['value'], -p['active_card']['suit_value'])
        return (2, 0, 0)

    participants.sort(key=initiative_sort_key)
    save_state(deck, participants, joker_drawn)
    broadcast_update(deck, participants)
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/get_initiative')
def get_initiative():
    _, participants, _ = get_state()
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/deck_info')
def deck_info():
    deck, _, _ = get_state()
    return jsonify({'remaining': len(deck)})

def count_active_cards(participants):
    return sum(len(p.get('cards', [])) for p in participants)

def replenish_deck_if_needed(deck, participants, cards_needed):
    if len(deck) >= cards_needed:
        return deck, True

    active = count_active_cards(participants)
    if (54 - active) < cards_needed:
        return deck, False

    active_cards = [c for p in participants for c in p.get('cards', [])]
    new_deck = generate_full_deck()

    jokers_to_remove = sum(1 for ac in active_cards if ac['rank'] == 'Joker')
    for ac in active_cards:
        if ac['rank'] == 'Joker':
            continue
        for i, c in enumerate(new_deck):
            if c['rank'] == ac['rank'] and c['suit'] == ac['suit']:
                new_deck.pop(i)
                break

    removed = 0
    i = 0
    while i < len(new_deck) and removed < jokers_to_remove:
        if new_deck[i]['rank'] == 'Joker':
            new_deck.pop(i)
            removed += 1
        else:
            i += 1

    return new_deck, True

def cards_needed_for_traits(traits):
    if 'improved_level_headed' in traits:
        return 3
    elif 'level_headed' in traits or 'hesitant' in traits:
        return 2
    return 1

def draw_for_participant(deck, participants, traits):
    num_cards = cards_needed_for_traits(traits)
    original_deck = list(deck)  # Backup the deck state

    deck, ok = replenish_deck_if_needed(deck, participants, num_cards)
    if not ok:
        return original_deck, None

    drawn = [deck.pop() for _ in range(min(num_cards, len(deck)))]

    if 'quick' in traits and drawn:
        first_card = drawn[0]
        first_val = first_card['value']
        if first_val <= 5 and first_card['rank'] != 'Joker':
            deck, ok = replenish_deck_if_needed(deck, participants, 1)
            if not ok:
                return original_deck, None  # Restore the backup to prevent data loss
            if deck:
                drawn.append(deck.pop())

    return deck, drawn

def determine_active_card(cards, traits, additional_cards):
    if not cards:
        return None
    if additional_cards:
        initial_cards = [c for c in cards if c not in additional_cards]
        if initial_cards:
            current_active = get_active_from_initial(initial_cards, traits)
            best_additional = max(additional_cards, key=lambda c: (c['value'], c['suit_value']))
            if (best_additional['value'], best_additional['suit_value']) > (current_active['value'], current_active['suit_value']):
                return best_additional
            return current_active
    return get_active_from_initial(cards, traits)

def get_active_from_initial(cards, traits):
    if not cards:
        return None
    jokers = [c for c in cards if c['rank'] == 'Joker']
    if jokers:
        return jokers[0]
    if 'level_headed' in traits or 'improved_level_headed' in traits:
        return max(cards, key=lambda c: (c['value'], c['suit_value']))
    elif 'hesitant' in traits:
        return min(cards, key=lambda c: (c['value'], c['suit_value']))
    elif 'quick' in traits:
        if len(cards) == 2 and cards[0]['value'] <= 5 and cards[0]['rank'] != 'Joker':
            return max(cards[0], cards[1], key=lambda c: (c['value'], c['suit_value']))
        return cards[0]
    else:
        return cards[0]

def get_traits_display(traits):
    trait_names = {
        'level_headed': 'Level Headed',
        'improved_level_headed': 'Improved Level Headed',
        'quick': 'Quick',
        'hesitant': 'Hesitant'
    }
    return ', '.join([trait_names.get(t, t) for t in traits]) if traits else ''

@app.route('/toggle_hidden', methods=['POST'])
@gm_required
@with_state_lock
def toggle_hidden():
    deck, participants, joker_drawn = get_state()
    data = request.json
    index = data.get('index')

    if not (0 <= index < len(participants)):
        return jsonify({'error': 'Invalid participant index'}), 400

    p = participants[index]
    p['is_hidden'] = not p.get('is_hidden', False)
    save_state(deck, participants, joker_drawn)
    broadcast_update(deck, participants)
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/toggle_hold', methods=['POST'])
@gm_required
@with_state_lock
def toggle_hold():
    deck, participants, joker_drawn = get_state()
    data = request.json
    index = data.get('index')

    if not (0 <= index < len(participants)):
        return jsonify({'error': 'Invalid participant index'}), 400

    p = participants[index]

    if not p.get('on_hold') and not p.get('has_drawn'):
        return jsonify({'error': 'Participant has not drawn cards yet'}), 400

    p['on_hold'] = not p.get('on_hold', False)

    if p['on_hold']:
        p['held_joker'] = any(c.get('rank') == 'Joker' for c in p.get('cards', []))
    else:
        p['held_joker'] = False

    p['cards'] = []
    p['active_card'] = None
    p['additional_cards'] = []
    p['has_drawn'] = False

    def initiative_sort_key(p):
        if p.get('on_hold'):
            return (1, 0, 0)
        if p.get('active_card'):
            return (0, -p['active_card']['value'], -p['active_card']['suit_value'])
        return (2, 0, 0)

    participants.sort(key=initiative_sort_key)
    save_state(deck, participants, joker_drawn)
    broadcast_update(deck, participants)
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/add_participant_placeholder', methods=['POST'])
@gm_required
@with_state_lock
def add_participant_placeholder():
    deck, participants, joker_drawn = get_state()
    
    name = "New Participant"
    original_name = name
    counter = 1
    temp_name = original_name
    while any(p['name'] == temp_name for p in participants):
        temp_name = f"{original_name} {counter}"
        counter += 1
    name = temp_name

    new_participant = {
        'name': name,
        'traits': [],
        'cards': [],
        'active_card': None,
        'trait_display': '',
        'additional_cards': [],
        'has_drawn': False,
        'on_hold': False,
        'is_hidden': False
    }
    participants.append(new_participant)
    save_state(deck, participants, joker_drawn)
    broadcast_update(deck, participants)
    return jsonify({'success': True, 'participant': new_participant})

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'SW_LOGO_FP_2018_ICON.ico', mimetype='image/x-icon')

if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get('PORT', 5000)), host='0.0.0.0', threaded=True)