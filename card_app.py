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
        .gm-error {
            margin-top: 10px;
            padding: 10px;
            border: 2px solid #000;
            background-color: #fff;
            font-weight: bold;
        }
        .hidden {
            display: none;
        }
        .hold-button.active {
            background-color: #000;
            color: #fff;
        }
        .hidden-button.active {
            background-color: #000;
            color: #fff;
        }
        .hold-card {
            border: 2px dashed #999;
            padding: 8px 20px;
            min-width: 60px;
            text-align: center;
            background-color: #f0f0f0;
            color: #555;
            font-style: italic;
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
               <!-- <button onclick="newEncounter()">New Encounter</button> -->
                <button onclick="nextRound()">Next Round</button>
                <button onclick="resetDeck()">Reset Deck</button>
                <button onclick="clearInitiative()">Clear Initiative</button>
                <button onclick="logout()">Logout</button>
            </div>
            <div style="margin-top: 10px;">Cards remaining: <span id="deckCount">54</span></div>
            <div id="gmError" class="gm-error hidden"></div>
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
    
    <footer style="text-align:center; margin-top:40px; padding: 20px 0;">
        <p style="max-width:800px; margin: 0 auto 20px auto;">This game references the Savage Worlds game system, available from Pinnacle Entertainment Group at <a href="https://www.peginc.com">www.peginc.com</a>. Savage Worlds and all associated logos and trademarks are copyrights of Pinnacle Entertainment Group. Used with permission. Pinnacle makes no representation or warranty as to the quality, viability, or suitability for purpose of this product.</p>
        <img src="{{ url_for('static', filename='SW_LOGO_FP_2018.png') }}" alt="Savage Worlds Logo" style="width:25%; height:auto; display:block; margin:0 auto;">
    </footer>

    <script>
        let isGM = false;

        function showGmError(message) {
            const el = document.getElementById('gmError');
            if (el) {
                el.textContent = message;
                el.classList.remove('hidden');
            }
        }

        function clearGmError() {
            const el = document.getElementById('gmError');
            if (el) {
                el.classList.add('hidden');
                el.textContent = '';
            }
        }

        // Dismiss the error message on any button click
        document.addEventListener('click', function(e) {
            if (e.target.tagName === 'BUTTON') {
                clearGmError();
            }
        });
        
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
                    // Send a request to the server to add an unnamed participant placeholder
                    fetch('/add_participant_placeholder', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({}) // Send empty body, server handles name creation
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (!data.success) {
                            alert(data.error || "Failed to add participant.");
                        }
                        // Server broadcast handles the UI redraw and focus restoration
                    });
                }
        
        function toggleTrait(button) {
                    const row = button.closest('.participant-row');
                    const traitButtons = row.querySelectorAll('.trait-button');
                    const trait = button.dataset.trait;
                    
                    // Toggle selection (Local DOM update - KEPT)
                    button.classList.toggle('selected');
                    
                    // Handle Hesitant conflicts (Existing logic - KEPT)
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
                    
                    // Sync selected traits to the server ===
                    const nameInput = row.querySelector('input[type="text"]');
                    const index = parseInt(nameInput.dataset.index); // Get server index from input field
                    const selectedTraits = Array.from(row.querySelectorAll('.trait-button.selected')).map(btn => btn.dataset.trait);

                    if (isGM && !isNaN(index)) {
                        fetch('/update_traits', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({index: index, traits: selectedTraits})
                        })
                        .then(response => response.json())
                        .then(data => {
                            if (data.error) {
                                alert('Error updating traits: ' + data.error);
                            }
                            // SSE broadcast handles the UI redraw and persistence
                        })
                        .catch(error => {
                            console.error('Network error during trait update:', error);
                            alert('Network error while updating traits.');
                        });
                    }
                }
        
        function renderParticipants(serverParticipants) {
                    if (!isGM) return;
                
                    const list = document.getElementById('participantList');
                    const currentRows = Array.from(list.querySelectorAll('.participant-row'));
                    const rowsToRemove = new Set(currentRows);

                    // --- CRITICAL ADDITION: Preserve Focus State ---
                    let activeElement = document.activeElement;
                    let focusedInputIndex = -1;
                    let focusedInputValue = null;
                    if (activeElement && activeElement.tagName === 'INPUT' && activeElement.closest('.participant-row')) {
                        // Get the server index before it's potentially removed/re-rendered
                        focusedInputIndex = parseInt(activeElement.dataset.index);
                        focusedInputValue = activeElement.value; // Store the actual typed value
                    }
                    // ------------------------------------------------

                    // Use data passed in directly (e.g. from SSE) to avoid a redundant
                    // fetch('/get_participants') that races with concurrent SSE events and
                    // causes duplicate rows. All callers must supply the participant list.
                    const doRender = (serverParticipants) => {

                            serverParticipants.forEach((p, index) => {
                                // Find the row by its stable server index, not by the typed value.
                                // Matching by value caused duplicates when the user hadn't yet typed
                                // a name that matched the server placeholder.
                                let row = currentRows.find(r => {
                                    const input = r.querySelector('input[type="text"]');
                                    return input && parseInt(input.dataset.index) === index;
                                });

                                if (row) {
                                    rowsToRemove.delete(row);
                                }

                                const traitsArray = Array.isArray(p.traits) ? p.traits : [];
                                const hasHesitant = traitsArray.includes('hesitant');
                                const hasOthers = traitsArray.some(t => ['level_headed', 'improved_level_headed', 'quick'].includes(t));

                                // Build trait buttons HTML
                                const traitButtonsHTML = `
                                    <button class="trait-button ${traitsArray.includes('level_headed') ? 'selected' : ''}" 
                                            data-trait="level_headed" ${hasHesitant ? 'disabled' : ''} onclick="toggleTrait(this)">Level Headed</button>
                                    <button class="trait-button ${traitsArray.includes('improved_level_headed') ? 'selected' : ''}" 
                                            data-trait="improved_level_headed" ${hasHesitant ? 'disabled' : ''} onclick="toggleTrait(this)">Improved Level Headed</button>
                                    <button class="trait-button ${traitsArray.includes('quick') ? 'selected' : ''}" 
                                            data-trait="quick" ${hasHesitant ? 'disabled' : ''} onclick="toggleTrait(this)">Quick</button>
                                    <button class="trait-button ${traitsArray.includes('hesitant') ? 'selected' : ''}" 
                                            data-trait="hesitant" ${hasOthers ? 'disabled' : ''} onclick="toggleTrait(this)">Hesitant</button>
                                `;

                                // Show Deal In button only if participant hasn't drawn any cards
                                const shouldShowDealIn = !p.has_drawn && !p.on_hold;
                                const dealInButtonHTML = shouldShowDealIn
                                    ? `<button class="deal-in-button" onclick="dealIn(${index})">Deal In</button>`
                                    : '';

                                // Show Hold button only if participant has drawn cards this round
                                // (on_hold itself counts since they were drawn before hold was set)
                                const shouldShowHold = p.has_drawn || p.on_hold;
                                    
                                let nameValue = p.name;
                                
                                // --- CRITICAL FIX: Restore Value from Focus State ---
                                // Check if the participant is the one that was actively being typed into
                                if (index === focusedInputIndex && focusedInputValue !== null) {
                                    nameValue = focusedInputValue;
                                }
                                // --------------------------------------------------------

                                if (!row) {
                                    // Participant row doesn't exist yet → create it
                                    row = document.createElement('div');
                                    row.className = 'participant-row';
                                    row.innerHTML = `
                                        <input type="text" value="${nameValue}" data-index="${index}" onblur="updateParticipantName(this)">
                                        <div class="trait-buttons">${traitButtonsHTML}</div>
                                        <div class="row-buttons" style="display:flex;gap:5px;">
                                            ${shouldShowHold ? `<button class="hold-button ${p.on_hold ? 'active' : ''}" onclick="toggleHold(${index})">Hold</button>` : ''}
                                            <button class="hidden-button ${p.is_hidden ? 'active' : ''}" onclick="toggleHidden(${index})">Hidden</button>
                                            <button class="remove-button" onclick="removeParticipant(${index})">Remove</button>
                                            ${dealInButtonHTML}
                                        </div>
                                    `;
                                    list.appendChild(row);
                                } else {
                                    // Participant row exists — update in place.
                                    // Protect the text input's focus/value; rebuild all buttons fresh.
                                    const nameInput = row.querySelector('input[type="text"]');
                                    nameInput.dataset.index = index;
                                    nameInput.onblur = () => updateParticipantName(nameInput);
                                    if (activeElement !== nameInput) {
                                        nameInput.value = nameValue;
                                    }

                                    const traitContainer = row.querySelector('.trait-buttons');
                                    traitContainer.innerHTML = traitButtonsHTML;

                                    // Rebuild button area from scratch so order and visibility
                                    // are always correct with no stale display:none state.
                                    let buttonContainer = row.querySelector('.row-buttons');
                                    if (!buttonContainer) {
                                        buttonContainer = document.createElement('div');
                                        buttonContainer.className = 'row-buttons';
                                        buttonContainer.style.display = 'flex';
                                        buttonContainer.style.gap = '5px';
                                        row.appendChild(buttonContainer);
                                    }
                                    buttonContainer.innerHTML =
                                        (shouldShowHold
                                            ? `<button class="hold-button ${p.on_hold ? 'active' : ''}">Hold</button>`
                                            : '') +
                                        `<button class="hidden-button ${p.is_hidden ? 'active' : ''}">Hidden</button>` +
                                        `<button class="remove-button">Remove</button>` +
                                        (shouldShowDealIn
                                            ? `<button class="deal-in-button">Deal In</button>`
                                            : '');

                                    const holdBtn = buttonContainer.querySelector('.hold-button');
                                    if (holdBtn) holdBtn.onclick = () => toggleHold(index);
                                    buttonContainer.querySelector('.hidden-button').onclick = () => toggleHidden(index);
                                    buttonContainer.querySelector('.remove-button').onclick = () => removeParticipant(index);
                                    const dealInBtn = buttonContainer.querySelector('.deal-in-button');
                                    if (dealInBtn) dealInBtn.onclick = () => dealIn(index);

                                    // Ensure the row is placed in the correct order in the DOM
                                    if (list.children[index] !== row) {
                                        list.insertBefore(row, list.children[index]);
                                    }
                                }
                            });
                            
                            // 3. Remove any UI rows that were not found in the server data
                            rowsToRemove.forEach(row => row.remove());

                            // --- CRITICAL FIX: Re-focus the element after redraw ---
                            if (focusedInputIndex !== -1) {
                                // Find the row that matches the index we saved
                                const matchingRow = Array.from(list.querySelectorAll('.participant-row')).find(row => {
                                    const input = row.querySelector('input[type="text"]');
                                    return input && parseInt(input.dataset.index) === focusedInputIndex;
                                });
                                
                                if (matchingRow) {
                                    matchingRow.querySelector('input[type="text"]').focus();
                                }
                            }
                            // ------------------------------------------------------------

                            // Find and focus on the latest added participant if nothing was being edited
                            if (focusedInputIndex === -1 && serverParticipants.length > currentRows.length) {
                                const lastIndex = serverParticipants.length - 1;
                                const lastRow = list.children[lastIndex];
                                if (lastRow) {
                                    const input = lastRow.querySelector('input[type="text"]');
                                    if (input) {
                                        input.focus();
                                        input.select(); // NEW QoL FEATURE: Selects the default text
                                    }
                                }
                            }
                    };

                    if (serverParticipants) {
                        doRender(serverParticipants);
                    } else {
                        fetch('/get_participants')
                            .then(response => response.json())
                            .then(data => doRender(data.participants));
                    }
                }

        function updateParticipantName(inputElement) {
            const index = parseInt(inputElement.dataset.index);
            const newName = inputElement.value.trim();

            if (isNaN(index) || newName === '') {
                return;
            }

            fetch('/update_name', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({index: index, name: newName})
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    alert(data.error);
                }
                // Server broadcast handles the redraw.
            });
        }

        function removeParticipant(index) {
                    fetch('/remove_participant', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({index})
                    })
                    .then(response => response.json())
                    .then(data => {
                        // SSE broadcast handles removal from all views
                    });
                }

        function toggleHold(index) {
            fetch('/toggle_hold', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({index})
            });
            // SSE broadcast handles the redraw
        }

        function toggleHidden(index) {
            fetch('/toggle_hidden', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({index})
            });
            // SSE broadcast handles the redraw
        }

        function dealIn(index) {
                    // Find the row using the *current* position in the UI list
                    const row = document.querySelectorAll('.participant-row')[index];
                    if (!row) return; 

                    const traitButtons = row.querySelectorAll('.trait-button.selected');
                    const traits = Array.from(traitButtons).map(btn => btn.dataset.trait);
                    const nameInput = row.querySelector('input[type="text"]');
                    const name = nameInput.value.trim();

                    if (!name) {
                        alert('Participant must have a name.');
                        return;
                    }

                    // Temporarily disable the button to prevent double-clicks
                    const dealInButton = row.querySelector('.deal-in-button');
                    if (dealInButton) dealInButton.disabled = true;

                    fetch('/deal_in', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        // NOTE: We rely on the server to find the participant by name if they are new,
                        // or update the existing one if they are already in the list.
                        body: JSON.stringify({name, traits})
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.error) {
                            showGmError(data.error);
                        } else {
                            // CRITICAL FIX: After a successful deal-in, the server has updated
                            // the global list and broadcast the result. The participant is
                            // now synchronized. The next renderParticipants will handle the redraw.
                            
                            // We don't need to manually update the UI here, as the SSE will trigger
                            // the complete redraw via displayInitiative and renderParticipants.
                            
                            // Re-enable the button (though renderParticipants should hide it)
                            if (dealInButton) dealInButton.disabled = false;
                        }
                    })
                    .catch(() => {
                        if (dealInButton) dealInButton.disabled = false;
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
        
        /* function newEncounter() {
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
                    showGmError(data.error);
                } else {
                    displayInitiative(data);
                    updateDeckCount();
                    if (isGM) renderParticipants();
                }
            });
        } */
        
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
                    showGmError(data.error);
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
                    showGmError(data.error);
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
            let participantsToShow = data.participants;
            if (!isGM) {
                // Players see: participants with cards drawn OR participants on hold
                participantsToShow = participantsToShow.filter(p => !p.is_hidden && ((p.cards && p.cards.length > 0) || p.on_hold));
            }

            if (participantsToShow.length === 0) {
                orderDiv.innerHTML = '<p>No initiative drawn yet.</p>';
                return;
            }

                orderDiv.innerHTML = '';
                participantsToShow.forEach((p, index) => {
                    const row = document.createElement('div');
                    row.className = 'initiative-row';
                    row.style.display = 'flex';
                    row.style.alignItems = 'center';
                    row.style.gap = '10px'; // spacing between main sections

                    // Rank + Name container
                    const rankNameHTML = `
                        <div class="rank-name" style="display:flex; align-items:center; gap:5px;">
                            <div class="rank">${index + 1}.</div>
                            <div class="participant-name">${p.name}</div>
                        </div>
                    `;

                    // Cards — show Hold card (and joker if applicable) if on hold, otherwise normal cards
                    let cardsContainerHTML;
                    if (p.on_hold) {
                        const jokerHTML = p.held_joker
                            ? `<div class="card joker">Joker</div>`
                            : '';
                        cardsContainerHTML = `<div class="cards" style="display:flex; gap:5px; flex-wrap:wrap;"><div class="hold-card">Hold</div>${jokerHTML}</div>`;
                    } else {
                        const cardsHTML = p.cards.map(card => {
                            const suitClass = card.rank === 'Joker' ? 'joker' : card.suit.toLowerCase();
                            const activeClass = card === p.active_card ? 'active' : '';
                            return `<div class="card ${suitClass} ${activeClass}">${card.display}</div>`;
                        }).join('');
                        cardsContainerHTML = `<div class="cards" style="display:flex; gap:5px; flex-wrap:wrap;">${cardsHTML}</div>`;
                    }

                    // Trait display
                    const traitText = p.trait_display ? `<div class="edge-hindrance">${p.trait_display}</div>` : '';

                    // GM-only button — suppress Draw Additional for held participants
                    const drawButtonHTML = (isGM && !p.on_hold && p.cards && p.cards.length > 0)
                    ? `<button style="margin-left:auto" onclick="drawAdditional(${index})">Draw Additional</button>`
                    : '';

                    row.innerHTML = rankNameHTML + cardsContainerHTML + traitText + drawButtonHTML;

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
                const data = JSON.parse(event.data);
                displayInitiative({participants: data.participants});
                const deckCountElem = document.getElementById('deckCount');
                if (deckCountElem) {
                    deckCountElem.textContent = data.deck_remaining;
                }
                if (isGM && document.getElementById('participantList')) {
                    renderParticipants(data.participants);
                }
            };

            eventSource.onerror = function() {
                console.log('Connection lost, reconnecting...', error);
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
