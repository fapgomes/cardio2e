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

from cardio2e_modules import cardio2e_zones

config = configparser.ConfigParser()
config.read('cardio2e.conf')

# Configurações gerais
DEBUG = int(config['global']['debug'])
HA_DISCOVER_TOPIC = config['global'].get('ha_discover_prefix', 'homeassistant')
DEFAULT_SERIAL_PORT = config['cardio2e'].get('serial_port', '/dev/ttyUSB0')
DEFAULT_BAUDRATE = int(config['cardio2e'].get('baudrate', 9600))
MQTT_BROKER = config['mqtt']['address']  # Endereço do broker MQTT
MQTT_PORT = int(config['mqtt']['port'])
MQTT_USERNAME = config['mqtt']['username']
MQTT_PASSWORD = config['mqtt']['password']

CARDIO2E_TERMINATOR="\r"

# Definição dos parâmetros padrão para cada tipo de entidade
entities_config = {
    "LIGHTS": {"fetch_names": "fetch_light_names", "skip_init_state": "skip_init_light_state", "count": "nlights", "default_count": 10},
    "SWITCHES": {"fetch_names": "fetch_switch_names", "skip_init_state": "skip_init_switch_state", "count": "nswitches", "default_count": 16},
    "COVERS": {"fetch_names": "fetch_cover_names", "skip_init_state": "skip_init_cover_state", "count": "ncovers", "default_count": 20},
    "HVAC": {"fetch_names": "fetch_names_hvac", "skip_init_state": "skip_init_state_hvac", "count": "nhvac", "default_count": 5},
    "SECURITY": {"fetch_names": "fetch_security_names", "skip_init_state": "skip_init_security_state", "count": "nsecurity", "default_count": 1},
    "ZONES": {"fetch_names": "fetch_zone_names", "skip_init_state": "skip_init_zone_state", "count": "nzones", "default_count": 16},
}

# Inicialização das variáveis
for entity, params in entities_config.items():
    globals()[f"CARDIO2E_FETCH_NAMES_{entity}"] = config['cardio2e'].get(params["fetch_names"], 'false').lower() == 'true'
    globals()[f"CARDIO2E_SKIP_INIT_STATE_{entity}"] = config['cardio2e'].get(params["skip_init_state"], 'false').lower() == 'true'
    globals()[f"CARDIO2E_N_{entity}"] = int(config['cardio2e'].get(params["count"], params["default_count"]))

########
## EXTRA VARS
########
CARDIO2E_UPDATE_DATE_INTERVAL = int(config['cardio2e'].get('update_date_interval', 3600))
CARDIO2E_ALARM_CODE = int(config['cardio2e'].get('code', 000000))
# Processa o valor de dimmer_lights a partir do arquivo de configuração
dimmer_lights_raw = config['cardio2e'].get('dimmer_lights', '[]')  # Use '[]' como padrão se não estiver no config
try:
    CARDIO2E_DIMMER_LIGHTS = ast.literal_eval(dimmer_lights_raw)
    if not isinstance(CARDIO2E_DIMMER_LIGHTS, list):
        raise ValueError("dimmer_lights no arquivo de configuração deve ser uma lista.")
    CARDIO2E_DIMMER_LIGHTS = [int(light_id) for light_id in CARDIO2E_DIMMER_LIGHTS]  # Converte cada item para int
except (ValueError, SyntaxError) as e:
    _LOGGER.error("Erro ao interpretar dimmer_lights no arquivo de configuração: %s", e)
    CARDIO2E_DIMMER_LIGHTS = []

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

if DEBUG:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

_LOGGER = logging.getLogger(__name__)


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
        serial_conn = serial.Serial(DEFAULT_SERIAL_PORT, DEFAULT_BAUDRATE, timeout=1)
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

        cardio_login(serial_conn, mqtt_client, state="login", password="000000")

        ############
        ### INITIALIZE LIGHTS, SWITCHES, COVERS, HVAC/TEMPERATURE AND ZONES (with bypass)
        ############
        initialize_entities("L", CARDIO2E_N_LIGHTS, CARDIO2E_FETCH_NAMES_LIGHTS, CARDIO2E_SKIP_INIT_STATE_LIGHTS, serial_conn, mqtt_client)
        initialize_entities("R", CARDIO2E_N_SWITCHES, CARDIO2E_FETCH_NAMES_SWITCHES, CARDIO2E_SKIP_INIT_STATE_SWITCHES, serial_conn, mqtt_client)
        initialize_entities("C", CARDIO2E_N_COVERS, CARDIO2E_FETCH_NAMES_COVERS, CARDIO2E_SKIP_INIT_STATE_COVERS, serial_conn, mqtt_client)
        initialize_entities("H", CARDIO2E_N_HVAC, CARDIO2E_FETCH_NAMES_HVAC, CARDIO2E_SKIP_INIT_STATE_HVAC, serial_conn, mqtt_client)
        initialize_entities("S", 1, CARDIO2E_FETCH_NAMES_SECURITY, False, serial_conn, mqtt_client)
        initialize_entities("Z", CARDIO2E_N_ZONES, CARDIO2E_FETCH_NAMES_ZONES, CARDIO2E_SKIP_INIT_STATE_ZONES, serial_conn, mqtt_client)

        _LOGGER.info("\n################\nCardio2e ready. Listening for events.\n################")
        # Inicia a thread de escuta na porta serial
        listener_thread = threading.Thread(target=listen_for_updates, args=(serial_conn, mqtt_client), daemon=True)
        listener_thread.start()

        # Mantém o programa principal ativo
        while True:
            time.sleep(1)

    except Exception as e:
        _LOGGER.error("Falha ao configurar Cardio2e: %s", e)

def on_mqtt_connect(client, userdata, flags, rc):
    """Callback para quando o cliente MQTT se conecta."""
    _LOGGER.info("Connected to broker MQTT with code %s", rc)

    # topics to subscribe
    client.subscribe("cardio2e/light/set/#")
    client.subscribe("cardio2e/switch/set/#")
    client.subscribe("cardio2e/cover/set/#")
    client.subscribe("cardio2e/hvac/+/set/#")  # Subscreve dinamicamente a qualquer hvac_id
    client.subscribe("cardio2e/alarm/set/#")
    client.subscribe("cardio2e/zone/bypass/set/#")

    _LOGGER.info("Subscribed to all necessary topics.")

def on_mqtt_message(client, userdata, msg):
    """Callback para quando uma mensagem é recebida em um tópico assinado."""
    # Armazena o estado atual de cada parâmetro do HVAC para cada hvac_id
    hvac_states = {}

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
            command = 100  # Valor padrão para ligar (pode ser ajustado se necessário)
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

        # Atualiza o tópico de estado com o valor convertido
        state_topic = f"cardio2e/light/state/{light_id}"
        light_state = "ON" if command > 0 else "OFF"
        client.publish(state_topic, light_state, retain=False)
        _LOGGER.debug("Updating status topic for %s with value %s", state_topic, light_state)
    
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

        # Atualiza o tópico de estado com o valor convertido
        state_topic = f"cardio2e/switch/state/{switch_id}"
        switch_state = "ON" if command == "O" else "OFF"
        client.publish(state_topic, switch_state, retain=False)
        _LOGGER.debug("Atualizando o tópico de estado para %s com valor %s", state_topic, switch_state)

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

        # Atualiza o tópico de posição do estore
        position_topic = f"cardio2e/cover/state/{cover_id}"
        client.publish(position_topic, position, retain=False)
        _LOGGER.debug("Updating position topic for %s with value %d", position_topic, position)

    # Check if the message is for HVAC
    elif topic.startswith("cardio2e/hvac/") and "/set/" in topic:
        try:
            # Extrai o ID do HVAC e o tipo de configuração do tópico
            parts = topic.split("/")
            hvac_id = int(parts[2])  # Obtém o ID do HVAC
            setting_type = parts[-1]  # Obtém o tipo de configuração (heating_setpoint, cooling_setpoint, etc.)

            # Inicializa o estado do HVAC se ainda não estiver registrado
            if hvac_id not in hvac_states:
                hvac_states[hvac_id] = {
                    "heating_setpoint": 32,  # Valores padrão iniciais
                    "cooling_setpoint": 35,
                    "fan_state": "off",
                    "mode": "off"
                }

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
            heating_setpoint = hvac_states[hvac_id]["heating_setpoint"]
            cooling_setpoint = hvac_states[hvac_id]["cooling_setpoint"]
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

            # Publica os valores atualizados nos tópicos de estado
            base_topic = f"cardio2e/hvac/{hvac_id}/state"
            client.publish(f"{base_topic}/heating_setpoint", heating_setpoint, retain=False)
            client.publish(f"{base_topic}/cooling_setpoint", cooling_setpoint, retain=False)
            client.publish(f"{base_topic}/fan", fan_state, retain=False)
            client.publish(f"{base_topic}/mode", mode, retain=False)

            _LOGGER.debug("Updated HVAC %d topics with new settings: Heating %.1f, Cooling %.1f, Fan %s, Mode %s",
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
        if payload == "ARM_AWAY":
            command = f"A {CARDIO2E_ALARM_CODE}" 
        elif payload == "DISARM":
            command = f"D {CARDIO2E_ALARM_CODE}"
        else:
            _LOGGER.error("Invalid Payload for security command: %s", payload)
            return

        # Envia comando convertido para o RS-232
        send_rs232_command(userdata["serial_conn"], "S", security_id, command)

        # Atualiza o tópico de estado com o valor convertido
        state_topic = f"cardio2e/alarm/state/{security_id}"
        security_state = "ARM_AWAY" if command == "A" else "DISARM"
        client.publish(state_topic, security_state, retain=False)
        _LOGGER.debug("Atualizando o tópico de estado para %s com valor %s", state_topic, security_state)

    # Checks if the message is for bypass control of a zone
    elif topic.startswith("cardio2e/zone/bypass/set/"):
        zone_bypass_states = ["N"] * CARDIO2E_N_ZONES  # 'N' significa ativo, 'Y' significa bypass
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

        # Envia o comando completo de bypass com o estado de todas as zonas
        send_rs232_command(userdata["serial_conn"], "B", 1, "".join(zone_bypass_states))

        # Publica o estado de bypass no MQTT para refletir a mudança
        bypass_topic = f"cardio2e/zone/bypass/state/{zone_id}"
        client.publish(bypass_topic, payload, retain=True)

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
            return

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
        # Comando para luzes e bypass de zonas
        if state is None:
            command = f"@S {entity_type} {entity_id}{CARDIO2E_TERMINATOR}"
        else:
            command = f"@S {entity_type} {entity_id} {state}{CARDIO2E_TERMINATOR}"

    try:
        _LOGGER.info("Sending command to RS-232: %s", command)
        serial_conn.write(command.encode())
    except Exception as e:
        _LOGGER.error("Error sending command to RS-232: %s", e)

# listen for cardio2e updates
def listen_for_updates(serial_conn, mqtt_client):
    """Escuta as atualizações na porta RS-232 e publica o estado e o brilho no MQTT."""
    last_time_sent = None  # Variável para armazenar o último horário de envio

    while True:
        if not serial_conn.is_open:
            _LOGGER.debug("The serial connection was closed.")
            break
        try:
            # Envia o comando de tempo a cada hora
            current_time = datetime.datetime.now()
            if last_time_sent is None or (current_time - last_time_sent).seconds >= CARDIO2E_UPDATE_DATE_INTERVAL:
                time_command = current_time.strftime("%Y%m%d%H%M%S")
                send_rs232_command(serial_conn, "D", time_command)
                _LOGGER.info("Sent time command to cardio2e: %s", time_command)
                last_time_sent = current_time

            # Ler a linha recebida do RS-232
            received_message = serial_conn.readline().decode().strip()
            if received_message:
                _LOGGER.debug("RS-232 message received: %s", received_message)

                # Dividir a linha em mensagens separadas (caso múltiplas mensagens estejam na mesma linha)
                #messages = received_message.split('@')
                # Dividir a linha em mensagens separadas com '@' e '\r' (#015) como delimitadores
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
                            get_entity_state(serial_conn, mqtt_client, light_id, "L")
                        elif message_parts[1] == "R":
                            # Comando para controle de luz "@A R <switch_id>"
                            switch_id = int(message_parts[2])
                            # Consultar o estado atual e publicar no MQTT
                            get_entity_state(serial_conn, mqtt_client, switch_id, "R")
                        elif message_parts[1] == "C":
                            # Comando para controle de luz "@A R <cover_id>"
                            cover_id = int(message_parts[2])
                            # Consultar o estado atual e publicar no MQTT
                            get_entity_state(serial_conn, mqtt_client, cover_id, "C")
                        elif message_parts[1] == "S":
                            # Comando para controle de luz "@A S <security_id>"
                            security_id = int(message_parts[2])
                            # Consultar o estado atual e publicar no MQTT
                            get_entity_state(serial_conn, mqtt_client, security_id, "S")
                    elif len(message_parts) >= 3 and message_parts[0] == "@N":
                        error_msg = ""
                        if (message_parts[3] == "1"):
                            error_msg = "Object type specified by the transaction is not recognized."
                        elif (message_parts[3] == "2"):
                            error_msg = "Object number is out of range for the object type specified."
                        elif (message_parts[3] == "3"):
                            error_msg = "One or more parameters are not valid."
                        elif (message_parts[3] == "4"):
                            error_msg = "Security code is not valid."
                        elif (message_parts[3] == "5"):
                            error_msg = "Transaction S (Set) not supported for the requested type of object."
                        elif (message_parts[3] == "6"):
                            error_msg = "Transaction G (Get) not supported for the requested type of object."
                        elif (message_parts[3] == "7"):
                            error_msg = "Transaction is refused because security is armed."
                        elif (message_parts[3] == "8"):
                            error_msg = "This zone can be ignored."
                        elif (message_parts[3] == "16"):
                            error_msg = "Security can not be armed because there are open zones."
                        elif (message_parts[3] == "17"):
                            error_msg = "Security can not be armed because there is a power problem."
                        elif (message_parts[3] == "18"):
                            error_msg = "Security can not be armed for an unknown reason."
                        else:
                            error_msg = "Unkown error message."
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
                            mqtt_client.publish(f"{base_topic}/heating_setpoint", heating_setpoint, retain=False)
                            _LOGGER.info("HVAC %d heating setpoint updated to: %s", hvac_id, heating_setpoint)

                            # Publica o setpoint de resfriamento
                            mqtt_client.publish(f"{base_topic}/cooling_setpoint", cooling_setpoint, retain=False)
                            _LOGGER.info("HVAC %d cooling setpoint updated to: %s", hvac_id, cooling_setpoint)

                            # Publica o estado do ventilador
                            mqtt_client.publish(f"{base_topic}/fan", fan_state, retain=False)
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
                            mqtt_client.publish(f"{base_topic}/mode", mode_state, retain=False)
                            _LOGGER.info("HVAC %d mode updated to: %s", hvac_id, mode_state)
                        # Caso o estado do alarme seja atualizado
                        elif message_parts[1] == "S":
                            # Estado atualizado "@I S <security_id> <state>"
                            security_id = int(message_parts[2])
                            security_state = "ARM_AWAY" if message_parts[3] == "A" else "DISARM"

                            state_topic = f"cardio2e/alarm/state/{security_id}"
                            mqtt_client.publish(state_topic, security_state, retain=False)
                            _LOGGER.info("Security %d state, updated to: %s", security_id, security_state)
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
                                #_LOGGER.debug("Estado da zona %d publicado no MQTT: %s", zone_id, zone_state)
                        # Caso o bypass das zonas seja atualizado
                        elif message_parts[1] == "B":
                            # Mensagem de estado das zonas, por exemplo: "@I B 1 NNNNNNNNNNNNNNNN"
                            bypass_states = message_parts[3]

                            # Processa cada caractere de estado para cada zona
                            for zone_id in range(1, len(bypass_states) + 1):
                                bypass_state_char = bypass_states[zone_id - 1]  # Caractere correspondente à zona
                                bypass_state = cardio2e_zones.interpret_bypass_character(bypass_state_char)

                                # Publica o estado da zona no MQTT
                                state_topic = f"cardio2e/zone/bypass/state/{zone_id}"
                                mqtt_client.publish(state_topic, bypass_state, retain=False)
                                #_LOGGER.debug("Estado da zona %d publicado no MQTT: %s", zone_id, bypass_state)
                    else:
                        _LOGGER.error("Response not processed: %s", message_parts)

        except Exception as e:
            _LOGGER.error("Error reading from RS-232: %s", e)

def initialize_entities(entity_type, num_entities, fetch_names_flag, skip_init_state_flag, serial_conn, mqtt_client):
    """
    Inicializa entidades e publica seus nomes e estados no MQTT.
    :param entity_type: Entity Type ("L" for light, "R" for switch, "C" for covers, "S" for security, "H" for HVAC, "Z" for zone, etc.)
    :param num_entities: Número de entidades desse tipo.
    :param fetch_names_flag: Flag para buscar e publicar os nomes das entidades no MQTT.
    :param skip_init_state_flag: Flag para pular a inicialização dos estados das entidades no MQTT.
    :param serial_conn: Conexão serial RS-232.
    :param mqtt_client: Cliente MQTT para publicação.
    """
    # Publica os nomes das entidades, se solicitado
    if fetch_names_flag:
        for entity_id in range(1, num_entities + 1):
            get_name(serial_conn, entity_id, entity_type, mqtt_client)
    else:
        _LOGGER.info("The flag for fetching %s names is deactivated; skipping name fetch.", entity_type)

    # Inicializa os estados das entidades, se solicitado
    if skip_init_state_flag:
        _LOGGER.info("Skipped initial %s state.", entity_type)
    else:
        initialize_entity_states(serial_conn, mqtt_client, num_entities, entity_type)

def initialize_entity_states(serial_conn, mqtt_client, num_entities, entity_type="L", interval=0.1):
    """
    Consulta o estado inicial de todas as entidades (luzes ou zonas) sequencialmente com um intervalo controlado e publica no MQTT.
    :param serial_conn: Conexão serial RS-232.
    :param mqtt_client: Cliente MQTT.
    :param num_entities: Número de entidades (luzes ou zonas).
    :param entity_type: Tipo da entidade ("L" para luz, "Z" para zona).
    :param interval: Intervalo de tempo entre cada consulta (usado apenas para luzes).
    """
    _LOGGER.info("Initializing entity state from type %s...", entity_type)

    if entity_type == "L" or entity_type == "R" or entity_type == "C" or entity_type == "S" or entity_type == "H":
        # for lights or switches, get sequencial one by one 
        for entity_id in range(1, num_entities + 1):
            get_entity_state(serial_conn, mqtt_client, entity_id, entity_type)
            time.sleep(interval)  # Intervalo entre consultas
    elif entity_type == "Z":
        # Para zonas, uma única chamada obtém o estado de todas as zonas
        get_entity_state(serial_conn, mqtt_client, 1, entity_type, num_zones=num_entities)
        get_entity_state(serial_conn, mqtt_client, 1, "B", num_zones=num_entities)

    _LOGGER.info("States of all entities of type %s have been initialized", entity_type)

def get_name(serial_conn, entity_id, entity_type, mqtt_client, max_retries=3, timeout=3.0):
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

def parse_login_response(response, mqtt_client):
    """
    Processa a resposta recebida durante o login e publica informações no MQTT.
    :param response: Resposta completa recebida após o login.
    :param mqtt_client: Cliente MQTT para publicação.
    """
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
                _LOGGER.info("Light %s state published to MQTT: %s", light_id, light_state_value)

        elif message.startswith("@I R"):
            # Estado dos interruptores
            match = re.match(r"@I R (\d+) ([OC])", message)
            if match:
                switch_id, switch_state = match.groups()
                switch_state_topic = f"cardio2e/switch/state/{switch_id}"
                switch_state_value = "ON" if switch_state == "O" else "OFF"
                mqtt_client.publish(switch_state_topic, switch_state_value, retain=True)
                _LOGGER.info("Switch %s state published to MQTT: %s", switch_id, switch_state_value)

        elif message.startswith("@I H"):
            # Estado dos sensores de aquecimento
            # @I H zone_number heating_setpoint cooling_setpoint fan_state system_mode
            match = re.match(r"@I H (\d+) (\d+\.\d+) (\d+\.\d+) ([SR]) ([AHCOEN])", message)
            if match:
                hvac_id, heating_setpoint, cooling_setpoint, fan_state, system_mode = match.groups()
                hvac_topic = f"cardio2e/hvac/{hvac_id}"
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
                mqtt_client.publish(f"{hvac_topic}/state/heating_setpoint", heating_setpoint, retain=True)
                mqtt_client.publish(f"{hvac_topic}/state/cooling_setpoint", cooling_setpoint, retain=True)
                mqtt_client.publish(f"{hvac_topic}/state/fan", fan_state_value, retain=True)
                mqtt_client.publish(f"{hvac_topic}/state/mode", hvac_state, retain=True)
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
                mqtt_client.publish(f"cardio2e/hvac/{temp_sensor_id}/state/current_temperature", temp_value, retain=True)
                mqtt_client.publish(f"cardio2e/hvac/{temp_sensor_id}/state/alternative_status_from_temp", temp_status_value, retain=True)
                _LOGGER.info("Temperature sensor %s state published to MQTT: %s °C, Status: %s", temp_sensor_id, temp_value, temp_status_value)

        elif message.startswith("@I S"):
            # alarm state
            match = re.match(r"@I S 1 ([AD])", message)
            if match:
                security_state = match.group(1)
                security_state_topic = f"cardio2e/alarm/state/1"
                if security_state == "A":
                    security_state_value = "ARM_AWAY" 
                elif security_state == "D":
                    security_state_value = "DISARM" 
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
                    _LOGGER.info("Zone %d state published to MQTT: %s", i, zone_state)

        elif message.startswith("@I B"):
            # Estado das zonas de bypass, onde cada caractere representa o estado de uma zona específica
            match = re.match(r"@I B \d+ ([NO]+)", message)
            if match:
                bypass_states = match.group(1)
                for i, bypass_state_char in enumerate(bypass_states, start=1):
                    bypass_state = cardio2e_zones.interpret_bypass_character(bypass_state_char)
                    mqtt_client.publish(f"cardio2e/zone/bypass/state/{i}", bypass_state, retain=True)
                    _LOGGER.info("Bypass state for zone %d published to MQTT: %s", i, bypass_state)

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
    # Determina o comando com base no tipo da entidade
    command = f"@G {entity_type} 1{CARDIO2E_TERMINATOR}" if entity_type == "Z" else f"@G {entity_type} {entity_id}{CARDIO2E_TERMINATOR}"
    attempts = 0

    while attempts < max_retries:
        try:
            # Enviar o comando para obter o estado da entidade
            serial_conn.write(command.encode())
            _LOGGER.debug("Enviado comando para obter estado da entidade %s %d: %s (tentativa %d)", entity_type, entity_id, command.strip(), attempts + 1)

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
                    # Para luzes, processa normalmente
                    state_message = message_parts[3]
                    state_topic = f"cardio2e/light/state/{entity_id}"
                    state = int(state_message)
                    light_state = "ON" if state > 0 else "OFF"
                    mqtt_client.publish(state_topic, light_state, retain=True)
                    _LOGGER.info("Status of light %d published to MQTT: %s", entity_id, light_state)
                    return light_state

                elif entity_type == "R" and len(message_parts) >= 4:
                    # for switches, process one 
                    state_message = message_parts[3]
                    state_topic = f"cardio2e/switch/state/{entity_id}"
                    state = state_message
                    switch_state = "ON" if state == "O" else "OFF"
                    mqtt_client.publish(state_topic, switch_state, retain=True)
                    _LOGGER.info("Switch %d state publish on MQTT: %s", entity_id, switch_state)
                    return state

                elif entity_type == "C" and len(message_parts) >= 4:
                    # for covers, process one 
                    state_message = message_parts[3]
                    state_topic = f"cardio2e/cover/state/{entity_id}"
                    state = state_message
                    mqtt_client.publish(state_topic, state, retain=True)
                    _LOGGER.info("Cover %d state publish on MQTT: %s", entity_id, state)
                    return state

                elif entity_type == "H" and len(message_parts) >= 7:
                    # Mapeamento dos tópicos e mensagens correspondentes
                    topics = {
                        "heating_setpoint": message_parts[3],
                        "cooling_setpoint": message_parts[4],
                        "fan": "on" if message_parts[5] == "R" else "off",
                        "mode": message_parts[6]
                    }

                    # Publicar os setpoints de aquecimento e resfriamento, e estado do ventilador
                    for topic_suffix, state in topics.items():
                        state_topic = f"cardio2e/hvac/{entity_id}/{topic_suffix}"
                        mqtt_client.publish(state_topic, state, retain=True)
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

                    # Publicar o modo de operação
                    mode_state = mode_mapping.get(topics["mode"], "Unknown")
                    mqtt_client.publish(f"cardio2e/hvac/{entity_id}/mode", mode_state, retain=True)
                    _LOGGER.info("Mode for %d state published on MQTT: %s", entity_id, mode_state)

                    return True

                elif entity_type == "S" and len(message_parts) >= 4:
                    # for security, process one 
                    state_message = message_parts[3]
                    state_topic = f"cardio2e/alarm/state/{entity_id}"
                    state = state_message
                    if state_message == "A":
                        state = "ARM_AWAY" 
                    elif state_message == "D":
                        state = "DISARM" 
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
                    # Para luzes, processa normalmente
                    bypass_states = message_parts[3]
                    for zone_id in range(1, min(num_zones, len(bypass_states)) + 1):
                        bypass_state_char = bypass_states[zone_id - 1]  # Pega o caractere correspondente à zona
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

def cardio_login(serial_conn, mqtt_client, state="login", password="000000", max_retries=5, timeout=3.0):
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
                    _LOGGER.info("%s successful with response: %s", state.capitalize(), received_message)
                    
                    # Chama o parse_login_response apenas se for um login
                    if state == "login":
                        parse_login_response(received_message, mqtt_client)
                    
                    return True
                else:
                    _LOGGER.warning("%s failed with response: %s", state.capitalize(), received_message)
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

        cover_config_payload = {
            "name": f"{entity_name}",
            "unique_id": f"cardio2e_cover_{entity_id}",
            "position_topic": position_topic,       # Mesma posição do estado para compatibilidade
            "set_position_topic": set_position_topic, # Tópico para definir a posição
            "payload_open": "100",
            "payload_closed": "0",
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
            "min_temp": 5,
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
            #"payload_ARM_AWAY": "ARM",  # Usaremos "ARM" como comando genérico para armar
            "payload_arm": "ARM_AWAY",  # Usaremos "ARM" como comando genérico para armar
            "payload_disarm": "DISARM",  # Comando para desarmar
            "code_arm_required": False,  # Define como True se o alarme exigir um código
            "code_disarm_required": False,  # Define como True se o alarme exigir um código para desarmar
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
            "retain": True,
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
