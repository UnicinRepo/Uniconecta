import boto3
import os
import re
import time
import json
import hmac
import base64
import hashlib
import logging
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Key

# --- Logger Configuration ---
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Environment Variable Configurations ---
# Aceita as duas convenções de nome (com e sem TARGET) para casar com a infra.
DYNAMODB_USERS_TABLE_NAME = os.environ.get('AWS_DYNAMODB_TABLE_NAME_0') or os.environ.get('AWS_DYNAMODB_TABLE_TARGET_NAME_0')
AWS_REGION = os.environ.get('REGION', 'us-east-1')
JWT_SECRET = os.environ.get('JWT_SECRET')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL')
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH')  # pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>
JWT_TTL_SECONDS = int(os.environ.get('JWT_TTL_SECONDS', '28800'))  # 8h
ALLOWED_ORIGIN = os.environ.get('ALLOWED_ORIGIN', '*')
# Prefixo que o CloudFront encaminha (behavior). Removido antes de rotear.
PATH_PREFIX = os.environ.get('API_PATH_PREFIX', '/behavior-dev')
# GSI por status para listar sem Scan (Query só lê os itens de cada status).
STATUS_INDEX = os.environ.get('STATUS_INDEX_NAME', 'StatusIndex')
LISTABLE_STATUSES = ('pending', 'approved', 'rejected')

# --- Admins (multi-admin) ---
# ADMINS_JSON = lista [{"email","hash"}]; mesclado com o admin único (fallback).
ADMINS = {}
try:
    for a in json.loads(os.environ.get('ADMINS_JSON', '[]')):
        if a.get('email') and a.get('hash'):
            ADMINS[a['email'].strip().lower()] = a['hash']
except Exception as e:
    logger.warning(f"ADMINS_JSON inválido: {e}")
if ADMIN_EMAIL and ADMIN_PASSWORD_HASH:
    ADMINS.setdefault(ADMIN_EMAIL.strip().lower(), ADMIN_PASSWORD_HASH)

# --- Initial Validations ---
if not all([DYNAMODB_USERS_TABLE_NAME, JWT_SECRET]) or not ADMINS:
    logger.error("CRITICAL ERROR: Essential environment variables not configured.")
    raise ValueError("Incomplete environment configuration.")

# --- Boto3 Clients ---
_DDB_ENDPOINT = os.environ.get('DYNAMODB_ENDPOINT_URL') or None
dynamodb_resource = boto3.resource("dynamodb", region_name=AWS_REGION, endpoint_url=_DDB_ENDPOINT)
users_table = dynamodb_resource.Table(DYNAMODB_USERS_TABLE_NAME)

# --- Campos do cadastro ---
REQUIRED_FIELDS = [
    'nome_completo', 'idade', 'endereco', 'bairro', 'whatsapp',
    'voluntario_ic', 'contato_familia', 'contato_ccci',
]
OPTIONAL_TEXT_FIELDS = [
    'plano_saude', 'hospital_preferencia', 'tem_pet', 'cadastro_ubs',
    'pessoa_chaves', 'profissional_saude', 'alergia_intolerancia',
    'tipo_sanguineo', 'medicacao_continua',
]
EDITABLE_FIELDS = REQUIRED_FIELDS + OPTIONAL_TEXT_FIELDS + ['mora_sozinho']
INTERNAL_FIELDS = {'ID', 'conversation_state', 'state_last_updated', 'lista_contatos'}


# =====================================================================
#  Helpers — telefone
# =====================================================================

def canonical_phone(raw):
    """'(31) 98615-5781' -> '5531986155781' (só dígitos, DDI 55)."""
    if not raw:
        return None
    digits = re.sub(r'\D', '', str(raw)).lstrip('0')
    if not digits:
        return None
    if digits.startswith('55') and len(digits) >= 12:
        return digits
    if len(digits) in (10, 11):
        return '55' + digits
    return digits


def extract_contact_numbers(*free_text_fields):
    found = []
    for text in free_text_fields:
        if not text:
            continue
        for match in re.findall(r'(?:\+?\d[\d\s().-]{8,}\d)', str(text)):
            phone = canonical_phone(match)
            if phone and phone not in found:
                found.append(phone)
    return found


# =====================================================================
#  Helpers — JWT (HS256, stdlib) e senha (PBKDF2, stdlib)
# =====================================================================

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4))


def jwt_encode(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(',', ':')).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(',', ':')).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


def jwt_decode(token: str):
    try:
        header_b64, payload_b64, signature_b64 = token.split('.')
        signing_input = f"{header_b64}.{payload_b64}".encode()
        expected = hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(signature_b64)):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if int(payload.get('exp', 0)) < int(time.time()):
            return None
        return payload
    except Exception as e:
        logger.warning(f"JWT decode failed: {e}")
        return None


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, hash_b64 = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        derived = hashlib.pbkdf2_hmac('sha256', password.encode(),
                                      base64.b64decode(salt_b64), int(iterations))
        return hmac.compare_digest(derived, base64.b64decode(hash_b64))
    except Exception as e:
        logger.warning(f"Password verification failed: {e}")
        return False


# =====================================================================
#  Helpers — HTTP (JSON)
# =====================================================================

CORS_HEADERS = {
    'Access-Control-Allow-Origin': ALLOWED_ORIGIN,
    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
    'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
    'Content-Type': 'application/json',
}


def respond(status_code, body=None):
    return {
        'statusCode': status_code,
        'headers': CORS_HEADERS,
        'body': '' if body is None else json.dumps(body, default=str),
    }


def error(status_code, message):
    return respond(status_code, {'error': message})


def _headers_lower(event):
    return {k.lower(): v for k, v in (event.get('headers') or {}).items()}


def get_auth_payload(event):
    auth = _headers_lower(event).get('authorization', '')
    if not auth.startswith('Bearer '):
        return None
    return jwt_decode(auth[len('Bearer '):].strip())


def parse_body(event):
    raw = event.get('body')
    if raw is None or raw == '':
        return {}
    if event.get('isBase64Encoded'):
        raw = base64.b64decode(raw).decode('utf-8')
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def item_to_response(item):
    """Item DynamoDB -> shape do frontend (expõe 'id', oculta internos)."""
    if not item:
        return None
    result = {'id': item.get('ID')}
    for key, value in item.items():
        if key in INTERNAL_FIELDS:
            continue
        result[key] = int(value) if key == 'idade' and value is not None else value
    return result


def _apply_update(recipient_id, updates):
    set_parts, expr_names, expr_values = [], {}, {}
    for i, (key, value) in enumerate(updates.items()):
        set_parts.append(f"#f{i} = :v{i}")
        expr_names[f"#f{i}"] = key
        expr_values[f":v{i}"] = value
    response = users_table.update_item(
        Key={'ID': recipient_id},
        UpdateExpression='SET ' + ', '.join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
        ReturnValues='ALL_NEW',
    )
    return response.get('Attributes')


def _list_all():
    """Lista todos os cadastros via Query no GSI por status (sem Scan).
    Query lê apenas os itens de cada status — custo proporcional ao resultado."""
    items = []
    for status in LISTABLE_STATUSES:
        kwargs = {'IndexName': STATUS_INDEX, 'KeyConditionExpression': Key('status').eq(status)}
        while True:
            response = users_table.query(**kwargs)
            items.extend(response.get('Items', []))
            if 'LastEvaluatedKey' not in response:
                break
            kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
    return items


# =====================================================================
#  Endpoints — público
# =====================================================================

def handle_cadastro(event):
    try:
        data = parse_body(event)
    except Exception:
        return error(400, "Corpo inválido (JSON esperado).")

    missing = [f for f in REQUIRED_FIELDS if not str(data.get(f, '')).strip()]
    if missing:
        return error(400, f"Campos obrigatórios ausentes: {', '.join(missing)}")

    try:
        idade = int(data['idade'])
    except (ValueError, TypeError):
        return error(400, "Campo 'idade' deve ser um número.")

    phone_id = canonical_phone(data['whatsapp'])
    if not phone_id:
        return error(400, "Número de WhatsApp inválido.")

    if users_table.get_item(Key={'ID': phone_id}).get('Item'):
        return error(409, "Já existe um cadastro com este número de WhatsApp.")

    timestamp = now_iso()
    item = {
        'ID': phone_id,
        'status': 'pending',
        'created_at': timestamp,
        'updated_at': timestamp,
        'nome_completo': str(data['nome_completo']).strip(),
        'idade': idade,
        'endereco': str(data['endereco']).strip(),
        'bairro': str(data['bairro']).strip(),
        'whatsapp': str(data['whatsapp']).strip(),
        'voluntario_ic': str(data['voluntario_ic']).strip(),
        'mora_sozinho': bool(data.get('mora_sozinho', False)),
        'contato_familia': str(data['contato_familia']).strip(),
        'contato_ccci': str(data['contato_ccci']).strip(),
        'conversation_state': 'initial',
        'state_last_updated': timestamp,
        'lista_contatos': extract_contact_numbers(data.get('contato_familia'), data.get('contato_ccci')),
    }
    for field in OPTIONAL_TEXT_FIELDS:
        value = data.get(field)
        item[field] = str(value).strip() if value not in (None, '') else None

    try:
        users_table.put_item(Item=item)
    except Exception as e:
        logger.error(f"Error creating recipient: {e}")
        return error(500, "Erro ao salvar o cadastro.")

    return respond(201, {'id': phone_id, 'status': 'pending'})


def handle_login(event):
    try:
        data = parse_body(event)
    except Exception:
        return error(400, "Corpo inválido (JSON esperado).")

    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', ''))
    stored = ADMINS.get(email)
    if not stored or not verify_password(password, stored):
        return error(401, "Credenciais inválidas.")

    exp = int(time.time()) + JWT_TTL_SECONDS
    return respond(200, {'token': jwt_encode({'sub': email, 'role': 'admin', 'exp': exp}), 'expires_at': exp})


# =====================================================================
#  Endpoints — admin (Bearer)
# =====================================================================

def handle_list_recipients(event):
    recipients = [item_to_response(i) for i in _list_all()]
    recipients.sort(key=lambda r: r.get('created_at') or '', reverse=True)
    return respond(200, {'recipients': recipients})


def handle_update_recipient(event, recipient_id):
    if not users_table.get_item(Key={'ID': recipient_id}).get('Item'):
        return error(404, "Cadastro não encontrado.")
    try:
        data = parse_body(event)
    except Exception:
        return error(400, "Corpo inválido (JSON esperado).")

    updates = {}
    for field in EDITABLE_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if field == 'idade':
            try:
                updates[field] = int(value)
            except (ValueError, TypeError):
                return error(400, "Campo 'idade' deve ser um número.")
        elif field == 'mora_sozinho':
            updates[field] = bool(value)
        else:
            updates[field] = str(value).strip() if value not in (None, '') else None

    if not updates:
        return error(400, "Nenhum campo editável informado.")
    updates['updated_at'] = now_iso()
    return respond(200, item_to_response(_apply_update(recipient_id, updates)))


def handle_patch_status(event, recipient_id):
    if not users_table.get_item(Key={'ID': recipient_id}).get('Item'):
        return error(404, "Cadastro não encontrado.")
    try:
        data = parse_body(event)
    except Exception:
        return error(400, "Corpo inválido (JSON esperado).")
    status = data.get('status')
    if status not in ('approved', 'rejected', 'pending'):
        return error(400, "Status inválido.")
    return respond(200, item_to_response(_apply_update(recipient_id, {'status': status, 'updated_at': now_iso()})))


def handle_delete_recipient(event, recipient_id):
    if not users_table.get_item(Key={'ID': recipient_id}).get('Item'):
        return error(404, "Cadastro não encontrado.")
    try:
        users_table.delete_item(Key={'ID': recipient_id})
    except Exception as e:
        logger.error(f"Error deleting recipient {recipient_id}: {e}")
        return error(500, "Erro ao remover o cadastro.")
    return respond(200, {'ok': True})


# =====================================================================
#  Roteamento
# =====================================================================

def _normalize_path(event):
    """Path sem o stage e sem o prefixo do CloudFront (/behavior-dev)."""
    path = event.get('path') or event.get('rawPath') or '/'
    if PATH_PREFIX and path.startswith(PATH_PREFIX):
        path = path[len(PATH_PREFIX):] or '/'
    return path.rstrip('/') or '/'


def lambda_handler(event, context):
    try:
        method = (event.get('httpMethod')
                  or event.get('requestContext', {}).get('http', {}).get('method'))
        path = _normalize_path(event)
        logger.info(f"Request: {method} {path}")

        if method == 'OPTIONS':
            return respond(204)

        # Rotas públicas
        if method == 'POST' and path == '/cadastro':
            return handle_cadastro(event)
        if method == 'POST' and path == '/auth/login':
            return handle_login(event)

        # Rotas admin (Bearer)
        recipient_match = re.match(r'^/recipients/([^/]+)$', path)
        needs_auth = (path == '/recipients') or bool(recipient_match)
        if needs_auth and not get_auth_payload(event):
            return error(401, "Não autorizado.")

        if method == 'GET' and path == '/recipients':
            return handle_list_recipients(event)
        if recipient_match:
            rid = recipient_match.group(1)
            if method == 'PUT':
                return handle_update_recipient(event, rid)
            if method == 'PATCH':
                return handle_patch_status(event, rid)
            if method == 'DELETE':
                return handle_delete_recipient(event, rid)

        return error(404, "Rota não encontrada.")
    except Exception as e:
        logger.error(f"Critical Error in app handler: {e}", exc_info=True)
        return error(500, "Erro interno do servidor.")
