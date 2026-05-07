from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
from queue import Queue
import threading
import json
from functools import wraps
import random
import secrets

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# SSE message queues for broadcasting updates
message_queues = []
message_queues_lock = threading.Lock()

# Simple GM password (in production, use proper authentication)
GM_PASSWORD = "gamemaster"

class Card:
    SUITS = ['Spades', 'Hearts', 'Diamonds', 'Clubs']
    RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    
    def __init__(self, suit, rank):
        self.suit = suit
        self.rank = rank
        
    def value(self):
        """Return numeric value for sorting"""
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
        """Return suit value for sorting (Spades > Hearts > Diamonds > Clubs)"""
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

class Deck:
    def __init__(self):
        self.cards = []
        for suit in Card.SUITS:
            for rank in Card.RANKS:
                self.cards.append(Card(suit, rank))
        self.cards.append(Card('', 'Joker'))
        self.cards.append(Card('', 'Joker'))
        self.shuffle()
    
    def shuffle(self):
        random.shuffle(self.cards)
    
    def draw(self, n=1):
        drawn = []
        for _ in range(min(n, len(self.cards))):
            if len(self.cards) == 0:
                break
            drawn.append(self.cards.pop())
        return drawn
    
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

# Global state
deck = Deck()
participants = []
joker_drawn = False

def gm_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_gm'):
            return jsonify({'error': 'GM authentication required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def broadcast_update():
    """Broadcast state update to all connected clients"""
    data = {
        'participants': serialize_participants(participants),
        'deck_remaining': len(deck.cards)
    }
    message = f"data: {json.dumps(data)}\n\n"

    with message_queues_lock:
        dead_queues = []
        for q in message_queues:
            try:
                q.put_nowait(message)
            except:
                dead_queues.append(q)
        for q in dead_queues:
            message_queues.remove(q)


@app.route('/')
def index():
    return render_template('initiative.html')

@app.route('/stream')
def stream():
    def event_stream():
        q = Queue()
        with message_queues_lock:
            message_queues.append(q)
        try:
            # Send initial state
            initial_data = {
                'participants': serialize_participants(participants),
                'deck_remaining': len(deck.cards)
            }
            yield f"data: {json.dumps(initial_data)}\n\n"

            # Keep connection alive and send updates
            while True:
                try:
                    message = q.get(timeout=15)
                    yield message
                except Exception:
                    # heartbeat to prevent buffering/timeout
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            with message_queues_lock:
                if q in message_queues:
                    message_queues.remove(q)

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
    return jsonify({'participants': [p.copy() for p in participants]})

@app.route('/update_name', methods=['POST'])
@gm_required
def update_participant_name():
    global participants
    data = request.json
    index = data.get('index')
    new_name = data.get('name')

    if 0 <= index < len(participants):
        old_name = participants[index]['name']
        
        # Check for name uniqueness among all other participants
        if any(p['name'] == new_name for i, p in enumerate(participants) if i != index):
            # If the name is a duplicate, alert the user and do not update
            return jsonify({'error': 'That name is already in use.'}), 400
        
        participants[index]['name'] = new_name
        broadcast_update()
        return jsonify({'success': True})

    return jsonify({'error': 'Invalid participant index'}), 400

@app.route('/update_traits', methods=['POST'])
@gm_required
def update_participant_traits():
    global participants
    data = request.json
    index = data.get('index')
    new_traits = data.get('traits', [])
    
    if 0 <= index < len(participants):
        participants[index]['traits'] = new_traits
        participants[index]['trait_display'] = get_traits_display(new_traits)
        
        # If the participant has cards, recalculate their active card based on new traits
        if participants[index]['cards']:
            cards = participants[index]['cards']
            additional_cards = participants[index]['additional_cards']
            participants[index]['active_card'] = determine_active_card(cards, new_traits, additional_cards)
            
            # Re-sort the initiative list if traits were changed while initiative is active
            def initiative_sort_key(p):
                if p.get('on_hold'):
                    return (1, 0, 0)
                if p.get('active_card'):
                    return (0, -p['active_card']['value'], -p['active_card']['suit_value'])
                return (2, 0, 0)

            participants.sort(key=initiative_sort_key)
        
        broadcast_update()
        return jsonify({'success': True})

    return jsonify({'error': 'Invalid participant index'}), 400

@app.route('/next_round', methods=['POST'])
@gm_required
def next_round():
    global participants, deck, joker_drawn

    if joker_drawn:
        deck = Deck()
        joker_drawn = False 
    
    # Pre-flight: verify we can draw for all non-held participants before
    # clearing any existing cards. This prevents partial state on error.
    total_needed = sum(
        cards_needed_for_traits(p['traits'])
        for p in participants
        if p.get('name') and not p.get('on_hold')
    )
    if not replenish_deck_if_needed(total_needed):
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

        cards_drawn = draw_for_participant(p['traits'])

        if cards_drawn is None:
            # Should not happen after a successful pre-flight, but guard anyway
            broadcast_update()
            return jsonify({'error': 'Not enough cards available. Too many cards are currently active.'}), 400

        if cards_drawn:
            p['has_drawn'] = True
            if any(c['rank'] == 'Joker' for c in cards_drawn):
                joker_drawn = True
            p['cards'] = cards_drawn
            p['active_card'] = determine_active_card(p['cards'], p['traits'], p['additional_cards'])


    # Update the global joker flag  
    # Sort participants: drawn cards first (by card value), then on-hold, then undrawn
    def next_round_sort_key(p):
        if p.get('on_hold'):
            return (1, 0, 0)
        if p.get('active_card'):
            return (0, -p['active_card']['value'], -p['active_card']['suit_value'])
        return (2, 0, 0)

    participants.sort(key=next_round_sort_key)
    
    broadcast_update()
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/reset_deck', methods=['POST'])
@gm_required
def reset_deck():
    global participants, deck, joker_drawn
    data = request.json
    participants_data = data.get('participants', [])
    
    # Reset deck to 54 cards and shuffle
    deck = Deck()
    joker_drawn = False
    
    # The client-side logic for reset_deck also sends participants, 
    # but since the global list is authoritative, we don't rebuild it here.
    # We only clear cards for the existing global participants (as done in new_encounter)
    for p in participants:
        p['cards'] = []
        p['active_card'] = None
        p['additional_cards'] = []
        p['has_drawn'] = False
        p['held_joker'] = False
        p['on_hold'] = False

    
    broadcast_update()
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/clear_initiative', methods=['POST'])
@gm_required
def clear_initiative():
    global deck, participants, joker_drawn
    deck = Deck()
    participants = []
    joker_drawn = False
    broadcast_update()
    return jsonify({'participants': []})

@app.route('/remove_participant', methods=['POST'])
@gm_required
def remove_participant():
    global participants
    data = request.json
    index = data.get('index')
    if 0 <= index < len(participants):
        participants.pop(index)
    broadcast_update()
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/draw_additional', methods=['POST'])
@gm_required
def draw_additional():
    global participants, deck, joker_drawn
    data = request.json
    index = data.get('index')
    
    if 0 <= index < len(participants):
        if participants[index].get('on_hold'):
            return jsonify({'error': 'Participant is on Hold'}), 400
        if count_active_cards() >= 54:
            return jsonify({'error': 'Not enough cards available. Too many cards are currently active.'}), 400
        additional_card = deck.draw(1)
        if not additional_card:
            return jsonify({'error': 'Not enough cards available. Too many cards are currently active.'}), 400
        card_dict = additional_card[0].to_dict()
        participants[index]['cards'].append(card_dict)
        
        # Check for joker
        if card_dict['rank'] == 'Joker':
            joker_drawn = True
        
        # Track this as an additional card
        if 'additional_cards' not in participants[index]:
            participants[index]['additional_cards'] = []
        participants[index]['additional_cards'].append(card_dict)
        
        # Recalculate active card using the standard logic helper
        p = participants[index]
        p['active_card'] = determine_active_card(p['cards'], p['traits'], p['additional_cards'])

        # Mark participant as having drawn
        participants[index]['has_drawn'] = True
    
    # Re-sort: drawn cards first (by card value desc), then on-hold, then undrawn
    def initiative_sort_key(p):
        if p.get('on_hold'):
            return (1, 0, 0)
        if p.get('active_card'):
            return (0, -p['active_card']['value'], -p['active_card']['suit_value'])
        return (2, 0, 0)

    participants.sort(key=initiative_sort_key)
    
    broadcast_update()
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/deal_in', methods=['POST'])
@gm_required
def deal_in():
    global participants, deck, joker_drawn
    data = request.json
    name = data.get('name')
    traits = data.get('traits', [])

    if not name:
        return jsonify({'error': 'Participant name required'}), 400
    
    # Look for existing participant
    existing = next((p for p in participants if p['name'] == name), None)

    if existing:
        if existing.get('has_drawn'):
            return jsonify({'error': 'Participant already dealt in'}), 400
        
        if existing.get('on_hold'):
            return jsonify({'error': 'Participant is on Hold'}), 400
        
        # Update traits and draw cards
        existing['traits'] = traits
        existing['trait_display'] = get_traits_display(traits)
        cards = draw_for_participant(traits)
        if cards is None:
            return jsonify({'error': 'Not enough cards available. Too many cards are currently active.'}), 400
        existing['cards'] = cards
        existing['active_card'] = determine_active_card(cards, traits, [])
        existing['has_drawn'] = True

        if any(card['rank'] == 'Joker' for card in cards):
            joker_drawn = True

    else:
        # New participant
        cards = draw_for_participant(traits)
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

    # Sort: drawn cards first (by card value desc), then on-hold, then undrawn
    def initiative_sort_key(p):
        if p.get('on_hold'):
            return (1, 0, 0)
        if p.get('active_card'):
            return (0, -p['active_card']['value'], -p['active_card']['suit_value'])
        return (2, 0, 0)

    participants.sort(key=initiative_sort_key)

    broadcast_update()
    return jsonify({'participants': serialize_participants(participants)})




@app.route('/get_initiative')
def get_initiative():
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/deck_info')
def deck_info():
    return jsonify({'remaining': len(deck.cards)})

def count_active_cards():
    """Count all cards currently assigned to participants."""
    total = 0
    for p in participants:
        total += len(p.get('cards', []))
    return total

def replenish_deck_if_needed(cards_needed):
    """If the deck has fewer cards than needed, silently reshuffle all
    unassigned cards back in. Returns False if even after replenishing
    there are not enough cards (i.e. too many active cards)."""
    if len(deck.cards) >= cards_needed:
        return True
    # Count cards currently held by participants
    active = count_active_cards()
    total_available = 54 - active
    if total_available < cards_needed:
        return False
    # Rebuild deck from scratch and remove active cards
    active_cards = []
    for p in participants:
        active_cards.extend(p.get('cards', []))
    new_deck = Deck()  # creates and shuffles a full 54-card deck
    # Remove active non-Joker cards by rank+suit match
    # Remove active Jokers by count (both are identical: rank='Joker', suit='')
    jokers_to_remove = sum(1 for ac in active_cards if ac['rank'] == 'Joker')
    for ac in active_cards:
        if ac['rank'] == 'Joker':
            continue  # handled separately below
        for i, c in enumerate(new_deck.cards):
            if c.rank == ac['rank'] and c.suit == ac['suit']:
                new_deck.cards.pop(i)
                break
    removed = 0
    i = 0
    while i < len(new_deck.cards) and removed < jokers_to_remove:
        if new_deck.cards[i].rank == 'Joker':
            new_deck.cards.pop(i)
            removed += 1
        else:
            i += 1
    deck.cards = new_deck.cards
    return True

def cards_needed_for_traits(traits):
    """Return the base number of cards a participant is entitled to
    given their traits. Does not account for Quick's conditional redraw."""
    if 'improved_level_headed' in traits:
        return 3
    elif 'level_headed' in traits or 'hesitant' in traits:
        return 2
    return 1

def draw_for_participant(traits):
    """Draw cards based on traits. Returns None if the deck cannot be
    replenished enough to fulfil the draw (too many active cards)."""
    num_cards = 1

    # Determine base number of cards to draw
    if 'improved_level_headed' in traits:
        num_cards = 3
    elif 'level_headed' in traits:
        num_cards = 2
    elif 'hesitant' in traits:
        num_cards = 2

    if not replenish_deck_if_needed(num_cards):
        return None

    cards = deck.draw(num_cards)

    # Handle Quick trait
    if 'quick' in traits and cards:
        first_card = cards[0]
        if first_card.value() <= 5 and first_card.rank != 'Joker':
            if not replenish_deck_if_needed(1):
                return None
            additional = deck.draw(1)
            if additional:
                cards.extend(additional)

    return [card.to_dict() for card in cards]

def determine_active_card(cards, traits, additional_cards):
    """Determine which card is active based on traits and additional cards"""
    if not cards:
        return None
    
    # If there are additional cards, check if any is better than current active
    if additional_cards:
        # Find the current active card (without considering additional cards)
        initial_cards = [c for c in cards if c not in additional_cards]
        if initial_cards:
            current_active = get_active_from_initial(initial_cards, traits)
            
            # Check if any additional card is better
            best_additional = max(additional_cards, key=lambda c: (c['value'], c['suit_value']))
            
            if (best_additional['value'], best_additional['suit_value']) > \
               (current_active['value'], current_active['suit_value']):
                return best_additional
            
            return current_active
    
    # No additional cards, use normal logic
    return get_active_from_initial(cards, traits)

def get_active_from_initial(cards, traits):
    """
    Determine the active initiative card based on the specified SWADE trait precedence:
    Joker > Level Headed/Improved Level Headed > Hesitant > Quick/Default.
    """
    if not cards:
        return None
    
    # 1. Joker Precedence: If a Joker is drawn, it supersedes all other rules.
    jokers = [c for c in cards if c['rank'] == 'Joker']
    if jokers:
        return jokers[0]
    
    # 2. Level Headed/Improved Level Headed: Use the highest card from all drawn cards.
    if 'level_headed' in traits or 'improved_level_headed' in traits:
        return max(cards, key=lambda c: (c['value'], c['suit_value']))
    
    # 3. Hesitant: Use the worst card (Joker check handled above).
    elif 'hesitant' in traits:
        return min(cards, key=lambda c: (c['value'], c['suit_value']))
    
    # 4. Quick (and Default):
    elif 'quick' in traits:
        # If Quick triggered, there should be 2 cards.
        if len(cards) == 2:
            if cards[0]['value'] <= 5 and cards[0]['rank'] != 'Joker':
                return max(cards[0], cards[1], key=lambda c: (c['value'], c['suit_value']))
        
        # If Quick didn't trigger, or only one card was drawn, use the first card.
        return cards[0]

    # 5. Default: Use the first card drawn.
    else:
        return cards[0]

def get_traits_display(traits):
    """Get display names for traits"""
    trait_names = {
        'level_headed': 'Level Headed',
        'improved_level_headed': 'Improved Level Headed',
        'quick': 'Quick',
        'hesitant': 'Hesitant'
    }
    return ', '.join([trait_names.get(t, t) for t in traits]) if traits else ''

@app.route('/toggle_hidden', methods=['POST'])
@gm_required
def toggle_hidden():
    global participants
    data = request.json
    index = data.get('index')

    if not (0 <= index < len(participants)):
        return jsonify({'error': 'Invalid participant index'}), 400

    p = participants[index]
    p['is_hidden'] = not p.get('is_hidden', False)
    broadcast_update()
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/toggle_hold', methods=['POST'])
@gm_required
def toggle_hold():
    global participants
    data = request.json
    index = data.get('index')

    if not (0 <= index < len(participants)):
        return jsonify({'error': 'Invalid participant index'}), 400

    p = participants[index]

    # Participants must have drawn cards before they can go on hold
    if not p.get('on_hold') and not p.get('has_drawn'):
        return jsonify({'error': 'Participant has not drawn cards yet'}), 400

    p['on_hold'] = not p.get('on_hold', False)

    if p['on_hold']:
        # Toggling ON: check if any drawn card was a joker and remember it
        p['held_joker'] = any(c.get('rank') == 'Joker' for c in p.get('cards', []))
    else:
        # Toggling OFF: clear joker memory along with all card state
        p['held_joker'] = False

    # Whether toggling on or off, clear all card state so they
    # re-enter initiative cleanly once hold is released.
    p['cards'] = []
    p['active_card'] = None
    p['additional_cards'] = []
    p['has_drawn'] = False

    # Sort: drawn first (by value desc), then on-hold, then undrawn
    def initiative_sort_key(p):
        if p.get('on_hold'):
            return (1, 0, 0)
        if p.get('active_card'):
            return (0, -p['active_card']['value'], -p['active_card']['suit_value'])
        return (2, 0, 0)

    participants.sort(key=initiative_sort_key)
    broadcast_update()
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/add_participant_placeholder', methods=['POST'])
@gm_required
def add_participant_placeholder():
    global participants
    
    # Use a generic name that will be updated by the client
    name = f"New Participant"
    
    # Ensure unique names (or handle duplicates by appending a number)
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
    broadcast_update()
    return jsonify({'success': True, 'participant': new_participant})

if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0', threaded=True)
