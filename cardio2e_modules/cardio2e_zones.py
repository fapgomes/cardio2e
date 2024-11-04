def interpret_zone_character(character, zone_id, zones_normal_as_off):
    """
    Interpreta um caractere de estado de uma zona.
    :param character: Caractere representando o estado da zona.
    :param zone_id: Identificador da zona.
    :param zones_normal_as_off: Lista de zonas que devem ser interpretadas como OFF quando NORMAL.
    :return: "ON" para movimento detectado (OPEN ou NORMAL), "OFF" para sem movimento (CLOSED ou caso especial NORMAL em certas zonas), ou "ERROR".
    """
    if character == "O":
        return "ON"   # Movimento detectado (OPEN)
    elif character == "N":
        # Exceção para zonas especificadas que devem estar OFF quando NORMAL
        return "OFF" if zone_id in zones_normal_as_off else "ON"
    elif character == "C":
        return "OFF"  # Sem movimento (CLOSED)
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
