import os
import json
import pickle
import re
import time
import uuid
import logging
import traceback
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, redirect, url_for, request, jsonify, session
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

os.makedirs('data', exist_ok=True)

logging.basicConfig(
    filename='data/app.log',
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s'
)

# ── secret key persistente ───────────────────────────────────────────────────
_sk_path = 'data/secret_key'
if os.path.exists(_sk_path):
    with open(_sk_path, 'rb') as _f:
        _secret = _f.read()
else:
    _secret = os.urandom(24)
    with open(_sk_path, 'wb') as _f:
        _f.write(_secret)

app = Flask(__name__)
app.secret_key = _secret

SCOPES               = ['https://www.googleapis.com/auth/calendar.readonly']
CREDENTIALS_FILE     = 'credentials.json'
TOKEN_FILE           = 'token.pickle'
CONCERT_DATA_FILE    = 'data/concert_data.json'
CONCERTS_BASE_FILE   = 'data/concerts_base.json'   # dados locais do calendário
DISTANCES_CACHE_FILE = 'data/distances_cache.json'
AGENCIES_FILE        = 'data/agencies.json'
OAUTH_STATE_FILE     = 'data/oauth_state.tmp'
DELETED_EVENTS_FILE  = 'data/deleted_events.json'
DESPESAS_FILE        = 'data/despesas.json'
DESPESAS_OVERRIDES_FILE = 'data/despesas_overrides.json'
CONTAB_CONFIG_FILE   = 'data/config_contab.json'
ORIGIN               = "Rua de Macau, Coimbra, Portugal"
APP_HOST             = '127.0.0.1'
APP_PORT             = 8765
APP_URL              = f'http://{APP_HOST}:{APP_PORT}'
REDIRECT_URI         = f'http://{APP_HOST}:{APP_PORT}/oauth/callback'

# cache de distâncias em memória (carregado uma vez do disco)
_distances_mem = None


# ── persistência ──────────────────────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── parse do título do evento ─────────────────────────────────────────────────
# Formato: "Artista | Evento, Local [SUB Substituto]"

def parse_event_title(summary):
    artista = evento = local = substituto = ''
    if not summary:
        return artista, evento, local, substituto

    if '|' in summary:
        artista, rest = summary.split('|', 1)
        artista = artista.strip()
        rest    = rest.strip()
    else:
        artista = summary.strip()
        rest    = ''

    if ',' in rest:
        evento, local_part = rest.split(',', 1)
        evento     = evento.strip()
        local_part = local_part.strip()
    else:
        evento     = rest.strip()
        local_part = ''

    sub_match = re.search(r'\bSUB\b', local_part)
    if sub_match:
        local      = local_part[:sub_match.start()].strip()
        substituto = local_part[sub_match.end():].strip()
    else:
        local = local_part

    return artista, evento, local, substituto


# ── Google Calendar ───────────────────────────────────────────────────────────

def get_credentials():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'rb') as f:
            creds = pickle.load(f)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_FILE, 'wb') as f:
                pickle.dump(creds, f)
            return creds
    return None

def save_credentials(creds):
    with open(TOKEN_FILE, 'wb') as f:
        pickle.dump(creds, f)

def get_service():
    creds = get_credentials()
    if not creds:
        return None
    return build('calendar', 'v3', credentials=creds)


# ── geocoding + distância ─────────────────────────────────────────────────────

def geocode(address):
    url     = "https://nominatim.openstreetmap.org/search"
    params  = {'q': address, 'format': 'json', 'limit': 1}
    headers = {'User-Agent': 'EmpresaGestaoApp/1.0'}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        results = r.json()
        if results:
            return float(results[0]['lat']), float(results[0]['lon'])
    except Exception:
        pass
    return None, None

def _migrate_distances_cache():
    cache = load_json(DISTANCES_CACHE_FILE, {})
    if cache.get('__version', 1) >= 2:
        return
    for k, v in list(cache.items()):
        if k != '__version' and isinstance(v, (int, float)):
            cache[k] = round(v * 2, 1)
    cache['__version'] = 2
    save_json(DISTANCES_CACHE_FILE, cache)

_migrate_distances_cache()


def _get_distances_mem():
    global _distances_mem
    if _distances_mem is None:
        _distances_mem = load_json(DISTANCES_CACHE_FILE, {})
    return _distances_mem


def driving_distance_km(destination):
    """Distância ida + volta. Usa cache em memória; só chama HTTP para locais novos."""
    if not destination:
        return None
    cache = _get_distances_mem()
    if destination in cache:
        return cache[destination]

    orig_lat, orig_lon = geocode(ORIGIN)
    dest_lat, dest_lon = geocode(destination)
    if None in (orig_lat, orig_lon, dest_lat, dest_lon):
        return None

    url = (
        f"http://router.project-osrm.org/route/v1/driving/"
        f"{orig_lon},{orig_lat};{dest_lon},{dest_lat}?overview=false"
    )
    try:
        time.sleep(0.3)
        r    = requests.get(url, timeout=15)
        data = r.json()
        if data.get('code') == 'Ok':
            km = round(data['routes'][0]['distance'] / 1000 * 2, 1)
            cache[destination] = km
            cache['__version'] = 2
            save_json(DISTANCES_CACHE_FILE, cache)
            return km
    except Exception:
        pass
    return None


# ── dados locais de concertos ─────────────────────────────────────────────────

def _build_concerts_from_local():
    """Constrói a lista de concertos a partir dos dados locais — sem chamadas de rede."""
    base         = load_json(CONCERTS_BASE_FILE, {'events': {}})
    concert_data = load_json(CONCERT_DATA_FILE, {})
    artist_base  = _build_artist_base_cachet()
    distances    = _get_distances_mem()

    concerts_list = []
    events_sorted = sorted(base.get('events', {}).items(),
                           key=lambda x: x[1].get('start', ''))

    for event_id, ev in events_sorted:
        start_raw = ev.get('start', '')
        try:
            if 'T' in start_raw:
                dt = datetime.fromisoformat(start_raw.replace('Z', '+00:00'))
            else:
                dt = datetime.strptime(start_raw, '%Y-%m-%d').replace(tzinfo=timezone.utc)
            date_str = dt.strftime('%d/%m/%Y')
            time_str = dt.strftime('%H:%M') if 'T' in start_raw else ''
            year, month = dt.year, dt.month
        except Exception:
            date_str, time_str, year, month = start_raw, '', 0, 0

        a_p, ev_p, lo_p, su_p = parse_event_title(ev.get('summary', ''))
        ov = concert_data.get(event_id, {})

        artista    = ov.get('artista',    a_p)
        evento     = ov.get('evento',     ev_p)
        local      = ov.get('local',      lo_p)
        substituto = ov.get('substituto', su_p)
        cachet     = ov.get('cachet', '') or artist_base.get(artista, '')
        if substituto:
            cachet = '0'
        # usa apenas o cache em memória — sem HTTP
        km = distances.get(local) if local else None

        concerts_list.append({
            'id': event_id, 'date': date_str, 'time': time_str,
            'year': year, 'month': month,
            'artista': artista, 'evento': evento, 'local': local,
            'substituto': substituto, 'cachet': cachet,
            'km': km if km is not None else ''
        })

    return concerts_list


def _get_last_sync():
    return load_json(CONCERTS_BASE_FILE, {}).get('last_sync')


# ── rotas de autenticação ─────────────────────────────────────────────────────

@app.route('/')
def index():
    if not os.path.exists(CREDENTIALS_FILE):
        return render_template('setup.html', step='credentials')
    if not get_credentials():
        return redirect(url_for('auth'))
    config = load_json('data/config.json', {})
    if not config.get('calendar_id'):
        return redirect(url_for('choose_calendar'))
    return redirect(url_for('concerts'))


@app.route('/auth')
def auth():
    if not os.path.exists(CREDENTIALS_FILE):
        return render_template('setup.html', step='credentials')
    return render_template('auth.html')


@app.route('/auth/start')
def auth_start():
    import webbrowser
    flow = Flow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    auth_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    save_json(OAUTH_STATE_FILE, {'state': state})
    webbrowser.open(auth_url)
    return jsonify({'ok': True})


@app.route('/auth/status')
def auth_status():
    return jsonify({'done': get_credentials() is not None})


@app.route('/oauth/callback')
def oauth_callback():
    state_data = load_json(OAUTH_STATE_FILE, {})
    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE, scopes=SCOPES,
        state=state_data.get('state'), redirect_uri=REDIRECT_URI
    )
    flow.fetch_token(authorization_response=request.url)
    save_credentials(flow.credentials)
    if os.path.exists(OAUTH_STATE_FILE):
        os.remove(OAUTH_STATE_FILE)
    return render_template('auth_done.html')


@app.route('/calendars')
def choose_calendar():
    service = get_service()
    if not service:
        return redirect(url_for('auth'))
    result    = service.calendarList().list().execute()
    calendars = [{'id': c['id'], 'name': c.get('summary', c['id'])} for c in result.get('items', [])]
    return render_template('calendars.html', calendars=calendars)


@app.route('/calendars/select', methods=['POST'])
def select_calendar():
    calendar_id = request.form.get('calendar_id')
    service     = get_service()
    cal_name    = calendar_id
    try:
        cal_name = service.calendars().get(calendarId=calendar_id).execute().get('summary', calendar_id)
    except Exception:
        pass
    config = load_json('data/config.json', {})
    config['calendar_id']   = calendar_id
    config['calendar_name'] = cal_name
    save_json('data/config.json', config)
    return redirect(url_for('concerts'))


@app.route('/change_calendar')
def change_calendar():
    config = load_json('data/config.json', {})
    config.pop('calendar_id', None)
    save_json('data/config.json', config)
    return redirect(url_for('choose_calendar'))


# ── sincronização com o Google Calendar ──────────────────────────────────────

@app.route('/api/sync', methods=['POST'])
def api_sync():
    """Vai ao Google Calendar, traz eventos novos/alterados e guarda localmente."""
    try:
        service = get_service()
        if not service:
            return jsonify({'ok': False, 'error': 'sem autenticação'})

        config      = load_json('data/config.json', {})
        calendar_id = config.get('calendar_id')
        if not calendar_id:
            return jsonify({'ok': False, 'error': 'calendário não configurado'})

        now      = datetime.now(timezone.utc)
        time_min = now.replace(year=now.year - 3, month=1, day=1,
                               hour=0, minute=0, second=0, microsecond=0).isoformat()
        time_max = now.replace(year=now.year + 3, month=12, day=31,
                               hour=23, minute=59, second=59, microsecond=0).isoformat()

        result = service.events().list(
            calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
            maxResults=2500, singleEvents=True, orderBy='startTime'
        ).execute()

        base        = load_json(CONCERTS_BASE_FILE, {'events': {}})
        existing    = base.get('events', {})
        deleted_ids = set(load_json(DELETED_EVENTS_FILE, []))

        added = 0
        for event in result.get('items', []):
            event_id  = event['id']
            if event_id in deleted_ids:
                continue  # apagado pelo utilizador — não repopular
            start_raw = event['start'].get('dateTime', event['start'].get('date', ''))
            summary   = event.get('summary', '')
            if event_id not in existing:
                existing[event_id] = {'start': start_raw, 'summary': summary}
                added += 1
            else:
                # actualiza data e título (podem ter mudado no calendário)
                existing[event_id]['start']   = start_raw
                existing[event_id]['summary'] = summary

        base['events']    = existing
        base['last_sync'] = datetime.now().strftime('%d/%m/%Y %H:%M')
        save_json(CONCERTS_BASE_FILE, base)

        # pré-aquece o cache de distâncias para todos os locais conhecidos
        concert_data = load_json(CONCERT_DATA_FILE, {})
        for event_id, ev in existing.items():
            ov = concert_data.get(event_id, {})
            _, _, lo_p, _ = parse_event_title(ev.get('summary', ''))
            local = ov.get('local', lo_p)
            if local:
                driving_distance_km(local)

        return jsonify({'ok': True, 'added': added, 'total': len(existing)})

    except Exception as e:
        logging.error('ERRO em /api/sync:\n' + traceback.format_exc())
        return jsonify({'ok': False, 'error': str(e)})


# ── páginas principais ────────────────────────────────────────────────────────

def _page_context():
    """Devolve (calendar_name, last_sync) para as páginas principais."""
    config = load_json('data/config.json', {})
    return config.get('calendar_name', ''), _get_last_sync()


@app.route('/concerts')
def concerts():
    try:
        if not get_credentials():
            return redirect(url_for('auth'))
        config = load_json('data/config.json', {})
        if not config.get('calendar_id'):
            return redirect(url_for('choose_calendar'))

        cal_name, last_sync = _page_context()
        lst = _build_concerts_from_local()
        return render_template('concerts.html',
                               concerts=lst,
                               concerts_json=json.dumps(lst),
                               calendar_name=cal_name,
                               last_sync=last_sync)
    except Exception as e:
        tb = traceback.format_exc()
        logging.error('ERRO em /concerts:\n' + tb)
        return render_template('error.html', error=str(e), detail=tb), 500


@app.route('/mapa_km')
def mapa_km():
    try:
        if not get_credentials():
            return redirect(url_for('auth'))
        config = load_json('data/config.json', {})
        if not config.get('calendar_id'):
            return redirect(url_for('choose_calendar'))

        cal_name, last_sync = _page_context()
        lst = _build_concerts_from_local()
        return render_template('mapa_km.html',
                               concerts_json=json.dumps(lst),
                               calendar_name=cal_name,
                               last_sync=last_sync)
    except Exception as e:
        tb = traceback.format_exc()
        logging.error('ERRO em /mapa_km:\n' + tb)
        return render_template('error.html', error=str(e), detail=tb), 500


@app.route('/faturacao')
def faturacao():
    try:
        if not get_credentials():
            return redirect(url_for('auth'))
        config = load_json('data/config.json', {})
        if not config.get('calendar_id'):
            return redirect(url_for('choose_calendar'))

        cal_name, last_sync = _page_context()
        lst = _build_concerts_from_local()
        return render_template('faturacao.html',
                               concerts_json=json.dumps(lst),
                               calendar_name=cal_name,
                               last_sync=last_sync)
    except Exception as e:
        tb = traceback.format_exc()
        logging.error('ERRO em /faturacao:\n' + tb)
        return render_template('error.html', error=str(e), detail=tb), 500


@app.route('/api/update_concert', methods=['POST'])
def update_concert():
    body     = request.get_json()
    event_id = body.get('event_id')
    field    = body.get('field')
    value    = body.get('value', '').strip()

    if field not in {'artista', 'evento', 'local', 'substituto', 'cachet'}:
        return jsonify({'ok': False, 'error': 'campo inválido'})

    data = load_json(CONCERT_DATA_FILE, {})
    if event_id not in data:
        data[event_id] = {}
    data[event_id][field] = value
    save_json(CONCERT_DATA_FILE, data)

    km = None
    if field == 'local':
        km = driving_distance_km(value) if value else None

    return jsonify({'ok': True, 'km': km})


# ── agências ──────────────────────────────────────────────────────────────────

def _norm_artista(a):
    if isinstance(a, str):
        return {'nome': a, 'cachet_base': ''}
    return a

def _artista_names(ag):
    return [_norm_artista(a)['nome'] for a in ag.get('artistas', [])]

def _build_artist_base_cachet():
    lookup = {}
    for ag in load_json(AGENCIES_FILE, {'agencies': []}).get('agencies', []):
        for a in ag.get('artistas', []):
            a = _norm_artista(a)
            if a['nome'] and a['cachet_base']:
                lookup[a['nome']] = a['cachet_base']
    return lookup


@app.route('/agencias')
def agencias():
    if not get_credentials():
        return redirect(url_for('auth'))
    cal_name, last_sync = _page_context()
    return render_template('agencies.html', calendar_name=cal_name, last_sync=last_sync)


@app.route('/api/agencias', methods=['GET'])
def api_get_agencias():
    data = load_json(AGENCIES_FILE, {'agencies': []})
    for ag in data['agencies']:
        ag['artistas'] = [_norm_artista(a) for a in ag.get('artistas', [])]
    return jsonify(data['agencies'])


@app.route('/api/agencias', methods=['POST'])
def api_create_agencia():
    body = request.get_json()
    nome = body.get('nome', '').strip()
    nif  = body.get('nif',  '').strip()
    if not nome:
        return jsonify({'ok': False, 'error': 'Nome obrigatório'})
    data   = load_json(AGENCIES_FILE, {'agencies': []})
    new_ag = {'id': str(uuid.uuid4()), 'nome': nome, 'nif': nif, 'artistas': []}
    data['agencies'].append(new_ag)
    save_json(AGENCIES_FILE, data)
    return jsonify({'ok': True, 'agency': new_ag})


@app.route('/api/agencias/<agency_id>', methods=['PUT'])
def api_update_agencia(agency_id):
    body = request.get_json()
    data = load_json(AGENCIES_FILE, {'agencies': []})
    for ag in data['agencies']:
        if ag['id'] == agency_id:
            ag['nome'] = body.get('nome', ag['nome']).strip()
            ag['nif']  = body.get('nif',  ag['nif']).strip()
            save_json(AGENCIES_FILE, data)
            return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'não encontrada'})


@app.route('/api/agencias/<agency_id>', methods=['DELETE'])
def api_delete_agencia(agency_id):
    data = load_json(AGENCIES_FILE, {'agencies': []})
    data['agencies'] = [a for a in data['agencies'] if a['id'] != agency_id]
    save_json(AGENCIES_FILE, data)
    return jsonify({'ok': True})


@app.route('/api/agencias/<agency_id>/artistas', methods=['POST'])
def api_add_artista(agency_id):
    body        = request.get_json()
    nome        = body.get('nome', '').strip()
    cachet_base = body.get('cachet_base', '').strip()
    if not nome:
        return jsonify({'ok': False, 'error': 'Nome obrigatório'})
    data = load_json(AGENCIES_FILE, {'agencies': []})
    for ag in data['agencies']:
        if ag['id'] == agency_id:
            ag['artistas'] = [_norm_artista(a) for a in ag.get('artistas', [])]
            if nome not in _artista_names(ag):
                ag['artistas'].append({'nome': nome, 'cachet_base': cachet_base})
            save_json(AGENCIES_FILE, data)
            return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'não encontrada'})


@app.route('/api/agencias/<agency_id>/artistas', methods=['DELETE'])
def api_remove_artista(agency_id):
    body = request.get_json()
    nome = body.get('nome', '').strip()
    data = load_json(AGENCIES_FILE, {'agencies': []})
    for ag in data['agencies']:
        if ag['id'] == agency_id:
            ag['artistas'] = [_norm_artista(a) for a in ag.get('artistas', [])]
            ag['artistas'] = [a for a in ag['artistas'] if a['nome'] != nome]
            save_json(AGENCIES_FILE, data)
            return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'não encontrada'})


@app.route('/api/agencias/<agency_id>/artistas/cachet', methods=['PUT'])
def api_update_artista_cachet(agency_id):
    body        = request.get_json()
    nome        = body.get('nome', '').strip()
    cachet_base = body.get('cachet_base', '').strip()
    data = load_json(AGENCIES_FILE, {'agencies': []})
    for ag in data['agencies']:
        if ag['id'] == agency_id:
            ag['artistas'] = [_norm_artista(a) for a in ag.get('artistas', [])]
            for a in ag['artistas']:
                if a['nome'] == nome:
                    a['cachet_base'] = cachet_base
            save_json(AGENCIES_FILE, data)
            return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'não encontrada'})


@app.route('/api/agencias/<agency_id>/artistas/refresh', methods=['POST'])
def api_refresh_artista(agency_id):
    """Aplica cachet_base a todos os concertos futuros do artista (usa dados locais)."""
    body        = request.get_json()
    nome        = body.get('nome', '').strip()
    cachet_base = body.get('cachet_base', '').strip()
    if not nome or not cachet_base:
        return jsonify({'ok': False, 'error': 'dados incompletos'})

    base         = load_json(CONCERTS_BASE_FILE, {'events': {}})
    concert_data = load_json(CONCERT_DATA_FILE, {})
    today        = datetime.now(timezone.utc)
    updated      = 0

    for event_id, ev in base.get('events', {}).items():
        start_raw = ev.get('start', '')
        try:
            if 'T' in start_raw:
                dt = datetime.fromisoformat(start_raw.replace('Z', '+00:00'))
            else:
                dt = datetime.strptime(start_raw, '%Y-%m-%d').replace(tzinfo=timezone.utc)
            if dt < today:
                continue
        except Exception:
            continue

        ov = concert_data.get(event_id, {})
        a_p, _, _, _ = parse_event_title(ev.get('summary', ''))
        artist = ov.get('artista', a_p)
        if artist == nome:
            if event_id not in concert_data:
                concert_data[event_id] = {}
            concert_data[event_id]['cachet'] = cachet_base
            updated += 1

    save_json(CONCERT_DATA_FILE, concert_data)
    return jsonify({'ok': True, 'updated': updated})


@app.route('/api/add_concert', methods=['POST'])
def api_add_concert():
    body = request.get_json()
    date_str = body.get('date', '').strip()   # formato yyyy-mm-dd (input type=date)
    time_str = body.get('time', '').strip()   # HH:MM ou ''
    if not date_str:
        return jsonify({'ok': False, 'error': 'Data obrigatória'})

    try:
        if time_str:
            start_iso = f"{date_str}T{time_str}:00"
        else:
            start_iso = date_str
    except Exception:
        return jsonify({'ok': False, 'error': 'Data inválida'})

    event_id = 'local_' + str(uuid.uuid4())

    base = load_json(CONCERTS_BASE_FILE, {'events': {}})
    base.setdefault('events', {})[event_id] = {'start': start_iso, 'summary': ''}
    save_json(CONCERTS_BASE_FILE, base)

    overrides = {
        'artista':    body.get('artista',    '').strip(),
        'evento':     body.get('evento',     '').strip(),
        'local':      body.get('local',      '').strip(),
        'substituto': body.get('substituto', '').strip(),
        'cachet':     body.get('cachet',     '').strip(),
    }
    data = load_json(CONCERT_DATA_FILE, {})
    data[event_id] = overrides
    save_json(CONCERT_DATA_FILE, data)

    if overrides['local']:
        driving_distance_km(overrides['local'])

    return jsonify({'ok': True, 'event_id': event_id})


@app.route('/api/delete_concert', methods=['POST'])
def api_delete_concert():
    body     = request.get_json()
    event_id = body.get('event_id', '').strip()
    if not event_id:
        return jsonify({'ok': False, 'error': 'ID em falta'})

    base = load_json(CONCERTS_BASE_FILE, {'events': {}})
    base.get('events', {}).pop(event_id, None)
    save_json(CONCERTS_BASE_FILE, base)

    data = load_json(CONCERT_DATA_FILE, {})
    data.pop(event_id, None)
    save_json(CONCERT_DATA_FILE, data)

    # eventos do Google Calendar: guardar na lista de apagados para não
    # reaparecerem em syncs futuros
    if not event_id.startswith('local_'):
        deleted = load_json(DELETED_EVENTS_FILE, [])
        if event_id not in deleted:
            deleted.append(event_id)
        save_json(DELETED_EVENTS_FILE, deleted)

    return jsonify({'ok': True})


@app.route('/api/conflitos_count')
def api_conflitos_count():
    cur_year = datetime.now().year
    lst = _build_concerts_from_local()
    by_date = {}
    for c in lst:
        if c['year'] == cur_year:
            by_date.setdefault(c['date'], []).append(c)
    count = sum(
        sem_sub
        for evs in by_date.values()
        if (sem_sub := sum(1 for e in evs if not e['substituto'])) >= 2
    )
    return jsonify({'count': count})


@app.route('/api/export_csv', methods=['POST'])
def api_export_csv():
    body     = request.get_json()
    filename = re.sub(r'[^\w\-_\. ]', '_', body.get('filename', 'export.csv'))
    content  = body.get('content', '')
    path     = os.path.join(os.path.expanduser('~/Downloads'), filename)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'ok': True, 'path': path})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/artistas')
def api_artistas():
    """Lista de artistas únicos extraída dos dados locais."""
    lst     = _build_concerts_from_local()
    artists = sorted({c['artista'] for c in lst if c.get('artista')})
    return jsonify(artists)


@app.route('/conflitos')
def conflitos():
    try:
        if not get_credentials():
            return redirect(url_for('auth'))
        config = load_json('data/config.json', {})
        if not config.get('calendar_id'):
            return redirect(url_for('choose_calendar'))

        cal_name, last_sync = _page_context()
        lst = _build_concerts_from_local()
        return render_template('conflitos.html',
                               concerts_json=json.dumps(lst),
                               calendar_name=cal_name,
                               last_sync=last_sync)
    except Exception as e:
        tb = traceback.format_exc()
        logging.error('ERRO em /conflitos:\n' + tb)
        return render_template('error.html', error=str(e), detail=tb), 500


# ── contabilidade ─────────────────────────────────────────────────────────────

_CONTAB_DEFAULTS = {
    'service_account_path': '/Users/gilfigueiredo/FATURAS/service_account.json',
    'sheet_id':             '1TCgrgH2lw_eEFDYDmNWuVfKThSO5io2x6ngJeXz9YYw',
    'sheet_name':           'Faturas',
    'taxa_iva_rendimentos':  23,
    'irc_taxa_reduzida':     16,
    'irc_limiar_reduzida':   50000,
    'irc_taxa_normal':       21,
    'taxa_derrama':          1.5,
}

# art. 21.º CIVA — factor de dedutibilidade do IVA por categoria de despesa
# 0.0 = não dedutível  |  0.5 = 50%  |  omitido = 100%
_IVA_FACTOR = {
    'Alimentação e Bebidas':        0.0,   # art. 21.º n.º 1 al. d)
    'Alojamento e Hotelaria':       0.0,   # art. 21.º n.º 1 al. d)
    'Combustíveis e Lubrificantes': 0.5,   # art. 21.º n.º 1 al. a) — viaturas ligeiras
}

# art. 88.º n.º 7 CIRC — tributação autónoma 10% sobre despesas de representação
_REPRESENTACAO_CATS = {'Alimentação e Bebidas', 'Alojamento e Hotelaria'}
_TAXA_TRIB_AUTONOMA = 0.10
# art. 88.º n.º 9 CIRC — tributação autónoma 5% sobre compensações por uso de viatura própria (km)
_TAXA_TA_KM = 0.05

# SNC (DL 158/2009) — mapeamento de categoria de despesa → conta SNC
_SNC_MAP = {
    'Telecomunicações':             ('6228', 'Comunicação'),
    'Electricidade e Energia':      ('6221', 'Energia e fluídos'),
    'Água e Saneamento':            ('6221', 'Energia e fluídos'),
    'Combustíveis e Lubrificantes': ('6226', 'Combustíveis'),
    'Material de Escritório':       ('6224', 'Material de escritório'),
    'Alimentação e Bebidas':        ('6227', 'Deslocações, estadas e transportes'),
    'Alojamento e Hotelaria':       ('6227', 'Deslocações, estadas e transportes'),
    'Transportes e Deslocações':    ('6227', 'Deslocações, estadas e transportes'),
    'Software e Tecnologia':        ('628',  'Outros FSE'),
    'Publicidade e Marketing':      ('625',  'Publicidade e propaganda'),
    'Seguros':                      ('6229', 'Seguros'),
    'Contabilidade e Consultoria':  ('6233', 'Honorários'),
    'Serviços Jurídicos':           ('6232', 'Contencioso e notariado'),
    'Saúde e Bem-estar':            ('628',  'Outros FSE'),
    'Formação e Educação':          ('628',  'Outros FSE'),
    'Manutenção e Reparação':       ('624',  'Conservação e reparação'),
    'Rendas e Alugueres':           ('6299', 'Rendas e alugueres'),
    'Outros':                       ('628',  'Outros FSE'),
}


def _despesa_key(row):
    """Chave única para uma linha de despesa (usada para overrides de categoria)."""
    return '|'.join([
        str(row.get('data_fatura', '')),
        str(row.get('fornecedor', '')),
        str(row.get('numero_fatura', '')),
    ])


def _enrich_despesas(rows):
    """Enriquece cada linha de despesa com classificação SNC e cálculos fiscais."""
    result = []
    for row in rows:
        cat        = row.get('tipo_despesa') or 'Outros'
        fator      = _IVA_FACTOR.get(cat, 1.0)
        iva        = _to_float(row.get('iva'))
        base       = _to_float(row.get('base_tributavel'))
        iva_ded    = round(iva * fator, 2)
        iva_nao    = round(iva - iva_ded, 2)
        custo_irc  = round(base + iva_nao, 2)
        is_rep     = cat in _REPRESENTACAO_CATS
        ta_val     = round(custo_irc * _TAXA_TRIB_AUTONOMA, 2) if is_rep else 0.0
        snc_conta, snc_desc = _SNC_MAP.get(cat, ('628', 'Outros FSE'))
        r = dict(row)
        r.update({
            '_key':                    _despesa_key(row),
            'snc_conta':               snc_conta,
            'snc_descricao':           snc_desc,
            'iva_fator':               fator,
            'iva_deducivel_calc':      iva_ded,
            'iva_nao_deducivel':       iva_nao,
            'custo_irc':               custo_irc,
            'is_representacao':        is_rep,
            'tributacao_autonoma_val': ta_val,
        })
        result.append(r)
    return result


def _get_contab_config():
    cfg = dict(_CONTAB_DEFAULTS)
    cfg.update(load_json(CONTAB_CONFIG_FILE, {}))
    return cfg


def _to_float(v):
    if v is None or v == '':
        return 0.0
    try:
        return float(str(v).replace(',', '.'))
    except Exception:
        return 0.0


# Época do Google Sheets: dias desde 30/12/1899
_SHEETS_EPOCH = datetime(1899, 12, 30)

def _sheets_date(v):
    """Converte número de série do Sheets (UNFORMATTED_VALUE) para 'YYYY-MM-DD'.
    Se já for string no formato esperado, devolve como está."""
    if isinstance(v, (int, float)) and v > 0:
        return (_SHEETS_EPOCH + timedelta(days=int(v))).strftime('%Y-%m-%d')
    s = str(v).strip()
    if not s:
        return ''
    # fallback: DD/MM/YYYY → YYYY-MM-DD
    if '/' in s:
        try:
            return datetime.strptime(s, '%d/%m/%Y').strftime('%Y-%m-%d')
        except Exception:
            pass
    return s


def _calc_irc(resultado, cfg):
    """Calcula estimativa IRC PME com taxa escalonada + derrama."""
    if resultado <= 0:
        return 0.0, 0.0, 0.0
    limiar      = cfg['irc_limiar_reduzida']
    taxa_red    = cfg['irc_taxa_reduzida'] / 100
    taxa_norm   = cfg['irc_taxa_normal'] / 100
    taxa_derrama = cfg['taxa_derrama'] / 100

    base_red  = min(resultado, limiar)
    base_norm = max(0.0, resultado - limiar)
    irc       = round(base_red * taxa_red + base_norm * taxa_norm, 2)
    derrama   = round(resultado * taxa_derrama, 2)
    return irc, derrama, round(irc + derrama, 2)


def _build_contabilidade():
    """Agrega rendimentos + despesas + km por (year, month)."""
    cfg        = _get_contab_config()
    taxa_iva   = cfg['taxa_iva_rendimentos'] / 100
    KM_RATE    = 0.40

    concerts   = _build_concerts_from_local()

    # rendimentos e km por mês
    rend_mes = {}   # (y,m) -> {'base': float, 'iva': float}
    km_mes   = {}   # (y,m) -> float  (valor em €)

    for c in concerts:
        if c.get('substituto'):
            continue
        try:
            cachet = _to_float(c.get('cachet') or 0)
        except Exception:
            cachet = 0.0
        key = (c['year'], c['month'])
        if cachet > 0:
            rend_mes.setdefault(key, {'base': 0.0, 'iva': 0.0})
            rend_mes[key]['base'] += cachet
            rend_mes[key]['iva']  += round(cachet * taxa_iva, 4)
        try:
            km = _to_float(c.get('km') or 0)
            if km > 0:
                km_mes[key] = km_mes.get(key, 0.0) + km * KM_RATE
        except Exception:
            pass

    # despesas por mês
    despesas_data = load_json(DESPESAS_FILE, {'rows': []})
    gast_mes = {}   # (y,m) -> dict

    for row in despesas_data.get('rows', []):
        data_str = row.get('data_fatura', '')
        try:
            dt  = datetime.strptime(data_str, '%Y-%m-%d')
            key = (dt.year, dt.month)
        except Exception:
            continue
        gast_mes.setdefault(key, {
            'base': 0.0, 'iva': 0.0,
            'iva_6': 0.0, 'iva_13': 0.0, 'iva_23': 0.0,
            'trib_autonoma': 0.0, 'gastos_rep': 0.0,
            'por_categoria': {}
        })
        base   = _to_float(row.get('base_tributavel'))
        iva    = _to_float(row.get('iva'))
        iva_6  = _to_float(row.get('iva_6'))
        iva_13 = _to_float(row.get('iva_13'))
        iva_23 = _to_float(row.get('iva_23'))
        cat    = row.get('tipo_despesa') or 'Outros'

        # art. 21.º CIVA: aplicar factor de dedutibilidade por categoria
        fator       = _IVA_FACTOR.get(cat, 1.0)
        iva_ded     = iva    * fator
        iva_nao_ded = iva    - iva_ded
        iva_6_ded   = iva_6  * fator
        iva_13_ded  = iva_13 * fator
        iva_23_ded  = iva_23 * fator
        # custo IRC = base + IVA não recuperado (o IVA não dedutível é custo real)
        custo_irc   = base + iva_nao_ded

        # art. 88.º n.º 7 CIRC: tributação autónoma 10% sobre despesas de representação
        trib_auto = custo_irc * _TAXA_TRIB_AUTONOMA if cat in _REPRESENTACAO_CATS else 0.0

        gast_mes[key]['base']          += custo_irc
        gast_mes[key]['iva']           += iva_ded
        gast_mes[key]['iva_6']         += iva_6_ded
        gast_mes[key]['iva_13']        += iva_13_ded
        gast_mes[key]['iva_23']        += iva_23_ded
        gast_mes[key]['trib_autonoma'] += trib_auto
        if cat in _REPRESENTACAO_CATS:
            gast_mes[key]['gastos_rep'] += custo_irc
        gast_mes[key]['por_categoria'][cat] = \
            gast_mes[key]['por_categoria'].get(cat, 0.0) + custo_irc

    all_keys = sorted(set(rend_mes) | set(gast_mes) | set(km_mes))
    result   = []

    for key in all_keys:
        y, m  = key
        rend  = rend_mes.get(key, {'base': 0.0, 'iva': 0.0})
        gast  = gast_mes.get(key, {
            'base': 0.0, 'iva': 0.0,
            'iva_6': 0.0, 'iva_13': 0.0, 'iva_23': 0.0,
            'trib_autonoma': 0.0, 'gastos_rep': 0.0,
            'por_categoria': {}
        })
        km_val     = round(km_mes.get(key, 0.0), 2)
        g_base     = round(gast['base'], 2)
        g_total    = round(g_base + km_val, 2)
        r_base     = round(rend['base'], 2)
        iva_liq    = round(rend['iva'], 2)
        iva_ded    = round(gast['iva'], 2)
        iva_saldo  = round(iva_liq - iva_ded, 2)
        resultado  = round(r_base - g_total, 2)
        ta_rep     = round(gast['trib_autonoma'], 2)
        ta_km      = round(km_val * _TAXA_TA_KM, 2)   # art. 88.º n.º 9 CIRC — 5%
        trib_auto  = round(ta_rep + ta_km, 2)
        gastos_rep = round(gast['gastos_rep'], 2)
        irc, derrama, irc_subtotal = _calc_irc(resultado, cfg)
        irc_total  = round(irc_subtotal + trib_auto, 2)

        result.append({
            'year': y, 'month': m,
            'rendimentos':           r_base,
            'iva_liquidado':         iva_liq,
            'gastos_despesas':       g_base,
            'gastos_km':             km_val,
            'gastos_total':          g_total,
            'iva_deducivel':         iva_ded,
            'iva_deducivel_6':       round(gast['iva_6'],  2),
            'iva_deducivel_13':      round(gast['iva_13'], 2),
            'iva_deducivel_23':      round(gast['iva_23'], 2),
            'iva_saldo':             iva_saldo,
            'resultado':             resultado,
            'irc_estimado':          irc,
            'derrama_estimada':      derrama,
            'irc_subtotal':          round(irc_subtotal, 2),
            'tributacao_autonoma':   trib_auto,
            'ta_representacao':      ta_rep,
            'ta_km':                 ta_km,
            'gastos_representacao':  gastos_rep,
            'irc_total':             irc_total,
            'por_categoria':         gast['por_categoria'],
        })

    return result


@app.route('/iva')
def iva():
    try:
        if not get_credentials():
            return redirect(url_for('auth'))
        config = load_json('data/config.json', {})
        if not config.get('calendar_id'):
            return redirect(url_for('choose_calendar'))
        cal_name, last_sync = _page_context()
        dados    = _build_contabilidade()
        cfg      = _get_contab_config()
        despesas = load_json(DESPESAS_FILE, {})
        return render_template('iva.html',
                               contab_json=json.dumps(dados),
                               contab_config=cfg,
                               despesas_last_sync=despesas.get('last_sync', ''),
                               calendar_name=cal_name,
                               last_sync=last_sync)
    except Exception as e:
        tb = traceback.format_exc()
        logging.error('ERRO em /iva:\n' + tb)
        return render_template('error.html', error=str(e), detail=tb), 500


@app.route('/conta_corrente')
def conta_corrente():
    try:
        if not get_credentials():
            return redirect(url_for('auth'))
        config = load_json('data/config.json', {})
        if not config.get('calendar_id'):
            return redirect(url_for('choose_calendar'))
        cal_name, last_sync = _page_context()
        dados    = _build_contabilidade()
        cfg      = _get_contab_config()
        despesas = load_json(DESPESAS_FILE, {})
        return render_template('conta_corrente.html',
                               contab_json=json.dumps(dados),
                               contab_config=cfg,
                               despesas_last_sync=despesas.get('last_sync', ''),
                               calendar_name=cal_name,
                               last_sync=last_sync)
    except Exception as e:
        tb = traceback.format_exc()
        logging.error('ERRO em /conta_corrente:\n' + tb)
        return render_template('error.html', error=str(e), detail=tb), 500


@app.route('/api/sync_despesas', methods=['POST'])
def api_sync_despesas():
    try:
        import gspread
        from google.oauth2.service_account import Credentials as SACredentials

        cfg     = _get_contab_config()
        sa_path = cfg['service_account_path']
        if not os.path.exists(sa_path):
            return jsonify({'ok': False,
                            'error': f'Service account não encontrado: {sa_path}'})

        creds  = SACredentials.from_service_account_file(
            sa_path,
            scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
        )
        client = gspread.authorize(creds)
        ws     = client.open_by_key(cfg['sheet_id']).worksheet(cfg['sheet_name'])
        rows   = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')

        normalized = []
        for r in rows:
            normalized.append({
                'data_fatura':    _sheets_date(r.get('Data Fatura', '')),
                'fornecedor':     str(r.get('Fornecedor', '')),
                'nif':            str(r.get('NIF', '')),
                'numero_fatura':  str(r.get('Numero Fatura', '')),
                'descricao':      str(r.get('Descricao', '')),
                'tipo_despesa':   str(r.get('Tipo Despesa', '')),
                'base_tributavel': _to_float(r.get('Base Tributavel')),
                'base_6':         _to_float(r.get('Base 6%')),
                'iva_6':          _to_float(r.get('IVA 6%')),
                'base_13':        _to_float(r.get('Base 13%')),
                'iva_13':         _to_float(r.get('IVA 13%')),
                'base_23':        _to_float(r.get('Base 23%')),
                'iva_23':         _to_float(r.get('IVA 23%')),
                'iva':            _to_float(r.get('IVA')),
                'total':          _to_float(r.get('Total')),
                'moeda':          str(r.get('Moeda', 'EUR')),
                'ficheiro':       str(r.get('Ficheiro', '')),
            })

        save_json(DESPESAS_FILE, {
            'last_sync': datetime.now().strftime('%d/%m/%Y %H:%M'),
            'rows':      normalized,
        })
        return jsonify({'ok': True, 'count': len(normalized)})

    except Exception as e:
        logging.error('ERRO em /api/sync_despesas:\n' + traceback.format_exc())
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/despesas')
def despesas_page():
    try:
        if not get_credentials():
            return redirect(url_for('auth'))
        config = load_json('data/config.json', {})
        if not config.get('calendar_id'):
            return redirect(url_for('choose_calendar'))
        cal_name, last_sync = _page_context()
        despesas_data = load_json(DESPESAS_FILE, {'rows': [], 'last_sync': ''})
        overrides = load_json(DESPESAS_OVERRIDES_FILE, {})
        raw_rows = despesas_data.get('rows', [])
        for r in raw_rows:
            k = _despesa_key(r)
            if k in overrides:
                r['tipo_despesa'] = overrides[k]
        rows = _enrich_despesas(raw_rows)
        return render_template('despesas.html',
                               despesas_json=json.dumps(rows),
                               despesas_last_sync=despesas_data.get('last_sync', ''),
                               calendar_name=cal_name,
                               last_sync=last_sync)
    except Exception as e:
        tb = traceback.format_exc()
        logging.error('ERRO em /despesas:\n' + tb)
        return render_template('error.html', error=str(e), detail=tb), 500


@app.route('/api/despesas/set_categoria', methods=['POST'])
def api_despesas_set_categoria():
    try:
        data      = request.get_json()
        key       = (data.get('key') or '').strip()
        categoria = (data.get('categoria') or '').strip()
        if not key or not categoria:
            return jsonify({'ok': False, 'error': 'key e categoria são obrigatórios'})
        if categoria not in _SNC_MAP:
            return jsonify({'ok': False, 'error': 'Categoria inválida'})
        overrides = load_json(DESPESAS_OVERRIDES_FILE, {})
        overrides[key] = categoria
        save_json(DESPESAS_OVERRIDES_FILE, overrides)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/despesas/setup_sheets_dropdown', methods=['POST'])
def api_setup_sheets_dropdown():
    try:
        import gspread
        from google.oauth2.service_account import Credentials as SACredentials

        cfg     = _get_contab_config()
        sa_path = cfg.get('service_account_path', '')
        sheet_id  = cfg.get('sheet_id', '')
        sheet_name = cfg.get('sheet_name', 'Faturas')

        if not sa_path or not sheet_id:
            return jsonify({'ok': False, 'error': 'Configure o service account e Sheet ID primeiro (tab Conta Corrente → ⚙)'})
        if not os.path.exists(sa_path):
            return jsonify({'ok': False, 'error': f'Service account não encontrado: {sa_path}'})

        creds = SACredentials.from_service_account_file(
            sa_path,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(sheet_id)
        ws = spreadsheet.worksheet(sheet_name)

        # detecta índice da coluna "Tipo Despesa" no cabeçalho
        headers = ws.row_values(1)
        if 'Tipo Despesa' not in headers:
            return jsonify({'ok': False, 'error': 'Coluna "Tipo Despesa" não encontrada no sheet'})
        col_idx = headers.index('Tipo Despesa')   # 0-based

        categorias = list(_SNC_MAP.keys())
        body = {
            'requests': [{
                'setDataValidation': {
                    'range': {
                        'sheetId':           ws.id,
                        'startRowIndex':     1,          # salta o cabeçalho
                        'startColumnIndex':  col_idx,
                        'endColumnIndex':    col_idx + 1,
                    },
                    'rule': {
                        'condition': {
                            'type':   'ONE_OF_LIST',
                            'values': [{'userEnteredValue': c} for c in categorias],
                        },
                        'showCustomUi': True,
                        'strict':       False,
                    },
                }
            }]
        }
        spreadsheet.batch_update(body)
        return jsonify({'ok': True, 'message': f'Dropdown configurado ({len(categorias)} categorias)'})

    except Exception as e:
        logging.error('ERRO em /api/despesas/setup_sheets_dropdown:\n' + traceback.format_exc())
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/contabilidade')
def api_contabilidade():
    return jsonify(_build_contabilidade())


@app.route('/api/contab_config', methods=['GET'])
def api_get_contab_config():
    return jsonify(_get_contab_config())


@app.route('/api/contab_config', methods=['PUT'])
def api_put_contab_config():
    body    = request.get_json()
    allowed = {
        'taxa_iva_rendimentos', 'irc_taxa_reduzida', 'irc_limiar_reduzida',
        'irc_taxa_normal', 'taxa_derrama', 'service_account_path',
        'sheet_id', 'sheet_name',
    }
    cfg = _get_contab_config()
    for k, v in body.items():
        if k in allowed:
            cfg[k] = v
    save_json(CONTAB_CONFIG_FILE, cfg)
    return jsonify({'ok': True})


# ── lançamento ────────────────────────────────────────────────────────────────

def run_flask():
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    app.run(host=APP_HOST, port=APP_PORT, debug=False, use_reloader=False)


def wait_for_flask(timeout=20):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(APP_URL, timeout=1)
            return True
        except Exception:
            time.sleep(0.15)
    return False


if __name__ == '__main__':
    import threading
    import webview

    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    wait_for_flask()

    window = webview.create_window(
        title='Gestão de Empresa',
        url=APP_URL,
        width=1300,
        height=800,
        min_size=(900, 600),
    )
    webview.start()
