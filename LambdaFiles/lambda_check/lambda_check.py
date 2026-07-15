import boto3
import os
import json
import logging
import urllib.request
from datetime import datetime, timedelta, timezone

# --- Logger Configuration ---
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Environment Variable Configurations ---
# Aceita as duas convenções de nome (com e sem TARGET) para casar com a infra.
DYNAMODB_USERS_TABLE_NAME = os.environ.get('AWS_DYNAMODB_TABLE_NAME_0') or os.environ.get('AWS_DYNAMODB_TABLE_TARGET_NAME_0')
DYNAMODB_REQUESTS_TABLE_NAME = os.environ.get('AWS_DYNAMODB_TABLE_NAME_1') or os.environ.get('AWS_DYNAMODB_TABLE_TARGET_NAME_1')
AWS_REGION = os.environ.get('REGION', 'us-east-1')
API_URL = os.environ.get('API_URL', 'https://gate.whapi.cloud')
PARAM_NAME_TOKEN = os.environ.get('AWS_SSM_PARAMETER_NAME_0') or os.getenv("AWS_SSM_PARAMETER_TARGET_NAME_0")

# --- Hardcoded Supervisor List ---
SUPERVISOR_NUMBERS = ['553186155781']

# --- Mensagens ---
# Enviada ~10-15 min após o aceite (não junto do atendimento). Mantida idêntica ao
# texto em chatbot_act.py (MESSAGES['feedback_prompt']).
FEEDBACK_PROMPT = (
    "A sua opinião é muito importante para nós! Estamos sempre em busca de evoluir a usabilidade e a experiência da Rede UniConecta CCCI.\n\n"
    "Se quiser contribuir com sugestões, críticas construtivas ou melhorias, envie um e-mail para: uniconecta@unicin.org\n\n"
    "A Rede de apoio UniConecta CCCI é construída por todos nós. \n"
    "Contamos com você para torná-la cada vez mais funcional, acolhedora e interassistencial!"
)

# Atraso mínimo antes de enviar o feedback após o aceite.
FEEDBACK_DELAY_MINUTES = 10

# --- Validações e Clientes Boto3 ---
if not all([DYNAMODB_USERS_TABLE_NAME, DYNAMODB_REQUESTS_TABLE_NAME, PARAM_NAME_TOKEN]):
    logger.error("CRITICAL ERROR: Essential environment variables not configured.")
    raise ValueError("Incomplete environment configuration.")

ssm = boto3.client("ssm", region_name=AWS_REGION)
dynamodb_resource = boto3.resource("dynamodb", region_name=AWS_REGION)
users_table = dynamodb_resource.Table(DYNAMODB_USERS_TABLE_NAME)
requests_table = dynamodb_resource.Table(DYNAMODB_REQUESTS_TABLE_NAME)

# --- Carregar Token do SSM Parameter Store ---
try:
    response = ssm.get_parameter(Name=PARAM_NAME_TOKEN, WithDecryption=True)
    TOKEN = response["Parameter"]["Value"]
except Exception as e:
    logger.error(f"CRITICAL ERROR loading token from SSM: {e}")
    raise e

# --- Funções de Comunicação (sem alterações) ---

def _send_whapi_request(endpoint, payload):
    headers = {'Authorization': f"Bearer {TOKEN}", 'Content-Type': 'application/json'}
    data_bytes = json.dumps(payload).encode('utf-8')
    url = f"{API_URL}/{endpoint}"
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req) as response:
            if 200 <= response.status < 300: return True
            logger.error(f"Error sending message. Status: {response.status}, Body: {response.read().decode()}")
            return False
    except Exception as e:
        logger.error(f"Unexpected error in _send_whapi_request: {e}")
        return False

def send_whapi_message(to, body):
    return _send_whapi_request('messages/text', {'to': to, 'body': body})

def send_main_menu_with_buttons(to):
    menu_body = (
        "Olá! Parece que sua sessão anterior expirou. Vamos começar de novo.\n\n"
        "Selecione uma das opções abaixo 👇:"
    )
    payload = {"to": to, "type": "button", "header": {"text": "UniConecta CCC!"}, "body": {"text": menu_body}, "action": {"buttons": [{"type": "quick_reply", "id": "menu_emergency", "title": "🚨 Ajuda Imediata"}, {"type": "quick_reply", "id": "menu_ride", "title": "🚗 Carona Solidária"}, {"type": "quick_reply", "id": "menu_chat", "title": "💬 Bate-papo"}]}}
    return _send_whapi_request('messages/interactive', payload)

# --- FUNÇÃO send_nps_survey REMOVIDA ---


# --- Lógica Principal do Monitor ---

def handle_pending_requests(now, start_time_iso):
    """(1) Notificar supervisores sobre solicitações pendentes há mais de 1 hora (criadas nas últimas 2h)."""
    logger.info("Checking for overdue pending requests...")
    one_hour_ago = now - timedelta(hours=1)
    
    response = requests_table.scan(
        FilterExpression='attribute_not_exists(escalated) AND #st = :status AND creation_timestamp > :start_time',
        ExpressionAttributeNames={'#st': 'status'},
        ExpressionAttributeValues={
            ':status': 'pendente',
            ':start_time': start_time_iso
        }
    )
    
    for item in response.get('Items', []):
        timestamp_str = item.get('creation_timestamp')
        if not timestamp_str:
            logger.warning(f"Skipping request {item.get('ID', 'N/A')} because it is missing 'creation_timestamp'.")
            continue

        creation_timestamp = datetime.fromisoformat(timestamp_str).replace(tzinfo=timezone.utc)
        if creation_timestamp < one_hour_ago:
            logger.warning(f"Request {item['ID']} is overdue. Escalating to supervisors.")
            requester_name = item.get('requester_name', 'N/A')
            message = f"ALERTA SUPERVISOR: A solicitação de ajuda de *{requester_name}* (ID: {item['ID']}) está pendente há mais de 1 hora e não foi atendida."
            
            for supervisor_num in SUPERVISOR_NUMBERS:
                send_whapi_message(f"{supervisor_num}@s.whatsapp.net", message)
            
            requests_table.update_item(Key={'ID': item['ID']}, UpdateExpression='SET escalated = :val', ExpressionAttributeValues={':val': True})

# --- FUNÇÃO handle_accepted_requests REMOVIDA ---

def handle_stale_conversations(now, start_time_iso):
    """(3) Reiniciar o fluxo para usuários parados por mais de 1 hora (com estado atualizado nas últimas 2h)."""
    logger.info("Checking for stale conversations...")
    one_hour_ago = now - timedelta(hours=1)
    
    response = users_table.scan(
        FilterExpression='conversation_state <> :initial AND state_last_updated > :start_time',
        ExpressionAttributeValues={
            ':initial': 'initial',
            ':start_time': start_time_iso
        }
    )

    for item in response.get('Items', []):
        timestamp_str = item.get('state_last_updated')
        if not timestamp_str:
            logger.warning(f"Skipping user {item.get('ID', 'N/A')} because 'state_last_updated' is missing unexpectedly.")
            continue
            
        last_updated = datetime.fromisoformat(timestamp_str).replace(tzinfo=timezone.utc)
        if last_updated < one_hour_ago:
            user_id = item['ID']
            user_id_wa = f"{user_id}@s.whatsapp.net"
            logger.warning(f"User {user_id} has a stale conversation state ({item['conversation_state']}). Resetting.")
            
            if send_main_menu_with_buttons(user_id_wa):
                users_table.update_item(Key={'ID': user_id}, UpdateExpression='SET conversation_state = :state', ExpressionAttributeValues={':state': 'initial'})


def handle_pending_feedback(now):
    """(4) Enviar a mensagem de feedback ~10-15 min após o aceite (não junto do
    atendimento). O bot marca feedback_pending=True e acceptance_timestamp ao aceitar."""
    logger.info("Checking for pending feedback messages...")
    cutoff = now - timedelta(minutes=FEEDBACK_DELAY_MINUTES)

    response = requests_table.scan(
        FilterExpression='#st = :status AND feedback_pending = :true',
        ExpressionAttributeNames={'#st': 'status'},
        ExpressionAttributeValues={':status': 'aceito', ':true': True}
    )

    for item in response.get('Items', []):
        timestamp_str = item.get('acceptance_timestamp')
        if not timestamp_str:
            logger.warning(f"Skipping request {item.get('ID', 'N/A')} for feedback: missing 'acceptance_timestamp'.")
            continue

        accepted_at = datetime.fromisoformat(timestamp_str).replace(tzinfo=timezone.utc)
        if accepted_at > cutoff:
            continue  # aceite recente demais; envia numa próxima execução do monitor

        target = item.get('requester_wa') or item.get('requester_id')
        if not target:
            logger.warning(f"Skipping request {item['ID']} for feedback: no requester target.")
            continue

        logger.info(f"Sending delayed feedback for request {item['ID']} to {target}.")
        if send_whapi_message(f"{target}@s.whatsapp.net", FEEDBACK_PROMPT):
            requests_table.update_item(
                Key={'ID': item['ID']},
                UpdateExpression='SET feedback_pending = :false',
                ExpressionAttributeValues={':false': False}
            )


# --- Handler Principal da Lambda ---

def lambda_handler(event, context):
    logger.info(f"Monitor function triggered at: {datetime.utcnow()}")
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    
    # Calcular o ponto de início para o filtro (2 horas atrás)
    two_hours_ago = now - timedelta(hours=2)
    two_hours_ago_iso = two_hours_ago.isoformat()
    
    try:
        # Passar o novo tempo de início para as funções
        handle_pending_requests(now, two_hours_ago_iso)
        
        # --- CHAMADA para handle_accepted_requests REMOVIDA ---
        
        handle_stale_conversations(now, two_hours_ago_iso)

        handle_pending_feedback(now)

        logger.info("Monitor run completed successfully.")
        return {'statusCode': 200, 'body': json.dumps('Monitor run completed.')}
        
    except Exception as e:
        logger.error(f"Critical Error during monitor execution: {e}", exc_info=True)
        return {'statusCode': 500, 'body': json.dumps('Internal Server Error')}