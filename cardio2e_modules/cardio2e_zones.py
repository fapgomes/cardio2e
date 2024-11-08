def interpret_zone_character(character, zone_id, zones_normal_as_off):
    """
    Interpreta um caractere de estado de uma zona, com inversão de estado para zonas específicas.
    :param character: Caractere representando o estado da zona.
    :param zone_id: Identificador da zona.
    :param zones_normal_as_off: Lista de zonas que devem ter o estado interpretado de forma inversa.
    :return: "ON" ou "OFF" dependendo do estado e do ajuste de inversão, ou "ERROR"/"UNKNOWN" para casos específicos.
    """
    # Determina se o estado deve ser invertido para a zona atual
    is_inverted = zone_id in zones_normal_as_off

    if character == "O":
        return "OFF" if is_inverted else "ON"   # Inverte o estado se necessário
    elif character == "N":
        # Zonas especificadas como `zones_normal_as_off` invertem o estado normal
        return "ON" if is_inverted else "OFF"
    elif character == "C":
        return "ON" if is_inverted else "OFF"  # Inverte o estado se necessário
    elif character == "E":
        return "ERROR"  # Estado de erro
    else:
        return "UNKNOWN"  # Estado desconhecido

def interpret_bypass_character(character):
    """
    Interpreta um caractere de estado de uma zona.
    :param character: Caractere representando o estado da zona.
    :param zone_id: Identificador da zona.
    :param zones_normal_as_off: Lista de zonas que devem ser interpretadas como OFF quando NORMAL.
    :return: "ON" para movimento detectado (OPEN ou NORMAL), "OFF" para sem movimento (CLOSED ou caso especial NORMAL em certas zonas), ou "ERROR".
    """
    if character == "Y":
        return "ON" 
    elif character == "N":
        return "OFF"
    else:
        return "UNKNOWN"  # Estado desconhecido
