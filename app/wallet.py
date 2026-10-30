"""Carteira W$ (dinheiro fictício dos torneios).

Todo movimento é uma linha em wallet_entries (o livro-caixa) e
players.wallet_cents é o saldo já somado, atualizado na mesma transação.
Valores sempre em centavos inteiros (W$1.000,00 = 100000).

Cada lançamento tem uma ref_key única: lançar de novo a mesma coisa (dois
cliques, recarregar a página, refazer um pagamento) não duplica nada.

W$ nunca vira R$: não existe saque. As funções aqui não fazem commit - quem
chama decide (e faz rollback se vier InsufficientFunds).
"""
import re
import sqlite3

from .db import now

# Ajustes do Admin WE Camp (site_settings), em centavos.
SETTING_DEFAULTS = {
    "torneios_bonus_inicial": 100000,     # W$1.000,00 ao abrir a carteira
    "torneios_inscricao_max": 5000,       # teto da inscrição: W$50,00
    "torneios_vitoria": 50,               # por jogo: V W$0,50
    "torneios_empate": 25,                #           E W$0,25
    "torneios_derrota": 0,                #           D W$0,00
    "torneios_abertos_por_jogador": 3,    # torneios não encerrados que um jogador pode ter criado
}
SETTING_LABELS = {
    "torneios_bonus_inicial": "Saldo inicial de cada conta",
    "torneios_inscricao_max": "Valor máximo da inscrição",
    "torneios_vitoria": "Prêmio por vitória",
    "torneios_empate": "Prêmio por empate",
    "torneios_derrota": "Prêmio por derrota",
    "torneios_abertos_por_jogador": "Torneios em aberto por criador (quantidade, não W$)",
}
MONEY_SETTINGS = ("torneios_bonus_inicial", "torneios_inscricao_max", "torneios_vitoria",
                  "torneios_empate", "torneios_derrota")

KINDS = {
    "bonus_inicial": "Saldo inicial",
    "inscricao": "Inscrição",
    "reembolso": "Reembolso de inscrição",
    "premio": "Prêmio do torneio",
    "vitoria": "Vitória",
    "empate": "Empate",
    "derrota": "Derrota",
    "estorno": "Estorno (resultado corrigido)",
    "ajuste_admin": "Ajuste da administração",
    "recarga_pix": "Recarga",
}


class InsufficientFunds(Exception):
    pass


def setting(db, key):
    try:
        row = db.execute("SELECT value FROM site_settings WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        try:
            return int(row["value"])
        except ValueError:
            pass
    return SETTING_DEFAULTS[key]


def format_cents(cents):
    """100000 -> "W$1.000,00"; -50 -> "-W$0,50"."""
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    reais, centavos = divmod(abs(cents), 100)
    return f"{sign}W${reais:,}".replace(",", ".") + f",{centavos:02d}"


def parse_cents(text):
    """"1.000,50" / "1000.5" / "50" -> centavos; ValueError se inválido ou negativo."""
    value = str(text or "").strip().replace("W$", "").replace(" ", "")
    if not value:
        raise ValueError("Informe um valor.")
    if "," in value:
        value = value.replace(".", "").replace(",", ".")      # 1.000,50
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", value):
        value = value.replace(".", "")                        # 1.000
    match = re.fullmatch(r"(\d{1,9})(?:\.(\d{1,2}))?", value)
    if not match:
        raise ValueError("Valor inválido. Use, por exemplo, 10,00.")
    return int(match.group(1)) * 100 + int((match.group(2) or "").ljust(2, "0"))


def post(db, player_id, amount_cents, kind, ref_key, tournament_id=None, match_id=None, note="",
         user_id=None, allow_negative=False):
    """Lança um movimento. Retorna False se a ref_key já foi lançada (nada
    muda). Débito sem saldo levanta InsufficientFunds, a não ser com
    allow_negative (estornos e ajustes da administração)."""
    cursor = db.execute(
        "INSERT OR IGNORE INTO wallet_entries (player_id, amount_cents, kind, ref_key, tournament_id, match_id, "
        "note, created_by_user_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (player_id, amount_cents, kind, ref_key, tournament_id, match_id, note, user_id, now()),
    )
    if cursor.rowcount == 0:
        return False
    if amount_cents < 0 and not allow_negative:
        updated = db.execute("UPDATE players SET wallet_cents = wallet_cents + ? WHERE id = ? AND wallet_cents >= ?",
                             (amount_cents, player_id, -amount_cents)).rowcount
        if not updated:
            raise InsufficientFunds("Saldo W$ insuficiente.")
    else:
        db.execute("UPDATE players SET wallet_cents = wallet_cents + ? WHERE id = ?", (amount_cents, player_id))
    return True


def ensure_wallet(db, player_id):
    """Abre a carteira (credita o saldo inicial) na primeira vez. Barato
    depois disso: só lê players.wallet_started_at."""
    row = db.execute("SELECT wallet_started_at FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None or row["wallet_started_at"]:
        return False
    post(db, player_id, setting(db, "torneios_bonus_inicial"), "bonus_inicial", f"bonus_inicial:{player_id}")
    db.execute("UPDATE players SET wallet_started_at = ? WHERE id = ?", (now(), player_id))
    return True


def balance(db, player_id):
    row = db.execute("SELECT wallet_cents FROM players WHERE id = ?", (player_id,)).fetchone()
    return row["wallet_cents"] if row else 0
