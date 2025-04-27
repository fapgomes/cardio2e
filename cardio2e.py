#!/usr/bin/env python3

import serial
import logging
import threading
import paho.mqtt.client as mqtt
import json
import time
import configparser
import ast
import signal
import re
import datetime

from cardio2e_modules import cardio2e_zones,cardio2e_errors,cardio2e_covers,cardio2e_hvac

config = configparser.ConfigParser()
config.read('cardio2e.conf')

########
## EXTRA VARS
########
# Configurações gerais
DEBUG = int(config['global']['debug'])
if DEBUG:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

_LOGGER = logging.getLogger(__name__)

HA_DISCOVER_TOPIC = config['global'].get('ha_discover_prefix', 'homeassistant')
DEFAULT_SERIAL_PORT = config['cardio2e'].get('serial_port', '/dev/ttyUSB0')
DEFAULT_BAUDRATE = int(config['cardio2e'].get('baudrate', 9600))
MQTT_BROKER = config['mqtt']['address']  # Endereço do broker MQTT
MQTT_PORT = int(config['mqtt']['port'])
MQTT_USERNAME = config['mqtt']['username']
MQTT_PASSWORD = config['mqtt']['password']

CARDIO2E_TERMINATOR="\r"
CARDIO2E_UPDATE_DATE_INTERVAL = int(config['cardio2e'].get('update_date_interval', 3600))
CARDIO2E_PASSWORD = config['cardio2e']['password']

########
## LIGHTS
########
CARDIO2E_FETCH_NAMES_LIGHTS = config['cardio2e'].get('fetch_light_names', 'true').lower() == 'true'
# Processes the value of dimmer_lights from the configuration file
dimmer_lights_raw = config['cardio2e'].get('dimmer_lights', '[]')  # Use '[]' como padrão se não estiver no config
try:
    CARDIO2E_DIMMER_LIGHTS = ast.literal_eval(dimmer_lights_raw)
    if not isinstance(CARDIO2E_DIMMER_LIGHTS, list):
        raise ValueError("dimmer_lights must be a list.")
    CARDIO2E_DIMMER_LIGHTS = [int(light_id) for light_id in CARDIO2E_DIMMER_LIGHTS]  # Converte cada item para int
except (ValueError, SyntaxError) as e:
    _LOGGER.error("Error interpreting dimmer_lights in config file: %s", e)
    CARDIO2E_DIMMER_LIGHTS = []
# Processes the value of force_include_lights from the configuration file
force_include_lights_raw = config['cardio2e'].get('force_include_lights', '[]')  # Use '[]' como padrão se não estiver no config
try:
    CARDIO2E_FORCE_INCLUDE_LIGHTS = ast.literal_eval(force_include_lights_raw)
    if not isinstance(CARDIO2E_FORCE_INCLUDE_LIGHTS, list):
        raise ValueError("force_include_lights must be a list.")
    CARDIO2E_FORCE_INCLUDE_LIGHTS = [int(light_id) for light_id in CARDIO2E_FORCE_INCLUDE_LIGHTS]  # Converte cada item para int
except (ValueError, SyntaxError) as e:
    _LOGGER.error("Error interpreting force_include_lights in config file: %s", e)
    CARDIO2E_FORCE_INCLUDE_LIGHTS = []

########
## SWITCHES
########
CARDIO2E_FETCH_NAMES_SWITCHES = config['cardio2e'].get('fetch_switch_names', 'true').lower() == 'true'

########
## COVERS
########
CARDIO2E_FETCH_NAMES_COVERS = config['cardio2e'].get('fetch_cover_names', 'true').lower() == 'true'
CARDIO2E_SKIP_INIT_COVER_STATE = config['cardio2e'].get('skip_init_cover_state', 'false').lower() == 'true'
CARDIO2E_NCOVERS = int(config['cardio2e'].get('ncovers', 20))

########
## HVAC
########
CARDIO2E_FETCH_NAMES_HVAC = config['cardio2e'].get('fetch_names_hvac', 'true').lower() == 'true'
hvac_states = {}
_LOGGER.info("HVAC_STATES: %s", hvac_states)

########
## ZONES
########
CARDIO2E_FETCH_NAMES_ZONES = config['cardio2e'].get('fetch_zone_names', 'true').lower() == 'true'
# Processa o valor de zones_normal_as_off a partir do arquivo de configuração
zones_normal_as_off_raw = config['cardio2e'].get('zones_normal_as_off', '[]')  # Use '[]' como padrão se não estiver no config
try:
    CARDIO2E_ZONES_NORMAL_AS_OFF = ast.literal_eval(zones_normal_as_off_raw)
    if not isinstance(CARDIO2E_ZONES_NORMAL_AS_OFF, list):
        raise ValueError("zones_normal_as_off no arquivo de configuração deve ser uma lista.")
    CARDIO2E_ZONES_NORMAL_AS_OFF = [int(zone_id) for zone_id in CARDIO2E_ZONES_NORMAL_AS_OFF]  # Converte cada item para int
except (ValueError, SyntaxError) as e:
    _LOGGER.error("Erro ao interpretar zones_normal_as_off no arquivo de configuração: %s", e)
    CARDIO2E_ZONES_NORMAL_AS_OFF = []
bypass_states = ""

########
## SECURITY
########
CARDIO2E_ALARM_CODE = int(config['cardio2e'].get('code', 12345))

def create_shutdown_handler(serial_conn, mqtt_client):
    def handle_shutdown(signum, frame):
        """
        Manipulador de sinais para realizar o logout antes de encerrar.
        """
        _LOGGER.info("Closing signal received. Logging out...")
        cardio_login(serial_conn, mqtt_client, state="logout")
        serial_conn.close()
        _LOGGER.info("Logout completed. Closing the program.")
        exit(0)
    return handle_shutdown

def main():
    try:
        # Configuração da conexão serial
        serial_conn = serial.Serial(
            port=DEFAULT_SERIAL_PORT, 
            baudrate=DEFAULT_BAUDRATE, 
            write_timeout=1,
            timeout=1)
        _LOGGER.info("Connection to Cardio2e established on port %s", DEFAULT_SERIAL_PORT)

        # Configuração do cliente MQTT
        mqtt_client = mqtt.Client()
        mqtt_client.on_connect = on_mqtt_connect
        mqtt_client.on_message = on_mqtt_message
        mqtt_client.user_data_set({"serial_conn": serial_conn})
        mqtt_client.username_pw_set(MQTT_USERNAME, password=MQTT_PASSWORD)
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start()

        # Registra os sinais de encerramento
        handle_shutdown = create_shutdown_handler(serial_conn, mqtt_client)
        signal.signal(signal.SIGTERM, handle_shutdown)  # Sinal enviado pelo systemd ao parar o serviço
        signal.signal(signal.SIGINT, handle_shutdown)   # Sinal de interrupção (ex.: Ctrl+C)

        # init the errors topic
        cardio2e_errors.initialize_error_payload(mqtt_client)

        cardio_login(serial_conn, mqtt_client, state="login", password=CARDIO2E_PASSWORD)

        _LOGGER.info("\n################\nCardio2e ready. Listening for events.\n################")
        # Inicia a thread de escuta na porta serial
        listener_thread = threading.Thread(target=listen_for_updates, args=(serial_conn, mqtt_client), daemon=True)
        listener_thread.start()

        # Mantém o programa principal ativo
        while True:
            time.sleep(0.1)

    except Exception as e:
        _LOGGER.error("Falha ao configurar Cardio2e: %s", e)

def on_mqtt_connect(client, userdata, flags, rc):
    """Callback para quando o cliente MQTT se conecta."""
    _LOGGER.info("Connected to broker MQTT with code %s", rc)

    # topics to subscribe
    client.subscribe("cardio2e/light/set/#")
    client.subscribe("cardio2e/switch/set/#")
    client.subscribe("cardio2e/cover/set/#")
    client.subscribe("cardio2e/cover/command/#")
    client.subscribe("cardio2e/hvac/+/set/#")  # Subscreve dinamicamente a qualquer hvac_id
    client.subscribe("cardio2e/alarm/set/#")
    client.subscribe("cardio2e/zone/bypass/set/#")
    client.subscribe("cardio2e/scenario/set/#")

    _LOGGER.info("Subscribed to all necessary topics.")

def on_mqtt_message(client, userdata, msg):
    """Callback para quando uma mensagem é recebida em um tópico assinado."""
    global hvac_states
    global bypass_states

    topic = msg.topic
    payload = msg.payload.decode().upper()
    _LOGGER.debug("Mensagem recebida no tópico %s: %s", topic, payload)

    # verify if a light message appears
    if topic.startswith("cardio2e/light/set/"):
        try:
            light_id = int(topic.split("/")[-1])
        except ValueError:
            _LOGGER.error("Invalid light ID on topic: %s", topic)
            return

        # Converte o payload para o comando apropriado para RS-232
        if payload == "ON":
            command = 100 
        elif payload == "OFF":
            command = 0
        else:
            # Tenta converter o payload diretamente para um valor numérico
            try:
                command = int(payload)
                if command < 0 or command > 100:
                    raise ValueError("O valor do comando deve estar entre 0 e 100")
            except ValueError:
                _LOGGER.error("Payload inválido para o comando de luz: %s", payload)
                return

        # Envia comando convertido para o RS-232
        send_rs232_command(userdata["serial_conn"], "L", light_id, command)

    # Verify if a switch message appears
    elif topic.startswith("cardio2e/switch/set/"):
        try:
            switch_id = int(topic.split("/")[-1])
        except ValueError:
            _LOGGER.error("Switch ID invalid on topic: %s", topic)
            return

        # Converte o payload para o comando apropriado para RS-232
        if payload == "ON":
            command = "O"  # Valor padrão para ligar (pode ser ajustado se necessário)
        elif payload == "OFF":
            command = "C"
        else:
            _LOGGER.error("Invalid Payload for switch command: %s", payload)
            return

        # Envia comando convertido para o RS-232
        send_rs232_command(userdata["serial_conn"], "R", switch_id, command)

    # Check if the message is for covers
    elif topic.startswith("cardio2e/cover/set/"):
        try:
            cover_id = int(topic.split("/")[-1])
        except ValueError:
            _LOGGER.error("Topic invalid Cover ID: %s", topic)
            return

        # Tenta converter o payload diretamente para uma posição numérica (0-100)
        try:
            position = int(payload)
            if position < 0 or position > 100:
                raise ValueError("The position must be between 0 and 100")
        except ValueError:
            _LOGGER.error("Invalid payload for shutter position command: %s", payload)
            return

        # Envia comando para definir a posição do estore no RS-232
        send_rs232_command(userdata["serial_conn"], "C", cover_id, position)

    elif topic.startswith("cardio2e/cover/command/"):
        try:
            cover_id = int(topic.split("/")[-1])
        except ValueError:
            _LOGGER.error("Topic invalid Cover ID: %s", topic)
            return

        # Verifica se o payload é um comando válido
        command = payload.upper()
        if command == "OPEN":
            position = 100
        elif command == "CLOSE":
            position = 0
        elif command == "STOP":
            # envio ficticio apenas para parar o cover
            send_rs232_command(userdata["serial_conn"], "C", cover_id, 50)
            time.sleep(1)
            # Obtém a posição atual do estore antes de enviar o comando STOP
            position = get_entity_state(userdata["serial_conn"], client, cover_id, "C")
        else:
            _LOGGER.error("Comando inválido recebido: %s", command)
            return

        # Envia o comando correspondente
        send_rs232_command(userdata["serial_conn"], "C", cover_id, position)

    # Check if the message is for HVAC
    elif topic.startswith("cardio2e/hvac/") and "/set/" in topic:
        try:
            # Extrai o ID do HVAC e o tipo de configuração do tópico
            parts = topic.split("/")
            hvac_id = int(parts[2])  # Obtém o ID do HVAC
            setting_type = parts[-1]  # Obtém o tipo de configuração (heating_setpoint, cooling_setpoint, etc.)

            # Verificar se todas as chaves obrigatórias estão no estado
            required_keys = ["heating_setpoint", "cooling_setpoint", "fan_state", "mode"]
            for key in required_keys:
                if key not in hvac_states[hvac_id]:
                    hvac_states[hvac_id][key] = 0 if "setpoint" in key else "off"  # Valores padrão

            # Atualiza o valor do parâmetro específico com base no tópico
            if setting_type == "heating_setpoint":
                hvac_states[hvac_id]["heating_setpoint"] = float(payload)
            elif setting_type == "cooling_setpoint":
                hvac_states[hvac_id]["cooling_setpoint"] = float(payload)
            elif setting_type == "fan":
                hvac_states[hvac_id]["fan_state"] = payload.lower()
            elif setting_type == "mode":
                hvac_states[hvac_id]["mode"] = payload.lower()
            else:
                _LOGGER.error("Unknown setting type for HVAC: %s", setting_type)
                return

            # Extrai os estados atuais para enviar o comando completo para o HVAC
            heating_setpoint = float(hvac_states[hvac_id]["cooling_setpoint"]) - 2
            cooling_setpoint = float(hvac_states[hvac_id]["cooling_setpoint"])
            fan_state = hvac_states[hvac_id]["fan_state"]
            mode = hvac_states[hvac_id]["mode"]

            # Envia o comando RS-232 com todos os parâmetros do HVAC
            send_rs232_command(
                serial_conn=userdata["serial_conn"],
                entity_type="H",
                entity_id=hvac_id,
                heating_setpoint=heating_setpoint,
                cooling_setpoint=cooling_setpoint,
                fan_state=fan_state,
                mode=mode
            )

            hvac_states = cardio2e_hvac.update_hvac_state(client, hvac_states, int(hvac_id), "heating_setpoint", heating_setpoint)
            hvac_states = cardio2e_hvac.update_hvac_state(client, hvac_states, int(hvac_id), "cooling_setpoint", cooling_setpoint)
            hvac_states = cardio2e_hvac.update_hvac_state(client, hvac_states, int(hvac_id), "fan_state", fan_state)
            hvac_states = cardio2e_hvac.update_hvac_state(client, hvac_states, int(hvac_id), "mode", mode)

            _LOGGER.info("Updated HVAC %d topics with new settings: Heating %.1f, Cooling %.1f, Fan %s, Mode %s",
                          hvac_id, heating_setpoint, cooling_setpoint, fan_state, mode)

        except ValueError:
            _LOGGER.error("Invalid topic or payload for HVAC command: %s", topic)
        except Exception as e:
            _LOGGER.error("Error processing HVAC message: %s", e)

    # Verify if a security message appears
    elif topic.startswith("cardio2e/alarm/set/"):
        try:
            security_id = int(topic.split("/")[-1])
        except ValueError:
            _LOGGER.error("Security ID invalid on topic: %s", topic)
            return

        # Converte o payload para o comando apropriado para RS-232
        if payload == "ARMED_AWAY":
            command = f"A {CARDIO2E_ALARM_CODE}" 
        elif payload == "DISARMED":
            command = f"D {CARDIO2E_ALARM_CODE}"
        else:
            _LOGGER.error("Invalid Payload for security command: %s", payload)
            return

        # Envia comando convertido para o RS-232
        send_rs232_command(userdata["serial_conn"], "S", security_id, command)

    # Checks if the message is for bypass control of a zone
    elif topic.startswith("cardio2e/zone/bypass/set/"):
        # Obter estado atual das zonas em formato string (ex: "NNYYNNNNNNNNNNNN")
        _LOGGER.debug("Entering bypass processing...")
        _LOGGER.info("Current Zones: %s", bypass_states)

        if not bypass_states or len(bypass_states) != 16:
            _LOGGER.error("Failed to get current state of zones. Using default state.")
            bypass_states = "N" * 16  # Caso falhe, assume tudo como ativo

        # Converte o estado atual em uma lista mutável
        zone_bypass_states = list(bypass_states)
        _LOGGER.debug("BEFORE: Zones state: %s - Bypass var: %s", zone_bypass_states, bypass_states)
        try:
            zone_id = int(topic.split("/")[-1])
        except ValueError:
            _LOGGER.error("ID da zona inválido no tópico: %s", topic)
            return

        # Atualiza o estado de bypass da zona na lista global
        if payload == "ON":
            zone_bypass_states[zone_id - 1] = "Y"  # Coloca a zona em bypass
            _LOGGER.info("Zone %d deactivated", zone_id)
        elif payload == "OFF":
            zone_bypass_states[zone_id - 1] = "N"  # Remove o bypass da zona
            _LOGGER.info("Zone %d activated", zone_id)
        else:
            _LOGGER.error("Payload inválido para controle de bypass da zona: %s", payload)
            return

        _LOGGER.debug("AFTER: Zones state: %s - Bypass var: %s", zone_bypass_states, bypass_states)
        try:
            # Envia o comando e verifica se foi bem-sucedido
            success = send_rs232_command(userdata["serial_conn"], "B", 1, "".join(zone_bypass_states))

            if success:
                # Atualiza a string apenas se o comando for bem recebido
                bypass_states = "".join(zone_bypass_states)
                _LOGGER.info("Bypass states updated successfully: %s", bypass_states)
            else:
                _LOGGER.warning("Bypass command failed, state not updated.")

        except Exception as e:
            _LOGGER.error("Error sending bypass command: %s", e)

def send_rs232_command(serial_conn, entity_type, entity_id, state=None, heating_setpoint=None, cooling_setpoint=None, fan_state=None, mode=None):
    """
    Envia comando para o RS-232 para alterar o estado de luz, bypass de zona ou configurações completas do HVAC.
    :param serial_conn: Conexão RS-232.
    :param entity_type: Tipo da entidade ("L" para luz, "Z" para zona, "H" para HVAC).
    :param entity_id: Identificador da entidade.
    :param state: Estado para luz ou bypass de zona.
    :param heating_setpoint: Setpoint de aquecimento para o HVAC (obrigatório para HVAC).
    :param cooling_setpoint: Setpoint de resfriamento para o HVAC (obrigatório para HVAC).
    :param fan_state: Estado do ventilador para o HVAC ("on" ou "off").
    :param mode: Modo de operação do HVAC ("auto", "heat", "cool", "off", "economy", "normal").
    """
    if entity_type == "H":
        # Verificação para garantir que todos os parâmetros necessários foram fornecidos para HVAC
        if heating_setpoint is None or cooling_setpoint is None or fan_state is None or mode is None:
            _LOGGER.error("Missing parameters for HVAC command: heating_setpoint, cooling_setpoint, fan_state, and mode are required.")
            return False

        # Mapeamento do estado do ventilador e do modo para códigos RS-232
        fan_state_code = "R" if fan_state == "on" else "S"  # Exemplo: "R" para ligado e "S" para desligado
        mode_mapping = {
            "auto": "A",
            "heat": "H",
            "cool": "C",
            "off": "O",
            "economy": "E",
            "normal": "N"
        }
        mode_code = mode_mapping.get(mode, "O")  # "O" como padrão se o modo não for reconhecido

        # Comando completo para o HVAC com todos os parâmetros
        command = f"@S H {entity_id} {heating_setpoint} {cooling_setpoint} {fan_state_code} {mode_code}{CARDIO2E_TERMINATOR}"
    else:
        if state is None:
            command = f"@S {entity_type} {entity_id}{CARDIO2E_TERMINATOR}"
        else:
            command = f"@S {entity_type} {entity_id} {state}{CARDIO2E_TERMINATOR}"

    try:
        _LOGGER.info("Sending command to RS-232: %s", command)
        serial_conn.write(command.encode())
        serial_conn.flush()
        return True
    except Exception as e:
        _LOGGER.error("Error sending command to RS-232: %s", e)
        return False

# listen for cardio2e updates
def listen_for_updates(serial_conn, mqtt_client):
    """Escuta as atualizações na porta RS-232 e publica o estado e o brilho no MQTT."""
    last_time_sent = None  # Variável para armazenar o último horário de envio

    while True:
        time.sleep(0.1)
        if not serial_conn.is_open:
            _LOGGER.debug("The serial connection was closed.")
            break
        try:
            # send the date every CARDIO2E_UPDATE_DATE_INTERVAL interval
            current_time = datetime.datetime.now()
            if last_time_sent is None or (current_time - last_time_sent).seconds >= CARDIO2E_UPDATE_DATE_INTERVAL:
                time_command = current_time.strftime("%Y%m%d%H%M%S")

                # scan every cover state because cardio have some problems leading with covers
                #for entity_id in range(1, CARDIO2E_NCOVERS + 1):  
                #    get_entity_state(serial_conn, mqtt_client, entity_id, "C")

                send_rs232_command(serial_conn, "D", time_command)
                # clean errors after some time
                cardio2e_errors.report_error_state(mqtt_client, "No errors.")
                _LOGGER.info("Sent time command to cardio2e: %s", time_command)
                last_time_sent = current_time

            # Verifica se há dados para ler antes de chamar readline()
            if serial_conn.in_waiting > 0:
                # Ler a linha recebida do RS-232
                received_message = serial_conn.readline().decode().strip()
                if received_message:
                    _LOGGER.info("RS-232 message received: %s", received_message)

                    # Dividir a linha em mensagens separadas (caso múltiplas mensagens estejam na mesma linha)
                    #messages = received_message.split('@')
                    # Dividir a linha em mensagens separadas com '@' e '\r' (#015) como delimitadores
                    # Substitui #015 por \r na mensagem recebida
                    received_message = received_message.replace('#015', '\r')
                    messages = []
                    for part in received_message.split('@'):
                        sub_parts = part.split('\r')
                        messages.extend(sub_parts)

                    # Processa cada mensagem individualmente
                    for msg in messages:
                        if not msg:  # Ignora strings vazias
                            continue

                        # Adiciona o caractere '@' de volta ao início da mensagem
                        msg = '@' + msg.strip()
                        _LOGGER.info("Processing individual message: %s", msg)

                        # Dividir a mensagem em partes para identificação
                        message_parts = msg.split()

                        # Caso o comando seja enviado pelo Home Assistant
                        if len(message_parts) == 2 and message_parts[0] == "@A":
                            if message_parts[1] == "D":
                                _LOGGER.info("Cardio date update sucessully.")
                        elif len(message_parts) == 3 and message_parts[0] == "@A":
                            if message_parts[1] == "L":
                                # Comando para controle de luz "@A L <light_id>"
                                light_id = int(message_parts[2])
                                # Consultar o estado atual e publicar no MQTT
                                #get_entity_state(serial_conn, mqtt_client, light_id, "L")
                                _LOGGER.info("OK for action light: %s", light_id)
                            elif message_parts[1] == "R":
                                # Comando para controle de luz "@A R <switch_id>"
                                switch_id = int(message_parts[2])
                                # Consultar o estado atual e publicar no MQTT
                                #get_entity_state(serial_conn, mqtt_client, switch_id, "R")
                                _LOGGER.info("OK for action switch: %s", switch_id)
                            elif message_parts[1] == "C":
                                # Comando para controle da cover "@A C <cover_id>"
                                cover_id = int(message_parts[2])
                                # Consultar o estado atual e publicar no MQTT
                                #get_entity_state(serial_conn, mqtt_client, cover_id, "C")
                                _LOGGER.info("OK for action cover: %s", cover_id)
                            elif message_parts[1] == "S":
                                # security command "@A S <security_id>"
                                security_id = int(message_parts[2])
                                # Consultar o estado atual e publicar no MQTT
                                #get_entity_state(serial_conn, mqtt_client, security_id, "S")
                                _LOGGER.info("OK for action security: %s", security_id)
                            elif message_parts[1] == "B" and int(message_parts[2]) == 1:
                                # Comando para controle de bypass "@A B 1"
                                get_entity_state(serial_conn, mqtt_client, 1, "B")
                                _LOGGER.info("Bypass zones re-publish.")
                        elif len(message_parts) >= 3 and message_parts[0] == "@N":
                            error_msg = f"@N {message_parts[1]} {message_parts[2]} {message_parts[3]}"
                            if (message_parts[3] == "1"):
                                error_msg = f"Object type specified by the transaction is not recognized: {error_msg}"
                            elif (message_parts[3] == "2"):
                                error_msg = f"Object number is out of range for the object type specified: {error_msg}"
                            elif (message_parts[3] == "3"):
                                error_msg = f"One or more parameters are not valid: {error_msg}"
                            elif (message_parts[3] == "4"):
                                error_msg = f"Security code is not valid: {error_msg}"
                            elif (message_parts[3] == "5"):
                                error_msg = f"Transaction S (Set) not supported for the requested type of object: {error_msg}"
                            elif (message_parts[3] == "6"):
                                error_msg = f"Transaction G (Get) not supported for the requested type of object: {error_msg}"
                            elif (message_parts[3] == "7"):
                                error_msg = f"Transaction is refused because security is armed: {error_msg}"
                            elif (message_parts[3] == "8"):
                                error_msg = f"This zone can be ignored: {error_msg}"
                            elif (message_parts[3] == "16"):
                                error_msg = f"Security can not be armed because there are open zones: {error_msg}"
                            elif (message_parts[3] == "17"):
                                error_msg = f"Security can not be armed because there is a power problem: {error_msg}"
                            elif (message_parts[3] == "18"):
                                error_msg = f"Security can not be armed for an unknown reason: {error_msg}"
                            else:
                                error_msg = f"Unkown error message ({message_parts[3]}): {error_msg}"
                            cardio2e_errors.report_error_state(mqtt_client, error_msg)
                            _LOGGER.info("\n#######\nNACK from cardio with transaction %s: %s", msg, error_msg)
                            
                        elif len(message_parts) >= 4 and message_parts[0] == "@I":
                            # Caso o estado da luz tenha sido alterado manualmente ou externamente
                            if message_parts[1] == "L":
                                # Estado atualizado "@I L <light_id> <state>"
                                light_id = int(message_parts[2])
                                state = int(message_parts[3])

                                # Define o estado como "ON" se o brilho for maior que 0, caso contrário "OFF"
                                light_state = "ON" if state > 0 else "OFF"

                                # Publica o estado ON/OFF no tópico de estado
                                state_topic = f"cardio2e/light/state/{light_id}"
                                mqtt_client.publish(state_topic, light_state, retain=False)
                                _LOGGER.info("Light %d state updated to: %s", light_id, light_state)

                                # Para luzes dimmer, publica o valor exato de brilho no tópico de brilho
                                if light_id in CARDIO2E_DIMMER_LIGHTS:
                                    brightness_topic = f"cardio2e/light/brightness/{light_id}"
                                    mqtt_client.publish(brightness_topic, state, retain=False)
                                    _LOGGER.info("Light %d brightness updated to: %d", light_id, state)
                            elif message_parts[1] == "R":
                                # Estado atualizado "@I R <relay_id> <state>"
                                switch_id = int(message_parts[2])
                                state = message_parts[3]

                                # Define o estado como "ON" se o brilho for maior que 0, caso contrário "OFF"
                                switch_state = "ON" if state == "O" else "OFF"

                                # Publica o estado ON/OFF no tópico de estado
                                state_topic = f"cardio2e/switch/state/{switch_id}"
                                mqtt_client.publish(state_topic, switch_state, retain=False)
                                _LOGGER.info("Switch %d state, updated to: %s", switch_id, switch_state)
                            elif message_parts[1] == "C":
                                # Estado atualizado "@I C <cover_id> <state>"
                                cover_id = int(message_parts[2])
                                cover_state = message_parts[3]

                                # Publica o estado ON/OFF no tópico de estado
                                state_topic = f"cardio2e/cover/state/{cover_id}"
                                mqtt_client.publish(state_topic, cover_state, retain=False)
                                _LOGGER.info("Cover %d state, updated to: %s", cover_id, cover_state)
                            elif message_parts[1] == "H":
                                # Estado atualizado "@I H <hvac_id> <heating_setpoint> <cooling_setpoint> <fan_state> <mode>"
                                hvac_id = int(message_parts[2])

                                # Extrai os valores do payload
                                heating_setpoint = message_parts[3]
                                cooling_setpoint = message_parts[4]
                                fan_state = "on" if message_parts[5] == "R" else "off"
                                mode_code = message_parts[6]

                                # Define os tópicos para cada propriedade do HVAC
                                base_topic = f"cardio2e/hvac/{hvac_id}/state"

                                # Publica o setpoint de aquecimento
                                mqtt_client.publish(f"{base_topic}/heating_setpoint", heating_setpoint, retain=True)
                                _LOGGER.info("HVAC %d heating setpoint updated to: %s", hvac_id, heating_setpoint)

                                # Publica o setpoint de resfriamento
                                mqtt_client.publish(f"{base_topic}/cooling_setpoint", cooling_setpoint, retain=True)
                                _LOGGER.info("HVAC %d cooling setpoint updated to: %s", hvac_id, cooling_setpoint)

                                # Publica o estado do ventilador
                                mqtt_client.publish(f"{base_topic}/fan", fan_state, retain=True)
                                _LOGGER.info("HVAC %d fan state updated to: %s", hvac_id, fan_state)

                                # Mapeamento do modo
                                mode_mapping = {
                                    "A": "auto",
                                    "H": "heat",
                                    "C": "cool",
                                    "O": "off",
                                    "E": "economy",
                                    "N": "normal"
                                }
                                mode_state = mode_mapping.get(mode_code, "unknown")

                                # Publica o modo de operação
                                mqtt_client.publish(f"{base_topic}/mode", mode_state, retain=True)
                                _LOGGER.info("HVAC %d mode updated to: %s", hvac_id, mode_state)
                            # Caso o estado do alarme seja atualizado
                            elif message_parts[1] == "S":
                                # Estado atualizado "@I S <security_id> <state>"
                                security_id = int(message_parts[2])
                                security_state = message_parts[3]

                                if security_state == "A":
                                    security_state_value = "armed_away" 
                                elif security_state == "D":
                                    security_state_value = "disarmed" 
                                else:
                                    security_state_value = "unkown"

                                state_topic = f"cardio2e/alarm/state/{security_id}"
                                mqtt_client.publish(state_topic, security_state_value, retain=False)
                                _LOGGER.info("Security %d state, updated to: %s - %s", security_id, security_state, security_state_value)
                            elif message_parts[1] == "Z":
                                # Mensagem de estado das zonas, por exemplo: "@I Z 1 CCCCCCCCCCOOOOCC"
                                zone_states = message_parts[3]

                                # Processa cada caractere de estado para cada zona
                                for zone_id in range(1, len(zone_states) + 1):
                                    zone_state_char = zone_states[zone_id - 1]  # Caractere correspondente à zona
                                    zone_state = cardio2e_zones.interpret_zone_character(zone_state_char, zone_id, CARDIO2E_ZONES_NORMAL_AS_OFF)

                                    # Publica o estado da zona no MQTT
                                    state_topic = f"cardio2e/zone/state/{zone_id}"
                                    mqtt_client.publish(state_topic, zone_state, retain=False)
                                    if zone_state == "ON":
                                        _LOGGER.info("Status of zone %d published to MQTT: %s", zone_id, zone_state)
                                    _LOGGER.debug("Status of zone %d published to MQTT: %s", zone_id, zone_state)
                            # Caso o bypass das zonas seja atualizado
                            elif message_parts[1] == "B":
                                # Mensagem de estado das zonas, por exemplo: "@I B 1 NNNNNNNNNNNNNNNN"
                                states = message_parts[3]

                                # Processa cada caractere de estado para cada zona
                                for zone_id in range(1, len(states) + 1):
                                    bypass_state_char = states[zone_id - 1]  # Caractere correspondente à zona
                                    bypass_state = cardio2e_zones.interpret_bypass_character(bypass_state_char)

                                    # Publica o estado da zona no MQTT
                                    state_topic = f"cardio2e/zone/bypass/state/{zone_id}"
                                    mqtt_client.publish(state_topic, bypass_state, retain=False)
                                    #_LOGGER.debug("Estado da zona %d publicado no MQTT: %s", zone_id, bypass_state)
                        else:
                            _LOGGER.error("Response not processed: %s", message_parts)

        except Exception as e:
            _LOGGER.error("Error reading from RS-232 loop: %s", e)
            time.sleep(1)

def get_name(serial_conn, entity_id, entity_type, mqtt_client, max_retries=3, timeout=10):
    """
    Consulta o nome de uma luz, zona ou outra entidade via RS-232, processa a resposta e publica no MQTT.
    :param serial_conn: Conexão serial RS-232.
    :param entity_id: Identificador da entidade (luz ou zona).
    :param entity_type: Tipo da entidade ("L" para light, "R" para switch, "C" para cover, "H" para HVAC, "Z" para zone, "S" para security).
    :param mqtt_client: Cliente MQTT para publicação.
    :param max_retries: Número máximo de tentativas.
    :param timeout: Tempo limite para resposta.
    :return: Nome da entidade.
    """
    
    # If entity_type is security ("S"), skip the name consult and publish only the autodiscovery config
    if entity_type == "S":
        entity_name = f"Security {entity_id}"  # Nome padrão para a entidade de segurança
        publish_autodiscovery_config(mqtt_client, entity_id, entity_name, entity_type)
        _LOGGER.info("Published autodiscovery config for security entity %s %d without fetching name.", entity_type, entity_id)
        return entity_name

    # Caso contrário, continua com a lógica normal para buscar o nome
    command = f"@G N {entity_type} {entity_id}{CARDIO2E_TERMINATOR}"
    attempts = 0

    while attempts < max_retries:
        try:
            # Envia o comando para obter o nome da entidade
            serial_conn.write(command.encode())
            _LOGGER.debug("Command sent to get entity name %s %d: %s", entity_type, entity_id, command.strip())

            start_time = time.time()
            received_message = ""

            # Loop para aguardar uma resposta válida dentro do tempo limite
            while time.time() - start_time < timeout:
                received_message = serial_conn.readline().decode(errors="ignore").strip()

                # Processa somente se a mensagem começar com o prefixo esperado para o nome
                if received_message.startswith(f"@I N {entity_type}"):
                    _LOGGER.debug("Complete message received for entity name %s %d: %s", entity_type, entity_id, received_message)

                    # Captura o nome após "@I N {entity_type}" até o próximo @ ou o final da linha
                    name_part = received_message.split(f"@I N {entity_type}", 1)[-1].strip()
                    entity_name = name_part.split("@")[0].strip()  # Ignora qualquer outra mensagem após o nome

                    # Publica o nome no broker MQTT
                    if entity_type == 'L':
                        mqtt_topic = f"cardio2e/light/name/{entity_id}"
                    elif entity_type == 'R':
                        mqtt_topic = f"cardio2e/switch/name/{entity_id}"
                    elif entity_type == 'C':
                        mqtt_topic = f"cardio2e/cover/name/{entity_id}"
                    elif entity_type == 'H':
                        mqtt_topic = f"cardio2e/hvac/{entity_id}/name"
                    elif entity_type == 'Z':
                        mqtt_topic = f"cardio2e/zone/name/{entity_id}"
                    mqtt_client.publish(mqtt_topic, entity_name, retain=True)
                    _LOGGER.info("Entity name %s %d published to MQTT: %s", entity_type, entity_id, entity_name)

                    # Publica a configuração de autodiscovery para o Home Assistant
                    publish_autodiscovery_config(mqtt_client, entity_id, entity_name, entity_type)

                    return entity_name
                else:
                    # Ignora mensagens irrelevantes
                    _LOGGER.debug("Message ignored during name search: %s", received_message)

            attempts += 1
            _LOGGER.debug("Attempt %d failed to get the name of entity %s %d. Trying again.", attempts + 1, entity_type, entity_id)

        except Exception as e:
            _LOGGER.error("Error getting entity name %s %d: %s", entity_type, entity_id, e)
            attempts += 1

    # Retorna um nome padrão se todas as tentativas falharem
    default_name = "Unknown"
    _LOGGER.warning("Could not get entity name %s %d after %d attempts. Using default name: %s", entity_type, entity_id, max_retries, default_name)
    return default_name

def parse_login_response(response, mqtt_client, serial_conn):
    """
    Processa a resposta recebida durante o login e publica informações no MQTT.
    :param response: Resposta completa recebida após o login.
    :param mqtt_client: Cliente MQTT para publicação.
    """
    global hvac_states
    global bypass_states

    # Divide a resposta em mensagens individuais usando o delimitador '\r'
    messages = response.split("\r")

    for message in messages:
        _LOGGER.debug("Message parsed in login response: %s", message)
        if message.startswith("@I V"):
            # Informação de versão do sistema
            _LOGGER.info("System Version Info: %s", message)
            version_info = message.split()
            for i in range(2, len(version_info), 2):  # Par chave/valor
                if version_info[i] == "C":
                    mqtt_client.publish("cardio2e/version/controller", version_info[i + 1], retain=True)
                elif version_info[i] == "M":
                    mqtt_client.publish("cardio2e/version/module", version_info[i + 1], retain=True)
                elif version_info[i] == "P":
                    mqtt_client.publish("cardio2e/version/protocol", version_info[i + 1], retain=True)
                elif version_info[i] == "S":
                    mqtt_client.publish("cardio2e/version/serial", version_info[i + 1], retain=True)

        elif message.startswith("@I L"):
            # Estado das luzes
            match = re.match(r"@I L (\d+) (\d+)", message)
            if match:
                light_id, light_state = match.groups()
                light_state_topic = f"cardio2e/light/state/{light_id}"
                light_state_value = "ON" if int(light_state) > 0 else "OFF"
                mqtt_client.publish(light_state_topic, light_state_value, retain=True)
                if CARDIO2E_FETCH_NAMES_LIGHTS:
                    get_name(serial_conn, int(light_id), "L", mqtt_client)
                else:
                    _LOGGER.info("The flag for fetching lights names is deactivated; skipping name fetch.")
                _LOGGER.info("Light %s state published to MQTT: %s", light_id, light_state_value)

        elif message.startswith("@I R"):
            # Estado dos interruptores
            match = re.match(r"@I R (\d+) ([OC])", message)
            if match:
                switch_id, switch_state = match.groups()
                switch_state_topic = f"cardio2e/switch/state/{switch_id}"
                switch_state_value = "ON" if switch_state == "O" else "OFF"
                mqtt_client.publish(switch_state_topic, switch_state_value, retain=True)
                if CARDIO2E_FETCH_NAMES_SWITCHES:
                    get_name(serial_conn, int(switch_id), "R", mqtt_client)
                else:
                    _LOGGER.info("The flag for fetching switch names is deactivated; skipping name fetch.")
                _LOGGER.info("Switch %s state published to MQTT: %s", switch_id, switch_state_value)

        elif message.startswith("@I H"):
            # Estado dos sensores de aquecimento
            # @I H zone_number heating_setpoint cooling_setpoint fan_state system_mode
            match = re.match(r"@I H (\d+) (\d+\.\d+) (\d+\.\d+) ([SR]) ([AHCOEN])", message)
            if match:
                hvac_id, heating_setpoint, cooling_setpoint, fan_state, system_mode = match.groups()
                fan_state_value = "on" if fan_state == "R" else "off"
                if system_mode == "A":
                    hvac_state = "auto"
                elif system_mode == "H":
                    hvac_state = "heat"
                elif system_mode == "C":
                    hvac_state = "cool"
                elif system_mode == "O":
                    hvac_state = "off"
                elif system_mode == "E":
                    hvac_state = "economy"
                elif system_mode == "N":
                    hvac_state = "normal"
                else:
                    hvac_state = "Unknown"  # Caso padrão

                # Inicializa o estado do HVAC com os dados extraídos
                hvac_states = cardio2e_hvac.initialize_hvac_state(hvac_states, int(hvac_id), heating_setpoint, cooling_setpoint, fan_state, hvac_state)

                hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "heating_setpoint", heating_setpoint)
                hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "cooling_setpoint", cooling_setpoint)
                hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "fan", fan_state_value)
                hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "mode", hvac_state)
                if CARDIO2E_FETCH_NAMES_HVAC:
                    get_name(serial_conn, int(hvac_id), "H", mqtt_client)
                else:
                    _LOGGER.info("The flag for fetching hvac names is deactivated; skipping name fetch.")
                _LOGGER.info("HVAC %s state published to MQTT: Heating Set Point: %s, Cooling Set Point: %s, Fan State: %s, System mode: %s", hvac_id, heating_setpoint, cooling_setpoint, fan_state, system_mode)

        elif message.startswith("@I T"):
            # Estado dos sensores de temperatura
            match = re.match(r"@I T (\d+) (\d+\.\d+) ([HCO])", message)
            if match:
                temp_sensor_id, temp_value, temp_status = match.groups()
                if temp_status == "H":
                    temp_status_value = "heat"
                elif temp_status == "C":
                    temp_status_value = "cool"
                elif temp_status == "O":
                    temp_status_value = "off"
                else:
                    temp_status_value = "Unknown"  # Caso padrão
                hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(temp_sensor_id), "current_temperature", temp_value)
                hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(temp_sensor_id), "alternative_status_from_temp", temp_status_value)
                _LOGGER.info("Temperature sensor %s state published to MQTT: %s °C, Status: %s", temp_sensor_id, temp_value, temp_status_value)

        elif message.startswith("@I S"):
            # alarm state
            match = re.match(r"@I S 1 ([AD])", message)
            if match:
                security_state = match.group(1)
                security_state_topic = f"cardio2e/alarm/state/1"
                if security_state == "A":
                    security_state_value = "armed_away" 
                elif security_state == "D":
                    security_state_value = "disarmed" 
                else:
                    security_state_value = "unkown"
                mqtt_client.publish(security_state_topic, security_state_value, retain=True)
                _LOGGER.info("Security state published to MQTT: %s - %s", security_state_value, security_state)

        elif message.startswith("@I Z"):
            # Estado das zonas, onde cada caractere representa o estado de uma zona específica
            match = re.match(r"@I Z \d+ ([CO]+)", message)
            if match:
                zone_states = match.group(1)
                for i, state_char in enumerate(zone_states, start=1):
                    zone_state = cardio2e_zones.interpret_zone_character(state_char, i, CARDIO2E_ZONES_NORMAL_AS_OFF)
                    mqtt_client.publish(f"cardio2e/zone/state/{i}", zone_state, retain=True)
                    if CARDIO2E_FETCH_NAMES_ZONES:
                        get_name(serial_conn, int(i), "Z", mqtt_client)
                    else:
                        _LOGGER.info("The flag for fetching zones names is deactivated; skipping name fetch.")
                    _LOGGER.info("Zone %d state published to MQTT: %s", i, zone_state)

        elif message.startswith("@I B"):
            # Estado das zonas de bypass, onde cada caractere representa o estado de uma zona específica
            match = re.match(r"@I B \d+ ([NY]+)", message)
            if match:
                bypass_states = match.group(1)
                for i, bypass_state_char in enumerate(bypass_states, start=1):
                    bypass_state = cardio2e_zones.interpret_bypass_character(bypass_state_char)
                    mqtt_client.publish(f"cardio2e/zone/bypass/state/{i}", bypass_state, retain=True)
                    _LOGGER.info("Bypass state for zone %d published to MQTT: %s", i, bypass_state)

    # Force inclusion of lights in the CARDIO2E_FORCE_INCLUDE_LIGHTS list
    for light_id in CARDIO2E_FORCE_INCLUDE_LIGHTS:
        _LOGGER.info("Forcing initialization of light %s (not found in login response)", light_id)
        # Inicialize o estado padrão (desligado ou outro valor apropriado)
        light_state_topic = f"cardio2e/light/state/{light_id}"
        light_state_value = "OFF"
        mqtt_client.publish(light_state_topic, light_state_value, retain=True)
        if CARDIO2E_FETCH_NAMES_LIGHTS:
            get_name(serial_conn, light_id, "L", mqtt_client)
        _LOGGER.info("Forced light %s state published to MQTT: %s", light_id, light_state_value)

    _LOGGER.info("Parsing completo da resposta de login.")

def get_entity_state(serial_conn, mqtt_client, entity_id, entity_type="L", num_zones=16, timeout=0.5, max_retries=5):
    """
    Consulta o estado de uma entidade (luz ou zona) via RS-232 e publica no MQTT, repetindo em caso de resposta incorreta.
    :param serial_conn: Conexão serial RS-232.
    :param mqtt_client: Cliente MQTT.
    :param entity_id: Identificador da entidade (luz ou zona).
    :param entity_type: Tipo da entidade ("L" para luz, "Z" para zona).
    :param num_zones: Número total de zonas (usado apenas para zonas e bypass zones).
    :param timeout: Tempo limite para resposta.
    :param max_retries: Número máximo de tentativas.
    :return: Estado da entidade.
    """
    global hvac_states

    # Determina o comando com base no tipo da entidade
    _LOGGER.debug("Entering get_entity_state function.")
    command = f"@G {entity_type} 1{CARDIO2E_TERMINATOR}" if entity_type == "Z" else f"@G {entity_type} {entity_id}{CARDIO2E_TERMINATOR}"
    attempts = 0

    while attempts < max_retries:
        try:
            # Enviar o comando para obter o estado da entidade
            _LOGGER.debug("Sending command to serial: %s", command)
            serial_conn.write(command.encode())
            _LOGGER.info("Sent command %s to get entity %s %d state (try %d / %d)", command.strip(), entity_type, entity_id, attempts + 1, max_retries)

            start_time = time.time()
            received_message = ""

            # Loop para aguardar uma resposta válida ou o tempo limite
            while time.time() - start_time < timeout:
                char = serial_conn.read().decode(errors="ignore")
                if char:
                    received_message += char
                    if received_message.startswith(f"@I {entity_type}") and received_message.endswith("\n"):
                        break

            # show what I received
            _LOGGER.debug("Message received: %s", received_message.strip())
            if received_message.startswith(f"@I {entity_type} "):
                _LOGGER.debug("Mensagem completa recebida para estado da entidade %s: %s", entity_type, received_message.strip())

                # Processa a mensagem para extrair o estado
                message_parts = received_message.strip().split()
                if entity_type == "L" and len(message_parts) >= 4:
                    state_message = message_parts[3]
                    state_topic = f"cardio2e/light/state/{entity_id}"
                    state = int(state_message)
                    light_state = "ON" if state > 0 else "OFF"
                    mqtt_client.publish(state_topic, light_state, retain=True)
                    _LOGGER.info("Status of light %d published to MQTT: %s", entity_id, light_state)
                    return light_state

                elif entity_type == "R" and len(message_parts) >= 4:
                    state_message = message_parts[3]
                    state_topic = f"cardio2e/switch/state/{entity_id}"
                    state = state_message
                    switch_state = "ON" if state == "O" else "OFF"
                    mqtt_client.publish(state_topic, switch_state, retain=True)
                    _LOGGER.info("Switch %d state publish on MQTT: %s", entity_id, switch_state)
                    return state

                elif entity_type == "C" and len(message_parts) >= 4:
                    state_message = message_parts[3]
                    state_topic = f"cardio2e/cover/state/{entity_id}"
                    state = state_message
                    mqtt_client.publish(state_topic, state, retain=True)
                    _LOGGER.info("Cover %d state publish on MQTT: %s", entity_id, state)
                    return state

                elif entity_type == "T" and len(message_parts) >= 4:
                    state_message = message_parts[3]
                    state_topic = f"cardio2e/hvac/{entity_id}/state/current_temperature"
                    state = state_message
                    mqtt_client.publish(state_topic, state, retain=True)
                    _LOGGER.info("Temperature %d state publish on MQTT: %s", entity_id, state)
                    return state

                elif entity_type == "H" and len(message_parts) >= 7:
                    # Mapeamento dos tópicos e mensagens correspondentes
                    topics = {
                        "heating_setpoint": message_parts[3],
                        "cooling_setpoint": message_parts[4],
                        "fan": "on" if message_parts[5] == "R" else "off",
                        "mode": message_parts[6]
                    }

                    # Publicar os setpoints de aquecimento e arrefecimento, e estado do ventilador
                    for topic_suffix, state in topics.items():
                        state_topic = f"cardio2e/hvac/{entity_id}/{topic_suffix}"
                        # update global hvac global var
                        hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(entity_id), topic_suffix, state)
                        _LOGGER.info("%s for %d state published on MQTT: %s", topic_suffix.capitalize(), entity_id, state)

                    # Mapeamento de modos de operação
                    mode_mapping = {
                        "A": "auto",
                        "H": "heat",
                        "C": "cool",
                        "O": "off",
                        "E": "economy",
                        "N": "normal"
                    }

                    mode_state = mode_mapping.get(topics["mode"], "Unknown")
                    # update global hvac global var
                    hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(entity_id), "mode", mode_state)
                    _LOGGER.info("Mode for %d state published on MQTT: %s", entity_id, mode_state)
                    return True

                elif entity_type == "S" and len(message_parts) >= 4:
                    # for security, process one 
                    state_message = message_parts[3]
                    state_topic = f"cardio2e/alarm/state/{entity_id}"
                    state = state_message
                    if state_message == "A":
                        state = "armed_away" 
                    elif state_message == "D":
                        state = "disarmed" 
                    else:
                        state = "unkown"
                    mqtt_client.publish(state_topic, state, retain=True)
                    _LOGGER.info("Security %d state publish on MQTT: %s", entity_id, state)
                    return state

                elif entity_type == "Z" and len(message_parts) >= 4:
                    # Para zonas, processa todos os estados das zonas de uma vez
                    zone_states = message_parts[3]
                    for zone_id in range(1, min(num_zones, len(zone_states)) + 1):
                        zone_state_char = zone_states[zone_id - 1]  # Pega o caractere correspondente à zona
                        zone_state = cardio2e_zones.interpret_zone_character(zone_state_char, zone_id, CARDIO2E_ZONES_NORMAL_AS_OFF)
                        state_topic = f"cardio2e/zone/state/{zone_id}"
                        mqtt_client.publish(state_topic, zone_state, retain=True)
                        _LOGGER.info("Status of zone %d published to MQTT: %s", zone_id, zone_state)
                    return zone_states  # Retorna a sequência de estados para referência

                elif entity_type == "B" and len(message_parts) >= 4:
                    states = message_parts[3]
                    for zone_id in range(1, min(num_zones, len(states)) + 1):
                        bypass_state_char = states[zone_id - 1]  # Pega o caractere correspondente à zona
                        bypass_state = cardio2e_zones.interpret_bypass_character(bypass_state_char)
                        bypass_topic = f"cardio2e/zone/bypass/state/{zone_id}"
                        mqtt_client.publish(bypass_topic, bypass_state, retain=True)
                        _LOGGER.info("Bypass status for zone %d published to MQTT: %s", zone_id, bypass_state)
                    return bypass_state

                else:
                    _LOGGER.warning("Unexpected format for entity status response %s %d: %s", entity_type, entity_id, received_message)

            _LOGGER.warning("Incorrect answer for entity %s %d, attempt %d by %d.", entity_type, entity_id, attempts + 1, max_retries)
            attempts += 1
            time.sleep(0.1)

        except Exception as e:
            _LOGGER.error("Error getting state of entity %s %d: %s", entity_type, entity_id, e)
            attempts += 1

    _LOGGER.warning("Could not get state for entity %s %d after %d attempts.", entity_type, entity_id, max_retries)
    return None

def cardio_login(serial_conn, mqtt_client, state="login", password="00000", max_retries=5, timeout=10):
    """
    Realiza o login ou logout via RS-232 enviando o comando correspondente.
    :param serial_conn: Conexão serial RS-232.
    :param mqtt_client: Cliente MQTT para publicação de dados após login.
    :param password: Senha de login a ser enviada (apenas para login).
    :param state: Estado da operação, "login" para entrar e "logout" para sair.
    :param max_retries: Número máximo de tentativas.
    :param timeout: Tempo limite para resposta de cada tentativa.
    :return: True se o login/logout foi bem-sucedido, False caso contrário.
    """
    _LOGGER.info("Logging into cardio2e (usually takes 10 seconds)...")
    if state == "login":
        command = f"@S P I {password}{CARDIO2E_TERMINATOR}"
        success_response_prefix = "@A P"
    elif state == "logout":
        command = f"@S P O{CARDIO2E_TERMINATOR}"
        success_response_prefix = "@A O"
    else:
        _LOGGER.error("Invalid state: %s. Use 'login' or 'logout'.", state)
        return False

    attempts = 0

    while attempts < max_retries:
        try:
            # Envia o comando de login ou logout
            serial_conn.write(command.encode())
            _LOGGER.debug("%s command sent: %s", state.capitalize(), command.strip())

            # Se for logout, retorne imediatamente sem tentar ler a resposta
            if state == "logout":
                _LOGGER.info("Logout command sent; no response required.")
                return True

            start_time = time.time()
            received_message = ""

            # Aguardar resposta de sucesso dentro do tempo limite
            while time.time() - start_time < timeout:
                received_message = serial_conn.readline().decode(errors="ignore").strip()

                # Verifica se a resposta indica sucesso
                if received_message.startswith(success_response_prefix):
                    _LOGGER.info("%s successful with response: %r", state.capitalize(), received_message)
                    
                    # Chama o parse_login_response apenas se for um login
                    if state == "login":
                        parse_login_response(received_message, mqtt_client, serial_conn)
                        # because on login we don't have the cover info
                        cardio2e_covers.initialize_entity_cover(serial_conn, mqtt_client, get_name, get_entity_state, CARDIO2E_NCOVERS, CARDIO2E_FETCH_NAMES_COVERS, CARDIO2E_SKIP_INIT_COVER_STATE)
                    
                    return True
                else:
                    _LOGGER.warning("%s failed with response: %r", state.capitalize(), received_message)
                    break  # Falha, então sai do loop interno

            attempts += 1
            _LOGGER.debug("Attempt %d failed for cardio2e %s. Trying again.", attempts + 1, state)

        except Exception as e:
            _LOGGER.error("Error during cardio2e %s attempt %d: %s", state, attempts + 1, e)
            attempts += 1

    _LOGGER.warning("Cardio2e %s failed after %d attempts.", state, max_retries)
    return False

def publish_autodiscovery_config(mqtt_client, entity_id, entity_name, entity_type="L"):
    """
    Publica a configuração de autodiscovery para o Home Assistant.
    :param mqtt_client: Cliente MQTT.
    :param entity_id: ID da entidade (luz ou zona).
    :param entity_name: Nome da entidade.
    :param entity_type: Tipo da entidade ("L" para luz, "Z" para zona).
    """
    _LOGGER.debug("Publishing autodiscovery info for %s", entity_name)
    if entity_type == "L":
        # Configuração de autodiscovery para luzes
        config_topic = f"homeassistant/light/cardio2e_{entity_id}/config"
        state_topic = f"cardio2e/light/state/{entity_id}"
        command_topic = f"cardio2e/light/set/{entity_id}"

        # Configuração base para luzes
        config_payload = {
            "name": entity_name,
            "unique_id": f"cardio2e_light_{entity_id}",
            "state_topic": state_topic,
            "command_topic": command_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "qos": 1,
            "retain": False,
            "device": {
                "identifiers": ["Cardio2e Lights"],
                "name": "Cardio2e Lights",
                "model": "Cardio2e",
                "manufacturer": "Cardio2e Manufacturer"
            }
        }

        # Configuração adicional para luzes dimmer
        if entity_id in CARDIO2E_DIMMER_LIGHTS:
            brightness_state_topic = f"cardio2e/light/brightness/{entity_id}"
            brightness_command_topic = command_topic

            config_payload.update({
                "brightness": True,
                "brightness_state_topic": brightness_state_topic,
                "brightness_command_topic": brightness_command_topic,
                "brightness_scale": 100,
                "on_command_type": "brightness"
            })

        # Publicar a configuração de autodiscovery para a luz
        mqtt_client.publish(config_topic, json.dumps(config_payload), retain=True)
        _LOGGER.info("Published autodiscovery config for light: %s", entity_name)

    elif entity_type == "R":
        # Configuração de autodiscovery para switches (para controle de bypass da zona)
        switch_config_topic = f"homeassistant/switch/cardio2e_switch_{entity_id}/config"
        command_topic = f"cardio2e/switch/set/{entity_id}"
        state_topic = f"cardio2e/switch/state/{entity_id}"

        switch_config_payload = {
            "name": f"{entity_name}",
            "unique_id": f"cardio2e_switch_{entity_id}",
            "command_topic": command_topic,
            "state_topic": state_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "qos": 1,
            "retain": False,
            "device": {
                "identifiers": ["Cardio2e Switches"],
                "name": "Cardio2e Switches",
                "model": "Cardio2e",
                "manufacturer": "Cardio2e Manufacturer"
            }
        }

        # Publicar a configuração de autodiscovery para o sensor da zona
        mqtt_client.publish(switch_config_topic, json.dumps(switch_config_payload), retain=True)
        _LOGGER.info("Publish autodiscovery config for switches (relays): %s", entity_name)

    elif entity_type == "C":
        # Configuração de autodiscovery para estores (cover)
        cover_config_topic = f"homeassistant/cover/cardio2e_cover_{entity_id}/config"
        position_topic = f"cardio2e/cover/state/{entity_id}"
        set_position_topic = f"cardio2e/cover/set/{entity_id}"
        command_topic = f"cardio2e/cover/command/{entity_id}"  # Tópico para comandos open/close/stop

        cover_config_payload = {
            "name": f"{entity_name}",
            "unique_id": f"cardio2e_cover_{entity_id}",
            "position_topic": position_topic,       # Mesma posição do estado para compatibilidade
            "set_position_topic": set_position_topic, # Tópico para definir a posição
            "command_topic": command_topic, # Tópico para comandos de abrir/fechar/parar
            "payload_open": "OPEN",
            "payload_close": "CLOSE",
            "payload_stop": "STOP",
            "position_open": 100,
            "position_closed": 0,
            "optimistic": False,
            "qos": 1,
            "retain": False,
            "device": {
                "identifiers": ["Cardio2e Covers"],
                "name": "Cardio2e Covers",
                "model": "Cardio2e",
                "manufacturer": "Cardio2e Manufacturer"
            }
        }

        # Publicar a configuração de autodiscovery para o estore
        mqtt_client.publish(cover_config_topic, json.dumps(cover_config_payload), retain=True)
        _LOGGER.info("Publish autodiscovery config for cover: %s", entity_name)

    elif entity_type == "H":
        # Tópicos de estado e comando base para a entidade HVAC
        state_topic_base = f"cardio2e/hvac/{entity_id}/state"
        command_topic_base = f"cardio2e/hvac/{entity_id}/set"

        # Configuração de autodiscovery para o Home Assistant como uma entidade do tipo "climate"
        climate_config_topic = f"homeassistant/climate/cardio2e_hvac_{entity_id}/config"
        climate_config_payload = {
            "name": f"{entity_name}",
            "unique_id": f"cardio2e_hvac_{entity_id}",
            "state_topic": state_topic_base,
            "current_temperature_topic": f"{state_topic_base}/current_temperature",
            
            # Set points de aquecimento e resfriamento
            #"temperature_low_state_topic": f"{state_topic_base}/cooling_setpoint",
            #"temperature_low_command_topic": f"{command_topic_base}/cooling_setpoint",
            #"temperature_high_state_topic": f"{state_topic_base}/heating_setpoint",
            #"temperature_high_command_topic": f"{command_topic_base}/heating_setpoint",
            "temperature_state_topic": f"{state_topic_base}/cooling_setpoint",
            "temperature_command_topic": f"{command_topic_base}/cooling_setpoint",
            "temp_step": "1",
            
            # Configuração de modos
            "mode_state_topic": f"{state_topic_base}/mode",
            "mode_command_topic": f"{command_topic_base}/mode",
            #"modes": ["auto", "heat", "cool", "off", "economy", "normal"],
            "modes": ["auto", "heat", "cool", "off"],
            
            # Configuração do ventilador
            "fan_mode_state_topic": f"{state_topic_base}/fan",
            "fan_mode_command_topic": f"{command_topic_base}/fan",
            "fan_modes": ["on", "off"],
            
            # Limites de temperatura
            "min_temp": 7,
            "max_temp": 35,
            
            # Qualidade de serviço e retenção
            "qos": 1,
            "retain": False,
            
            # Informações do dispositivo
            "device": {
                "identifiers": [f"Cardio2e HVAC"],
                "name": "Cardio2e HVAC",
                "model": "Cardio2e",
                "manufacturer": "Cardio2e Manufacturer"
            }
        }

        # Publicar a configuração de autodiscovery para a entidade HVAC consolidada
        mqtt_client.publish(climate_config_topic, json.dumps(climate_config_payload), retain=True)
        _LOGGER.info("Published autodiscovery config for consolidated HVAC entity: %s", entity_name)

    elif entity_type == "S":
        # Configuração de autodiscovery para o painel de controle do alarme
        alarm_config_topic = f"homeassistant/alarm_control_panel/cardio2e_alarm_{entity_id}/config"
        command_topic = f"cardio2e/alarm/set/{entity_id}"
        state_topic = f"cardio2e/alarm/state/{entity_id}"

        alarm_config_payload = {
            "name": f"{entity_name}",
            "unique_id": f"cardio2e_alarm_{entity_id}",
            "command_topic": command_topic,
            "state_topic": state_topic,
            "payload_arm_away": "armed_away",
            "payload_disarm": "disarmed",  # Comando para desarmar
            "code_arm_required": False,  # Define como True se o alarme exigir um código
            "code_disarm_required": False,  # Define como True se o alarme exigir um código para desarmar
            #"code": "REMOTE_CODE",
            "supported_features": ["arm_away", "arm_night"],
            "qos": 1,
            "retain": False,
            "device": {
                "identifiers": ["Cardio2e Alarm"],
                "name": "Cardio2e Alarm",
                "model": "Cardio2e",
                "manufacturer": "Cardio2e Manufacturer"
            }
        }

        # Publicar a configuração de autodiscovery para o alarme
        mqtt_client.publish(alarm_config_topic, json.dumps(alarm_config_payload), retain=True)
        _LOGGER.info("Published autodiscovery config for alarm: %s", entity_name)

    elif entity_type == "Z":
        # Configuração de autodiscovery para sensores binários (zonas)
        sensor_config_topic = f"homeassistant/binary_sensor/cardio2e_zone_{entity_id}/config"
        state_topic = f"cardio2e/zone/state/{entity_id}"

        sensor_config_payload = {
            "name": entity_name,
            "unique_id": f"cardio2e_zone_{entity_id}",
            "state_topic": state_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "motion",  # Ajuste conforme o tipo de sensor, ex: "motion", "door"
            "qos": 1,
            "retain": False,
            "device": {
                "identifiers": ["Cardio2e Zones"],
                "name": "Cardio2e Zones",
                "model": "Cardio2e",
                "manufacturer": "Cardio2e Manufacturer"
            }
        }

        # Publicar a configuração de autodiscovery para o sensor da zona
        mqtt_client.publish(sensor_config_topic, json.dumps(sensor_config_payload), retain=True)
        _LOGGER.info("Published autodiscovery config for binary sensor (zone): %s", entity_name)

        # Configuração de autodiscovery para o switch de bypass (ativação/desativação)
        switch_config_topic = f"homeassistant/switch/cardio2e_zone_{entity_id}_bypass/config"
        state_topic = f"cardio2e/zone/bypass/state/{entity_id}"
        command_topic = f"cardio2e/zone/bypass/set/{entity_id}"

        switch_config_payload = {
            "name": f"{entity_name} Bypass",
            "unique_id": f"cardio2e_zone_bypass_{entity_id}",
            "state_topic": state_topic,
            "command_topic": command_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "qos": 1,
            "retain": False,
            "device": {
                "identifiers": ["Cardio2e Zones"],
                "name": "Cardio2e Zones",
                "model": "Cardio2e",
                "manufacturer": "Cardio2e Manufacturer"
            }
        }

        # Publicar a configuração de autodiscovery para o switch de bypass
        mqtt_client.publish(switch_config_topic, json.dumps(switch_config_payload), retain=True)
        _LOGGER.info("Published autodiscovery config for zone bypass switch: %s", entity_name)

if __name__ == "__main__":
    main()
