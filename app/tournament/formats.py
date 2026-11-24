"""Os 6 formatos e a validação da configuração de cada um.

A configuração fica em tournaments.settings_json. Chaves usadas:
  league:       legs
  league_knockout: legs, playoff_qualifiers, ko_legs, final_legs, third_place
  world_cup:    groups, group_legs, qualifiers, ko_legs, final_legs, third_place
  dynamic_groups: groups, group_names, group_legs, qualifiers, ko_legs, final_legs, third_place
  ucl:          pots, per_pot, direct, ko_legs, final_legs, third_place
  knockout:     ko_legs, final_legs, third_place

A Copa (world_cup) é a antiga Copa do Mundo e também a antiga Libertadores,
que era a mesma coisa com ida e volta - opção dos grupos e do mata-mata. O
mata-mata segue os grupos: em jogo único, o cruzamento da Copa do Mundo (1º A
x 2º B); em ida e volta, pela campanha, como na Libertadores (store.advance).

Grupos dinâmicos (dynamic_groups) é a Copa com os grupos que o organizador
nomeia e que cada jogador escolhe na inscrição (tournament_entries.group_slot,
o índice em group_names); cada grupo aceita até vagas / grupos (arredondado
para cima) e o organizador reorganiza os grupos até o início (store).
"""
FORMATS = {
    "league": "Pontos corridos",
    "league_knockout": "Pontos corridos + mata-mata",
    "ucl": "Liga dos Campeões (formato novo)",
    "world_cup": "Copa (grupos + mata-mata)",
    "dynamic_groups": "Grupos dinâmicos (o jogador escolhe o grupo + mata-mata)",
    "knockout": "Eliminatórias (mata-mata)",
}

DEFAULTS = {
    "league": {"legs": 2},
    "league_knockout": {"legs": 1, "playoff_qualifiers": 4, "ko_legs": 1, "final_legs": 1, "third_place": False},
    "world_cup": {"groups": 2, "group_legs": 1, "qualifiers": 2, "ko_legs": 1, "final_legs": 1, "third_place": True},
    "dynamic_groups": {"groups": 2, "group_names": [], "group_legs": 1, "qualifiers": 2, "ko_legs": 1,
                       "final_legs": 1, "third_place": True},
    "ucl": {"pots": 4, "per_pot": 1, "direct": 2, "ko_legs": 2, "final_legs": 1, "third_place": False},
    "knockout": {"ko_legs": 1, "final_legs": 1, "third_place": False},
}

MIN_PLAYERS, MAX_PLAYERS = 2, 64
# Grupos dinâmicos: quantos grupos e o tamanho do nome de cada um.
MAX_GROUPS, GROUP_NAME_MAX = 16, 30
GROUPED = ("world_cup", "dynamic_groups")  # fase de grupos + mata-mata


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize(fmt, raw):
    """Configuração do formulário (strings, checkbox) -> dict tipado com os
    padrões do formato preenchendo o que faltar."""
    config = dict(DEFAULTS[fmt])
    for key, default in DEFAULTS[fmt].items():
        if key not in raw:
            if isinstance(default, bool):
                config[key] = False if raw.get("_form") else default
            continue
        if isinstance(default, bool):
            config[key] = raw[key] in (True, "1", "on", "true", 1)
        elif isinstance(default, list):
            config[key] = list(raw[key]) if isinstance(raw[key], (list, tuple)) else []
        else:
            config[key] = _int(raw[key], default)
    if fmt == "dynamic_groups":
        config["group_names"] = group_names(config["group_names"], config["groups"])
    return config


def default_group_name(index):
    return f"Grupo {chr(ord('A') + index) if index < 26 else index + 1}"


def group_names(names, count):
    """Os nomes dos grupos dinâmicos: um por grupo, sem espaços sobrando, no
    máximo GROUP_NAME_MAX caracteres; em branco vira "Grupo A", "Grupo B"..."""
    names = [" ".join(str(name or "").split())[:GROUP_NAME_MAX] for name in (names or [])]
    return [names[index] if index < len(names) and names[index] else default_group_name(index)
            for index in range(max(0, min(count, MAX_GROUPS)))]


def _is_power_of_two(n):
    return n >= 1 and n & (n - 1) == 0


def config_error(fmt, config, players):
    """Mensagem de erro se `players` inscritos não cabem nessa configuração,
    ou None. Chamado ao criar (com o máximo de vagas) e ao iniciar (com os
    inscritos de verdade)."""
    if fmt not in FORMATS:
        return "Formato inválido."
    if players < MIN_PLAYERS:
        return f"São necessários pelo menos {MIN_PLAYERS} jogadores."
    if players > MAX_PLAYERS:
        return f"O máximo é {MAX_PLAYERS} jogadores."
    for key in ("legs", "group_legs", "ko_legs", "final_legs"):
        if key in config and config[key] not in (1, 2):
            return "Os jogos devem ser de ida (1) ou ida e volta (2)."
    if fmt == "league_knockout":
        qualifiers = config["playoff_qualifiers"]
        if not 2 <= qualifiers < players:
            return f"Classificados para o mata-mata: entre 2 e {players - 1} (menos que o número de jogadores)."
    if fmt == "dynamic_groups":
        if not 2 <= config["groups"] <= MAX_GROUPS:
            return f"Grupos dinâmicos: de 2 a {MAX_GROUPS} grupos."
        names = config["group_names"]
        if len({name.casefold() for name in names}) < len(names):
            return "Os nomes dos grupos precisam ser diferentes."
    if fmt in GROUPED:
        groups, qualifiers = config["groups"], config["qualifiers"]
        if groups < 1:
            return "Informe o número de grupos."
        if players // groups < 2:
            return f"Com {players} jogadores dá para ter no máximo {players // 2} grupos."
        if qualifiers < 1 or qualifiers > players // groups:
            return f"Classificados por grupo: entre 1 e {players // groups}."
        if groups * qualifiers < 2:
            return "A fase de grupos precisa classificar pelo menos 2 jogadores."
    if fmt == "ucl":
        pots, per_pot, direct = config["pots"], config["per_pot"], config["direct"]
        if pots < 1 or players % pots:
            return f"O número de jogadores ({players}) precisa ser múltiplo do número de potes ({pots})."
        size = players // pots
        if per_pot not in (1, 2):
            return "Adversários por pote: 1 ou 2."
        if per_pot == 1 and size % 2:
            return f"Com 1 adversário por pote, cada pote precisa de um número par de jogadores (hoje {size})."
        if per_pot == 2 and size < 3:
            return "Com 2 adversários por pote, cada pote precisa de pelo menos 3 jogadores."
        if not _is_power_of_two(direct):
            return "Classificados direto às oitavas: 1, 2, 4, 8 ou 16."
        if direct * 3 > players:
            return (f"Com {direct} classificados direto, o playoff precisa de mais {direct * 2} jogadores "
                    f"({direct * 3} no total) - há só {players}.")
    return None


def describe(fmt, config):
    """Resumo da configuração para a página do torneio."""
    legs = lambda n: "ida e volta" if n == 2 else "jogo único"
    final = f"; final em {legs(config.get('final_legs', 1))}" if fmt != "league" else ""
    turns = lambda n: "turno e returno" if n == 2 else "turno único"
    third = "; com disputa de 3º lugar" if config.get("third_place") else ""
    if fmt == "league":
        return f"Pontos corridos em {turns(config['legs'])}."
    if fmt == "league_knockout":
        return (f"Pontos corridos em {turns(config['legs'])}; os {config['playoff_qualifiers']} primeiros vão ao "
                f"mata-mata em {legs(config['ko_legs'])}{final}{third}.")
    if fmt == "world_cup":
        return (f"{config['groups']} grupo(s) em {legs(config['group_legs'])}, {config['qualifiers']} classificado(s) "
                f"por grupo; mata-mata em {legs(config['ko_legs'])}{final}{third}.")
    if fmt == "dynamic_groups":
        return (f"{config['groups']} grupos escolhidos pelos jogadores na inscrição "
                f"({', '.join(config['group_names'])}), em {legs(config['group_legs'])}; {config['qualifiers']} "
                f"classificado(s) por grupo; mata-mata em {legs(config['ko_legs'])}{final}{third}.")
    if fmt == "ucl":
        return (f"Fase de liga com {config['pots']} potes e {config['per_pot']} adversário(s) de cada pote; "
                f"os {config['direct']} primeiros vão direto ao mata-mata e os {config['direct'] * 2} seguintes "
                f"disputam o playoff; mata-mata em {legs(config['ko_legs'])}{final}{third}.")
    return f"Mata-mata em {legs(config['ko_legs'])}{final}{third}."
