from flask import Flask, render_template_string, request, jsonify, session, Response, stream_with_context
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
            return 14
        elif self.rank == 'A':
            return 13
        elif self.rank == 'K':
            return 12
        elif self.rank == 'Q':
            return 11
        elif self.rank == 'J':
            return 10
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
            'cards': [c if isinstance(c, dict) else c.to_dict() for c in p.get('cards', [])],
            'additional_cards': [c if isinstance(c, dict) else c.to_dict() for c in p.get('additional_cards', [])],        
            'active_card': (
                p['active_card'] if isinstance(p.get('active_card'), dict)
                else p.get('active_card').to_dict() if p.get('active_card')
                else None
            )
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
                q.put(message)
            except:
                dead_queues.append(q)
        for q in dead_queues:
            message_queues.remove(q)

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Savage Worlds Initiative Tracker</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
            background-color: #ffffff;
            color: #000000;
        }
        h1 {
            text-align: center;
            margin-bottom: 10px;
        }
        .subtitle {
            text-align: center;
            font-style: italic;
            margin-bottom: 30px;
        }
        .gm-section {
            border: 2px solid #000;
            padding: 15px;
            margin-bottom: 20px;
        }
        .gm-controls {
            display: flex;
            gap: 10px;
            margin-top: 10px;
            flex-wrap: wrap;
        }
        .participant-setup {
            border: 2px solid #000;
            padding: 15px;
            margin-bottom: 20px;
        }
        .participant-row {
            display: flex;
            gap: 10px;
            margin-bottom: 10px;
            align-items: center;
            flex-wrap: wrap;
        }
        .participant-row input[type="text"] {
            flex: 0 0 200px;
            padding: 5px;
            border: 1px solid #000;
        }
        .trait-buttons {
            display: flex;
            gap: 5px;
            flex-wrap: wrap;
        }
        .trait-button {
            padding: 5px 10px;
            border: 2px solid #000;
            background-color: #ffffff;
            cursor: pointer;
            font-size: 12px;
        }
        .trait-button.selected {
            background-color: #000;
            color: #fff;
        }
        .trait-button:disabled {
            opacity: 0.3;
            cursor: not-allowed;
        }
        .participant-row button {
            padding: 5px 10px;
        }
        button {
            background-color: #ffffff;
            color: #000000;
            padding: 8px 15px;
            border: 2px solid #000;
            cursor: pointer;
            font-size: 14px;
        }
        button:hover {
            background-color: #f0f0f0;
        }
        button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .initiative-tracker {
            border: 2px solid #000;
            padding: 15px;
        }
        .initiative-row {
            display: flex;
            gap: 15px;
            padding: 10px;
            margin-bottom: 5px;
            border-bottom: 1px solid #ccc;
            align-items: center;
        }
        .initiative-row:last-child {
            border-bottom: none;
        }
        .rank {
            font-weight: bold;
            min-width: 30px;
        }
        .participant-name {
            min-width: 150px;
            font-weight: bold;
        }
        .cards {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }
        .card {
            border: 1px solid #000;
            padding: 8px 12px;
            min-width: 60px;
            text-align: center;
            background-color: #ffffff;
        }
        .card.active {
            border: 3px solid #000;
            font-weight: bold;
        }
        .card.spades::before {
            content: "♠ ";
        }
        .card.hearts::before {
            content: "♥ ";
            color: red;
        }
        .card.diamonds::before {
            content: "♦ ";
            color: red;
        }
        .card.clubs::before {
            content: "♣ ";
        }
        .card.hearts, .card.diamonds {
            color: red;
        }
        .card.joker {
            font-weight: bold;
            text-decoration: underline;
        }
        .edge-hindrance {
            font-size: 12px;
            font-style: italic;
            color: #666;
        }
        .login-form {
            max-width: 300px;
            margin: 50px auto;
            border: 2px solid #000;
            padding: 20px;
        }
        .login-form input {
            width: 100%;
            padding: 8px;
            margin-bottom: 10px;
            border: 1px solid #000;
            box-sizing: border-box;
        }
        .login-form button {
            width: 100%;
        }
        .status-message {
            padding: 10px;
            margin-bottom: 10px;
            border: 1px solid #000;
        }
        .hidden {
            display: none;
        }
        .viewer-note {
            text-align: center;
            font-style: italic;
            margin-bottom: 20px;
            padding: 10px;
            border: 1px solid #ccc;
        }
    </style>
</head>
<body>
    <h1>Savage Worlds Adventure Edition</h1>
    <div class="subtitle">Initiative Tracker</div>
    
    <div id="loginSection" class="login-form hidden">
        <h3>GM Login</h3>
        <input type="password" id="gmPassword" placeholder="Enter GM Password" onkeypress="if(event.key === 'Enter') login()">
        <button onclick="login()">Login</button>
        <div id="loginError" class="status-message hidden"></div>
    </div>
    
    <div id="viewerNote" class="viewer-note hidden">
        You are viewing as a player. Only the GM can make changes.
        <button onclick="showLogin()">GM Login</button>
    </div>
    
    <div id="mainContent" class="hidden">
        <div id="gmSection" class="gm-section hidden">
            <h3>GM Controls</h3>
            <div class="gm-controls">
                <button onclick="newEncounter()">New Encounter</button>
                <button onclick="nextRound()">Next Round</button>
                <button onclick="resetDeck()">Reset Deck</button>
                <button onclick="clearInitiative()">Clear Initiative</button>
                <button onclick="logout()">Logout</button>
            </div>
            <div style="margin-top: 10px;">Cards remaining: <span id="deckCount">54</span></div>
        </div>
        
        <div id="participantSection" class="participant-setup hidden">
            <h3>Participants</h3>
            <div id="participantList"></div>
            <button onclick="addParticipant()">Add Participant</button>
        </div>
        
        <div class="initiative-tracker">
            <h3>Initiative Order</h3>
            <div id="initiativeOrder"></div>
        </div>
    </div>
    
    <script>
        let isGM = false;
        
        function checkAuth() {
            return fetch('/check_auth')
                .then(response => response.json())
                .then(data => {
                    isGM = data.is_gm;
                    updateUI();
                    if (!isGM) {
                        document.getElementById('viewerNote').classList.remove('hidden');
                    }
                    loadInitiative();
                    return data;
                });
        }
        
        function updateUI() {
            document.getElementById('mainContent').classList.remove('hidden');
            document.getElementById('loginSection').classList.add('hidden');
            
            if (isGM) {
                document.getElementById('gmSection').classList.remove('hidden');
                document.getElementById('participantSection').classList.remove('hidden');
                document.getElementById('viewerNote').classList.add('hidden');
                renderParticipants();
            } else {
                document.getElementById('gmSection').classList.add('hidden');
                document.getElementById('participantSection').classList.add('hidden');
            }
        }
        
        function showLogin() {
            document.getElementById('loginSection').classList.remove('hidden');
            document.getElementById('mainContent').classList.add('hidden');
        }
        
        function login() {
            const password = document.getElementById('gmPassword').value;
            fetch('/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({password: password})
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    isGM = true;
                    updateUI();
                    loadInitiative();
                } else {
                    document.getElementById('loginError').textContent = 'Invalid password';
                    document.getElementById('loginError').classList.remove('hidden');
                }
            });
        }
        
        function logout() {
            fetch('/logout', {method: 'POST'})
                .then(() => {
                    isGM = false;
                    window.location.reload();
                });
        }
        
        function addParticipant() {
            const list = document.getElementById('participantList');
            const index = list.children.length;
            const row = document.createElement('div');
            row.className = 'participant-row';
            row.innerHTML = `
                <input type="text" placeholder="Participant name" data-index="${index}">
                <div class="trait-buttons">
                    <button class="trait-button" data-trait="level_headed" onclick="toggleTrait(this)">Level Headed</button>
                    <button class="trait-button" data-trait="improved_level_headed" onclick="toggleTrait(this)">Improved Level Headed</button>
                    <button class="trait-button" data-trait="quick" onclick="toggleTrait(this)">Quick</button>
                    <button class="trait-button" data-trait="hesitant" onclick="toggleTrait(this)">Hesitant</button>
                </div>
                <button onclick="removeParticipant(this)">Remove</button>
                <button class="deal-in-button" onclick="dealIn(${index})">Deal In</button>
            `;
            list.appendChild(row);

            const input = row.querySelector('input[type="text"]');
            input.focus();
        }
        
        function toggleTrait(button) {
            const row = button.closest('.participant-row');
            const traitButtons = row.querySelectorAll('.trait-button');
            const trait = button.dataset.trait;
            
            // Toggle selection
            button.classList.toggle('selected');
            
            // Handle Hesitant conflicts
            if (trait === 'hesitant' && button.classList.contains('selected')) {
                // Deselect and disable conflicting traits
                traitButtons.forEach(btn => {
                    if (['level_headed', 'improved_level_headed', 'quick'].includes(btn.dataset.trait)) {
                        btn.classList.remove('selected');
                        btn.disabled = true;
                    }
                });
            } else if (trait === 'hesitant' && !button.classList.contains('selected')) {
                // Re-enable traits when Hesitant is deselected
                traitButtons.forEach(btn => {
                    if (['level_headed', 'improved_level_headed', 'quick'].includes(btn.dataset.trait)) {
                        btn.disabled = false;
                    }
                });
            } else if (['level_headed', 'improved_level_headed', 'quick'].includes(trait) && button.classList.contains('selected')) {
                // If selecting these, deselect and disable Hesitant
                traitButtons.forEach(btn => {
                    if (btn.dataset.trait === 'hesitant') {
                        btn.classList.remove('selected');
                        btn.disabled = true;
                    }
                });
            } else if (['level_headed', 'improved_level_headed', 'quick'].includes(trait) && !button.classList.contains('selected')) {
                // Check if any of these traits are still selected
                const anySelected = Array.from(traitButtons).some(btn => 
                    ['level_headed', 'improved_level_headed', 'quick'].includes(btn.dataset.trait) && 
                    btn.classList.contains('selected')
                );
                if (!anySelected) {
                    // Re-enable Hesitant
                    traitButtons.forEach(btn => {
                        if (btn.dataset.trait === 'hesitant') {
                            btn.disabled = false;
                        }
                    });
                }
            }
        }
        
        function removeParticipant(button) {
 //           button.parentElement.remove();
            const row = button.parentElement;
            const index = Array.from(row.parentElement.children).indexOf(row);

            // Remove from server
            fetch('/remove_participant', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({index})
            })
            .then(() => {
                row.remove();
            });
        }
        
        function renderParticipants() {
            fetch('/get_participants')
                .then(response => response.json())
                .then(data => {
                    const list = document.getElementById('participantList');
                    list.innerHTML = '';
                    data.participants.forEach((p, index) => {
                        const row = document.createElement('div');
                        row.className = 'participant-row';
                        
                        const traitsArray = Array.isArray(p.traits) ? p.traits : [];
                        const hasHesitant = traitsArray.includes('hesitant');
                        const hasOthers = traitsArray.some(t => ['level_headed', 'improved_level_headed', 'quick'].includes(t));
                        
                        // Only show "Deal In" if participant hasn't drawn yet
                        const dealInButton = !p.has_drawn
                            ? `<button onclick="dealIn(${index})">Deal In</button>`
                            : '';
                        
                        row.innerHTML = `
                            <input type="text" value="${p.name}" data-index="${index}">
                            <div class="trait-buttons">
                                <button class="trait-button ${traitsArray.includes('level_headed') ? 'selected' : ''}" 
                                        data-trait="level_headed" 
                                        ${hasHesitant ? 'disabled' : ''}
                                        onclick="toggleTrait(this)">Level Headed</button>
                                <button class="trait-button ${traitsArray.includes('improved_level_headed') ? 'selected' : ''}" 
                                        data-trait="improved_level_headed" 
                                        ${hasHesitant ? 'disabled' : ''}
                                        onclick="toggleTrait(this)">Improved Level Headed</button>
                                <button class="trait-button ${traitsArray.includes('quick') ? 'selected' : ''}" 
                                        data-trait="quick" 
                                        ${hasHesitant ? 'disabled' : ''}
                                        onclick="toggleTrait(this)">Quick</button>
                                <button class="trait-button ${traitsArray.includes('hesitant') ? 'selected' : ''}" 
                                        data-trait="hesitant" 
                                        ${hasOthers ? 'disabled' : ''}
                                        onclick="toggleTrait(this)">Hesitant</button>
                            </div>
                            <button onclick="removeParticipant(this)">Remove</button>
                            <button onclick="drawAdditional(${index})">Draw Card</button>
                        `;
                        list.appendChild(row);
                    });
                });
        }

        function dealIn(index) {
            const row = document.querySelectorAll('.participant-row')[index];
            const traitButtons = row.querySelectorAll('.trait-button.selected');
            const traits = Array.from(traitButtons).map(btn => btn.dataset.trait);
            const nameInput = row.querySelector('input[type="text"]');
            const name = nameInput.value.trim();

            if (!name) {
                alert('Participant must have a name.');
                return;
            }

            fetch('/deal_in', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({index, name, traits})
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    alert(data.error);
                } else {
                    displayInitiative(data);
                    updateDeckCount();
                    if (isGM) renderParticipants();
                    // Hide the Deal In button
                    const dealInButton = row.querySelector('.deal-in-button');
                    if (dealInButton) dealInButton.style.display = 'none';
                }
            });
        }
        
        function getParticipantsFromUI() {
            const participants = [];
            document.querySelectorAll('.participant-row').forEach(row => {
                const nameInput = row.querySelector('input[type="text"]');
                const traitButtons = row.querySelectorAll('.trait-button.selected');
                if (nameInput.value.trim() !== '') {
                    const selectedTraits = Array.from(traitButtons).map(btn => btn.dataset.trait);
                    participants.push({
                        name: nameInput.value.trim(),
                        traits: selectedTraits
                    });
                }
            });
            return participants;
        }
        
        function newEncounter() {
            const participants = getParticipantsFromUI();
            if (participants.length === 0) {
                alert('Please add participants first');
                return;
            }
            
            fetch('/new_encounter', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({participants: participants})
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    alert(data.error);
                } else {
                    displayInitiative(data);
                    updateDeckCount();
                    if (isGM) renderParticipants();
                }
            });
        }
        
        function nextRound() {
            const participants = getParticipantsFromUI();
            if (participants.length === 0) {
                alert('Please add participants first');
                return;
            }
            
            fetch('/next_round', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({participants: participants})
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    alert(data.error);
                } else {
                    displayInitiative(data);
                    updateDeckCount();
                    if (isGM) renderParticipants();
                }
            });
        }
        
        function resetDeck() {
            const participants = getParticipantsFromUI();
            fetch('/reset_deck', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({participants: participants})
            })
            .then(response => response.json())
            .then(data => {
                displayInitiative(data);
                updateDeckCount();
                if (isGM) renderParticipants();
            });
        }
        
        function clearInitiative() {
            if (confirm('Clear all participants and reset deck?')) {
                fetch('/clear_initiative', {method: 'POST'})
                    .then(response => response.json())
                    .then(data => {
                        displayInitiative(data);
                        updateDeckCount();
                        if (isGM) renderParticipants();
                    });
            }
        }
        
        function drawAdditional(index) {
            fetch('/draw_additional', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({index: index})
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    alert(data.error);
                } else {
                    displayInitiative(data);
                    updateDeckCount();
                    if (isGM && Array.isArray(data.participants) && data.participants.length > 0) {
                        renderParticipants();
                    }
                }
            });
        }
        
        function nextRound() {
            const participants = getParticipantsFromUI();
            fetch('/next_round', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({participants: participants})
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    alert(data.error);
                } else {
                    displayInitiative(data);
                    updateDeckCount();
                    if (isGM) renderParticipants();
                }
            });
        }
        
        function loadInitiative() {
            fetch('/get_initiative')
                .then(response => response.json())
                .then(data => {
                    displayInitiative(data);
                    updateDeckCount();
                });
        }
        
        function displayInitiative(data) {
            const orderDiv = document.getElementById('initiativeOrder');
            if (data.participants.length === 0) {
                orderDiv.innerHTML = '<p>No initiative drawn yet.</p>';
                return;
            }
            
            orderDiv.innerHTML = '';
            data.participants.forEach((p, index) => {
                const row = document.createElement('div');
                row.className = 'initiative-row';
                
                const cardsHTML = p.cards.map(card => {
                    const suitClass = card.rank === 'Joker' ? 'joker' : card.suit.toLowerCase();
                    const activeClass = card === p.active_card ? 'active' : '';
                    return `<div class="card ${suitClass} ${activeClass}">${card.display}</div>`;
                }).join('');
                
                const traitText = p.trait_display ? `<div class="edge-hindrance">${p.trait_display}</div>` : '';
                
                row.innerHTML = `
                    <div class="rank">${index + 1}.</div>
                    <div class="participant-name">${p.name}</div>
                    <div class="cards">${cardsHTML}</div>
                    ${traitText}
                `;
                orderDiv.appendChild(row);
            });
        }
        
        function updateDeckCount() {
            fetch('/deck_info')
                .then(response => response.json())
                .then(data => {
                    const countElem = document.getElementById('deckCount');
                    if (countElem) {
                        countElem.textContent = data.remaining;
                    }
                });
        }
        
        // Auto-refresh for non-GM users
        //function startAutoRefresh() {
        //    if (!isGM) {
        //        setInterval(loadInitiative, 2000);
        //    }
        //}

        let eventSource = null;

        function setupSSE() {
            if (eventSource) {
                eventSource.close();
            }

            eventSource = new EventSource('/stream');

            eventSource.onopen = function() {
                console.log('Connected to server');
            };

            eventSource.onmessage = function(event) {
                console.log('Received update:', event.data);
                const data = JSON.parse(event.data);
                displayInitiative({participants: data.participants});
                const deckCountElem = document.getElementById('deckCount');
                if (deckCountElem) {
                    deckCountElem.textContent = data.deck_remaining;
                }
                if (isGM && document.getElementById('participantList')) {
                    renderParticipants();
                }
            };

            eventSource.onerror = function() {
                console.log('Connection lost, reconnecting...');
                eventSource.close();
                setTimeout(setupSSE, 3000);
            };
            }

        // Initialize at page load
        checkAuth().then(() => {
            setupSSE();
});

    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

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
                except:
                    # heartbeat to prevent buffering/timeout
                    yield ": ping\n\n"
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

@app.route('/new_encounter', methods=['POST'])
@gm_required
def new_encounter():
    global participants, deck, joker_drawn
    data = request.json
    participants_data = data.get('participants', [])
    
    # Reset deck and joker flag
    deck = Deck()
    joker_drawn = False
    
    # Keep participants but clear their cards
    participants = []
    for p_data in participants_data:
        participant = {
            'name': p_data['name'],
            'traits': p_data.get('traits', []),
            'cards': [],
            'active_card': None,
            'trait_display': get_traits_display(p_data.get('traits', [])),
            'additional_cards': [],
            'has_drawn': False
        }
        participants.append(participant)
    
    broadcast_update()
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/next_round', methods=['POST'])
@gm_required
def next_round():
    global participants, deck, joker_drawn
    data = request.json
    participants_data = data.get('participants', [])
    
    # If a joker was drawn in the previous round, reset and reshuffle the deck
    if joker_drawn:
        deck = Deck()
        joker_drawn = False
    
    # Draw new cards for all participants
    participants = []
    for p_data in participants_data:
        participant = {
            'name': p_data['name'],
            'traits': p_data.get('traits', []),
            'cards': [],
            'active_card': None,
            'trait_display': get_traits_display(p_data.get('traits', [])),
            'additional_cards': []
        }
        
        cards = draw_for_participant(participant['traits'])
        participant['cards'] = cards
        participant['active_card'] = determine_active_card(cards, participant['traits'], [])
        
        # Check for jokers in this draw
        if any(card['rank'] == 'Joker' for card in cards):
            joker_drawn = True
        
        participants.append(participant)
    
    participants.sort(key=lambda p: (
        p['active_card']['value'] if p['active_card'] else -1,
        p['active_card']['suit_value'] if p['active_card'] else -1
    ), reverse=True)
    
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
    
    # Keep participants but clear their cards
    participants = []
    for p_data in participants_data:
        participant = {
            'name': p_data['name'],
            'traits': p_data.get('traits', []),
            'cards': [],
            'active_card': None,
            'trait_display': get_traits_display(p_data.get('traits', [])),
            'additional_cards': []
        }
        participants.append(participant)
    
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
        additional_card = deck.draw(1)
        if additional_card:
            card_dict = additional_card[0].to_dict()
            participants[index]['cards'].append(card_dict)
            
            # Check for joker
            if card_dict['rank'] == 'Joker':
                joker_drawn = True
            
            # Track this as an additional card
            if 'additional_cards' not in participants[index]:
                participants[index]['additional_cards'] = []
            participants[index]['additional_cards'].append(card_dict)
            
            # For additional cards, if it's higher than current active, use it
            current_active = participants[index]['active_card']
            if current_active:
                if (card_dict['value'], card_dict['suit_value']) > \
                   (current_active['value'], current_active['suit_value']):
                    participants[index]['active_card'] = card_dict
            else:
                participants[index]['active_card'] = card_dict

            # Mark participant as having drawn
            participants[index]['has_drawn'] = True
    
    # Re-sort by active card
    participants.sort(key=lambda p: (
        p['active_card']['value'] if p['active_card'] else -1,
        p['active_card']['suit_value'] if p['active_card'] else -1
    ), reverse=True)
    
    broadcast_update()
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/reset', methods=['POST'])
@gm_required
def reset():
    global deck, participants, joker_drawn
    deck = Deck()
    participants = []
    joker_drawn = False
    broadcast_update()
    return jsonify({'participants': []})

@app.route('/deal_in', methods=['POST'])
@gm_required
def deal_in():
    global participants, deck, joker_drawn
    data = request.json
    name = data.get('name')
    traits = data.get('traits', [])

    if not name:
        return jsonify({'error': 'Participant name required'}), 400

    participant = {
        'name': name,
        'traits': traits,
        'cards': [],
        'active_card': None,
        'trait_display': get_traits_display(traits),
        'additional_cards': [],
        'has_drawn': True
    }

    # Draw cards according to traits
    cards = draw_for_participant(traits)
    participant['cards'] = cards
    participant['active_card'] = determine_active_card(cards, traits, [])

    if any(card['rank'] == 'Joker' for card in cards):
        joker_drawn = True

    # Always append new participant
    participants.append(participant)

    # Sort initiative
    participants.sort(key=lambda p: (
        p['active_card']['value'] if p['active_card'] else -1,
        p['active_card']['suit_value'] if p['active_card'] else -1
    ), reverse=True)

    broadcast_update()
    return jsonify({'participants': serialize_participants(participants)})


@app.route('/get_initiative')
def get_initiative():
    return jsonify({'participants': serialize_participants(participants)})

@app.route('/deck_info')
def deck_info():
    return jsonify({'remaining': len(deck.cards)})

def draw_for_participant(traits):
    """Draw cards based on traits"""
    num_cards = 1
    
    # Determine base number of cards to draw
    if 'improved_level_headed' in traits:
        num_cards = 3
    elif 'level_headed' in traits:
        num_cards = 2
    elif 'hesitant' in traits:
        num_cards = 2
    
    cards = deck.draw(num_cards)
    
    # Handle Quick trait
    if 'quick' in traits and cards:
        first_card = cards[0]
        if first_card.value() <= 5 and first_card.rank != 'Joker':
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
    """Get active card from initial draw based on traits"""
    if not cards:
        return None
    
    if 'level_headed' in traits or 'improved_level_headed' in traits:
        # Use best card
        return max(cards, key=lambda c: (c['value'], c['suit_value']))
    elif 'hesitant' in traits:
        # Use worst card unless there's a joker
        jokers = [c for c in cards if c['rank'] == 'Joker']
        if jokers:
            return jokers[0]
        return min(cards, key=lambda c: (c['value'], c['suit_value']))
    elif 'quick' in traits:
        # If first card is 5 or lower (and not joker), use second card
        if len(cards) > 1 and cards[0]['value'] <= 5 and cards[0]['rank'] != 'Joker':
            return cards[1]
        return cards[0]
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

if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0', threaded=True)
