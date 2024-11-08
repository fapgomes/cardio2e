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

### LIGHTS
CARDIO2E_FETCH_LIGHT_NAMES = config['cardio2e'].get('fetch_light_names', 'false').lower() == 'true'
CARDIO2E_SKIP_INIT_LIGHT_STATE = config['cardio2e'].get('skip_init_light_state', 'false').lower() == 'true'
CARDIO2E_NLIGHTS = int(config['cardio2e'].get('nlights', 10))
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

### SWITCHES
CARDIO2E_FETCH_SWITCH_NAMES = config['cardio2e'].get('fetch_switch_names', 'false').lower() == 'true'
CARDIO2E_SKIP_INIT_SWITCH_STATE = config['cardio2e'].get('skip_init_switch_state', 'false').lower() == 'true'
CARDIO2E_NSWITCHES = int(config['cardio2e'].get('nswitches', 16))

### COVER
CARDIO2E_FETCH_COVER_NAMES = config['cardio2e'].get('fetch_cover_names', 'false').lower() == 'true'
CARDIO2E_SKIP_INIT_COVER_STATE = config['cardio2e'].get('skip_init_cover_state', 'false').lower() == 'true'
CARDIO2E_NCOVERS = int(config['cardio2e'].get('ncovers', 20))

### ZONES
CARDIO2E_FETCH_ZONE_NAMES = config['cardio2e'].get('fetch_zone_names', 'false').lower() == 'true'
CARDIO2E_SKIP_INIT_ZONE_STATE = config['cardio2e'].get('skip_init_zone_state', 'false').lower() == 'true'
CARDIO2E_NZONES = int(config['cardio2e'].get('nzones', 16))
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
        ### LIGHTS
        ############
        # publish the mqtt names if the CARDIO2E_FETCH_LIGHT_NAMES is True
        _LOGGER.debug("Fetch the names: %s", CARDIO2E_FETCH_LIGHT_NAMES)
        if CARDIO2E_FETCH_LIGHT_NAMES:
            num_lights = CARDIO2E_NLIGHTS  # Ajuste conforme o número de luzes
            for light_id in range(1, num_lights + 1):
                get_name(serial_conn, light_id, "L", mqtt_client)
        else:
            _LOGGER.info("The flag FETCH_LIGHT_NAMES is desativated; I will not fetch the names.")
        # init lights  state on mqtt
        if CARDIO2E_SKIP_INIT_LIGHT_STATE:
            _LOGGER.info("Skipped initial light state.")
        else:
            initialize_entity_states(serial_conn, mqtt_client, CARDIO2E_NLIGHTS, "L")
        ############
        ### SWITCHES
        ############
        if CARDIO2E_FETCH_SWITCH_NAMES:
            num_switches = CARDIO2E_NSWITCHES  # Ajuste conforme o número de luzes
            for switch_id in range(1, num_switches + 1):
                get_name(serial_conn, switch_id, "R", mqtt_client)
        else:
            _LOGGER.info("The flag CARDIO2E_FETCH_SWITCH_NAMES is desativated; I will not fetch the names.")
        # init switch state on mqtt
        if CARDIO2E_SKIP_INIT_SWITCH_STATE:
            _LOGGER.info("Skipped initial switch state.")
        else:
            initialize_entity_states(serial_conn, mqtt_client, CARDIO2E_NZONES, "R")
        ############
        ### COVERS
        ############
        if CARDIO2E_FETCH_COVER_NAMES:
            num_covers = CARDIO2E_NCOVERS  # Ajuste conforme o número de luzes
            for cover_id in range(1, num_covers + 1):
                get_name(serial_conn, cover_id, "C", mqtt_client)
        else:
            _LOGGER.info("The flag CARDIO2E_FETCH_COVER_NAMES is desativated; I will not fetch the names.")
        # init cover state on mqtt
        if CARDIO2E_SKIP_INIT_COVER_STATE:
            _LOGGER.info("Skipped initial cover state.")
        else:
            initialize_entity_states(serial_conn, mqtt_client, CARDIO2E_NCOVERS, "C")
        ############
        ### ZONES
        ############
        if CARDIO2E_FETCH_ZONE_NAMES:
            num_zones = CARDIO2E_NZONES  # Ajuste conforme o número de luzes
            for zone_id in range(1, num_zones + 1):
                get_name(serial_conn, zone_id, "Z", mqtt_client)
        else:
            _LOGGER.info("The flag FETCH_ZONE_NAMES is desativated; I will not fetch the names.")
        # init zones state on mqtt
        if CARDIO2E_SKIP_INIT_ZONE_STATE:
            _LOGGER.info("Skipped initial zone state.")
        else:
            initialize_entity_states(serial_conn, mqtt_client, CARDIO2E_NZONES, "Z")

        # Inicia a thread de escuta na porta serial
        listener_thread = threading.Thread(target=listen_for_updates, args=(serial_conn, mqtt_client), daemon=True)
        listener_thread.start()

        # Mantém o programa principal ativo
        while True:
            time.sleep(1)

    except Exception as e:
        _LOGGER.error("Falha ao configurar Cardio2e: %s", e)

# Funções MQTT
def on_mqtt_connect(client, userdata, flags, rc):
    """Callback para quando o cliente MQTT se conecta."""
    _LOGGER.info("Connected to broker MQTT with code %s", rc)
    client.subscribe("cardio2e/light/set/#")
    client.subscribe("cardio2e/switch/set/#")
    client.subscribe("cardio2e/cover/set/#")
    client.subscribe("cardio2e/zone/bypass/set/#")

def on_mqtt_message(client, userdata, msg):
    """Callback para quando uma mensagem é recebida em um tópico assinado."""
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

    # Verifica se a mensagem é para controle de estores (cover)
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

    # Checks if the message is for bypass control of a zone
    elif topic.startswith("cardio2e/zone/bypass/set/"):
        zone_bypass_states = ["N"] * CARDIO2E_NZONES  # 'N' significa ativo, 'Y' significa bypass
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

def send_rs232_command(serial_conn, entity_type, entity_id, state):
    """Envia comando para o RS-232 para alterar o estado da luz ou bypass da zona."""
    command = f"@S {entity_type} {entity_id} {state}\r"  # Comando para controle de luz
    try:
        _LOGGER.debug("Enviando comando para RS-232: %s", command)
        serial_conn.write(command.encode())
    except Exception as e:
        _LOGGER.error("Erro ao enviar comando para RS-232: %s", e)

# listen for rs232 updates
def listen_for_updates(serial_conn, mqtt_client):
    """Escuta as atualizações na porta RS-232 e publica o estado e o brilho no MQTT."""
    while True:
        if not serial_conn.is_open:
            _LOGGER.debug("The serial connection was closed.")
            break
        try:
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
                    if len(message_parts) == 3 and message_parts[0] == "@A":
                        if message_parts[1] == "L":
                            # Comando para controle de luz "@A L <light_id>"
                            light_id = int(message_parts[2])
                            # Consultar o estado atual e publicar no MQTT
                            get_entity_state(serial_conn, mqtt_client, light_id, "L")
                        elif message_parts[1] == "R":
                            # Comando para controle de luz "@A R <relay_id>"
                            switch_id = int(message_parts[2])
                            # Consultar o estado atual e publicar no MQTT
                            get_entity_state(serial_conn, mqtt_client, switch_id, "R")
                        elif message_parts[1] == "C":
                            # Comando para controle de luz "@A R <relay_id>"
                            cover_id = int(message_parts[2])
                            # Consultar o estado atual e publicar no MQTT
                            get_entity_state(serial_conn, mqtt_client, cover_id, "C")

                    elif len(message_parts) == 4 and message_parts[0] == "@I":
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
                        # Caso o estado das zonas seja atualizado
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

    if entity_type == "L" or entity_type == "R" or entity_type == "C":
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
    Consulta o nome de uma luz ou zona via RS-232, processa a resposta e publica no MQTT.
    :param serial_conn: Conexão serial RS-232.
    :param entity_id: Identificador da entidade (luz ou zona).
    :param entity_type: Entity Type ("L" for light, "R" for switch, "Z" for zone).
    :param mqtt_client: Cliente MQTT para publicação.
    :param max_retries: Número máximo de tentativas.
    :param timeout: Tempo limite para resposta.
    :return: Nome da entidade.
    """
    command = f"@G N {entity_type} {entity_id}\n\r"
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

                    # Publish the name on the MQTT broker
                    if entity_type == 'L':
                        mqtt_topic = f"cardio2e/light/name/{entity_id}"
                    elif entity_type == 'R':
                        mqtt_topic = f"cardio2e/switch/name/{entity_id}"
                    elif entity_type == 'C':
                        mqtt_topic = f"cardio2e/cover/name/{entity_id}"
                    elif entity_type == 'Z':
                        mqtt_topic = f"cardio2e/zone/name/{entity_id}"
                    mqtt_client.publish(mqtt_topic, entity_name, retain=True)
                    _LOGGER.info("Entity name %s %d published to MQTT: %s", entity_type, entity_id, entity_name)

                    # Publica a configuração de autodiscovery para o Home Assistant, apenas para luzes
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
    default_name = "Unkown"
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
            match = re.match(r"@I H (\d+) (\d+\.\d+) (\d+\.\d+) S ([OH])", message)
            if match:
                heater_id, temp_current, temp_setpoint, status = match.groups()
                heater_topic = f"cardio2e/heater/{heater_id}"
                heater_state = "ON" if status == "O" else "OFF"
                mqtt_client.publish(f"{heater_topic}/current_temp", temp_current, retain=True)
                mqtt_client.publish(f"{heater_topic}/setpoint_temp", temp_setpoint, retain=True)
                mqtt_client.publish(f"{heater_topic}/status", heater_state, retain=True)
                _LOGGER.info("Heater %s state published to MQTT: Current: %s, Setpoint: %s, Status: %s", heater_id, temp_current, temp_setpoint, heater_state)

        elif message.startswith("@I T"):
            # Estado dos sensores de temperatura
            match = re.match(r"@I T (\d+) (\d+\.\d+) ([OH])", message)
            if match:
                temp_sensor_id, temp_value, temp_status = match.groups()
                temp_status_value = "ON" if temp_status == "O" else "OFF"
                mqtt_client.publish(f"cardio2e/temperature/{temp_sensor_id}", temp_value, retain=True)
                mqtt_client.publish(f"cardio2e/temperature/{temp_sensor_id}/status", temp_status_value, retain=True)
                _LOGGER.info("Temperature sensor %s state published to MQTT: %s °C, Status: %s", temp_sensor_id, temp_value, temp_status_value)

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
    command = f"@G {entity_type} 1\n\r" if entity_type == "Z" else f"@G {entity_type} {entity_id}\n\r"
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
                    # for switches, process one 
                    state_message = message_parts[3]
                    state_topic = f"cardio2e/cover/state/{entity_id}"
                    state = state_message
                    mqtt_client.publish(state_topic, state, retain=True)
                    _LOGGER.info("Cover %d state publish on MQTT: %s", entity_id, state)
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
    if state == "login":
        command = f"@S P I {password}\n\r"
        success_response_prefix = "@A P"
    elif state == "logout":
        command = "@S P O\n\r"
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