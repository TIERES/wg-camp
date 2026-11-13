import sqlite3
from datetime import datetime, timezone

import click
from flask import current_app, g
from werkzeug.security import generate_password_hash


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Contas de jogadores e Memory Card online - separadas da tabela `users`, que
# é só de administradores. Idempotente: roda no init_db() e no migrate_db().
#  - players.username é o nick do Kaillera e nunca muda depois do cadastro
#    (o Memory Card de cada um é identificado por ele);
#  - player_tokens guarda só o SHA-256 dos tokens (confirmação de e-mail,
#    redefinição de senha e o token de API que o kailleraclient.dll usa);
#  - games é identificado pelo mesmo "CRC32:TAMANHO" (hex) que o anti-desync
#    do retroarch-k3-ffw calcula do conteúdo (.cue/.m3u resolvidos);
#  - cada versão de um cartão aponta para o arquivo pelo SHA-256 do conteúdo
#    (MEMCARDS_DIR/<sha256>.mcd), então versões iguais compartilham o arquivo;
#  - um memcard sem player_id é o "M. Card da ISO": um cartão só do jogo,
#    usado por todos que jogam nesse modo (no máximo um por jogo);
#  - iso_card_locks: a partida que está usando o M. Card da ISO de um jogo
#    (uma por vez - holder = os nicks dela; expira se o DLL parar de renovar);
#  - room_tickets são os "ingressos" de uso único que provam, ao host de uma
#    sala "Só logados", que quem entrou é mesmo o dono da conta.
PLAYERS_DDL = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    email_verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS player_tokens (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('verify', 'reset', 'api')),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    used_at TEXT,
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS player_tokens_player_idx ON player_tokens(player_id, kind);

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    content_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memcards (
    id INTEGER PRIMARY KEY,
    player_id INTEGER REFERENCES players(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE RESTRICT,
    current_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    iso_slot INTEGER NOT NULL DEFAULT 1,
    UNIQUE (player_id, game_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS memcards_iso_slot_idx ON memcards(game_id, iso_slot) WHERE player_id IS NULL;

CREATE TABLE IF NOT EXISTS memcard_versions (
    id INTEGER PRIMARY KEY,
    memcard_id INTEGER NOT NULL REFERENCES memcards(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('blank', 'upload', 'match', 'restore')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (memcard_id, version)
);

CREATE TABLE IF NOT EXISTS room_tickets (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    room TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    verified_by INTEGER REFERENCES players(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS iso_card_locks (
    game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    holder TEXT NOT NULL,
    players TEXT NOT NULL DEFAULT '',
    room_game TEXT NOT NULL DEFAULT '',
    server TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS iso_card_locks_room_game_idx ON iso_card_locks(room_game COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS voice_requests (
    id INTEGER PRIMARY KEY,
    room_key TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    UNIQUE (room_key, discord_id)
);

CREATE TABLE IF NOT EXISTS voice_channels (
    id INTEGER PRIMARY KEY,
    room_key TEXT NOT NULL UNIQUE,
    channel_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    member_ids TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_join_at TEXT NOT NULL
);
"""


#  Torneios (/torneios - app/tournaments.py, motor em app/tournament/):
#  - game_titles: os jogos dos torneios (lista fixa em app/tournament/games.py)
#    (não confundir com `games`, que é o conteúdo da ISO do Memory Card);
#  - um torneio é uma sequência de fases (tournament_stages): liga, grupos,
#    fase de liga suíça (Liga dos Campeões) ou mata-mata;
#  - no mata-mata, tournament_ties é o confronto (agrega ida e volta) e aponta
#    para onde vão o vencedor (next_tie_id) e o perdedor (disputa de 3º);
#  - wallet_entries é o livro-caixa do W$ (app/wallet.py), ref_key única.
TOURNAMENTS_DDL = """
CREATE TABLE IF NOT EXISTS game_titles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    platform TEXT NOT NULL DEFAULT '',
    championship_id INTEGER REFERENCES championships(id) ON DELETE SET NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    game_title_id INTEGER NOT NULL REFERENCES game_titles(id) ON DELETE RESTRICT,
    organizer_id INTEGER NOT NULL REFERENCES players(id) ON DELETE RESTRICT,
    format TEXT NOT NULL
        CHECK (format IN ('league', 'league_knockout', 'ucl', 'world_cup', 'libertadores', 'knockout')),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'registration', 'running', 'finished', 'cancelled')),
    max_players INTEGER NOT NULL,
    entry_fee_cents INTEGER NOT NULL DEFAULT 0 CHECK (entry_fee_cents >= 0),
    prize_split TEXT NOT NULL DEFAULT '60,30,10',
    settings_json TEXT NOT NULL DEFAULT '{}',
    rules TEXT NOT NULL DEFAULT '',
    -- free: o jogador digita o time; choose/draw: times da lista tournament_teams.
    team_mode TEXT NOT NULL DEFAULT 'free' CHECK (team_mode IN ('free', 'choose', 'draw')),
    -- Sem uso: seleções aparecem sempre com a bandeira (a opção "escudo da federação" saiu).
    team_display TEXT NOT NULL DEFAULT 'flag' CHECK (team_display IN ('logo', 'flag')),
    -- Datas prováveis de início e fim (AAAA-MM-DD), informadas pelo organizador.
    starts_on TEXT,
    ends_on TEXT,
    started_at TEXT,
    finished_at TEXT,
    -- Último jogo confirmado (aviso ao organizador; encerra sozinho 24 h depois - notify.py).
    all_played_at TEXT,
    -- Imagem do campeonato enviada pelo organizador (arquivo em TOURNAMENT_IMAGES_DIR - app/images.py).
    image TEXT,
    -- A imagem como aparece no link compartilhado (WhatsApp, Telegram...): um JPEG 1200 x 630 montado no
    -- navegador de quem gerencia (static/js/share-image.js), na mesma pasta da imagem.
    share_image TEXT,
    -- Somente para amigos (privado): só o organizador, os amigos dele e os inscritos o veem nas listas e entram
    -- nele; só amigos se inscrevem. Com o link, os outros veem o aviso e a Visão geral (tournaments.show).
    friends_only INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS tournaments_status_idx ON tournaments(status);

-- ISO do campeonato (app/tournament/iso.py): fica em DOWNLOADS_DIR até o
-- archive.org confirmar a cópia (mesmo MD5); daí a local é apagada e o
-- download redireciona para archive_url.
CREATE TABLE IF NOT EXISTS tournament_isos (
    tournament_id INTEGER PRIMARY KEY REFERENCES tournaments(id) ON DELETE CASCADE,
    stored_name TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    file_size INTEGER NOT NULL CHECK (file_size >= 0),
    md5 TEXT NOT NULL,
    archive_name TEXT NOT NULL,
    archive_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (archive_status IN ('pending', 'uploading', 'verifying', 'done', 'error')),
    archive_url TEXT,
    archive_error TEXT,
    archive_updated_at TEXT,
    local_removed_at TEXT,
    uploaded_by INTEGER REFERENCES players(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Ajudantes do campeonato (app/tournament/editors.py): amigos que o organizador
-- escolhe para ajudar a editar os dados, a imagem e a ISO. Com ajudantes, uma
-- alteração feita por um responsável (organizador ou ajudante) só entra depois
-- que outro responsável confirma (tournament_changes).
CREATE TABLE IF NOT EXISTS tournament_editors (
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    added_by INTEGER REFERENCES players(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tournament_id, player_id)
);
CREATE INDEX IF NOT EXISTS tournament_editors_player_idx ON tournament_editors(player_id);

-- Alteração do campeonato esperando a confirmação de outro responsável
-- (app/tournament/changes.py). Uma por campeonato; cada parte é opcional:
--   values_json: o formulário "Dados do campeonato" validado;
--   image_change = 1: a imagem nova (image; NULL = tirar a imagem);
--   stored_name...: a ISO nova, guardada só neste servidor até a confirmação
--   (só então substitui a atual e vai para o archive.org).
CREATE TABLE IF NOT EXISTS tournament_changes (
    id INTEGER PRIMARY KEY,
    tournament_id INTEGER NOT NULL UNIQUE REFERENCES tournaments(id) ON DELETE CASCADE,
    requested_by INTEGER REFERENCES players(id) ON DELETE SET NULL,
    values_json TEXT,
    image_change INTEGER NOT NULL DEFAULT 0 CHECK (image_change IN (0, 1)),
    image TEXT,
    stored_name TEXT,
    original_filename TEXT,
    file_size INTEGER,
    md5 TEXT,
    content_id TEXT,
    submitted_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tournament_entries (
    id INTEGER PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE RESTRICT,
    team_name TEXT NOT NULL DEFAULT '',
    team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    fee_ref TEXT,
    final_position INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (tournament_id, player_id)
);

-- Um time por jogador e nenhum time repetido no mesmo campeonato.
CREATE UNIQUE INDEX IF NOT EXISTS tournament_entries_team_idx ON tournament_entries(tournament_id, team_id)
    WHERE team_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS tournament_entries_team_name_idx
    ON tournament_entries(tournament_id, team_name COLLATE NOCASE) WHERE team_name != '';

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'club' CHECK (kind IN ('club', 'national', 'custom')),
    country TEXT NOT NULL DEFAULT '',
    logo TEXT,
    flag TEXT,
    source_key TEXT UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_groups (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_group_members (
    group_id INTEGER NOT NULL REFERENCES team_groups(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, team_id)
);

CREATE TABLE IF NOT EXISTS tournament_teams (
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    PRIMARY KEY (tournament_id, team_id)
);

CREATE TABLE IF NOT EXISTS tournament_stages (
    id INTEGER PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('league', 'groups', 'swiss', 'knockout')),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done')),
    settings_json TEXT NOT NULL DEFAULT '{}',
    all_played_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tournament_id, seq)
);

CREATE TABLE IF NOT EXISTS tournament_groups (
    id INTEGER PRIMARY KEY,
    stage_id INTEGER NOT NULL REFERENCES tournament_stages(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tournament_group_members (
    group_id INTEGER NOT NULL REFERENCES tournament_groups(id) ON DELETE CASCADE,
    entry_id INTEGER NOT NULL REFERENCES tournament_entries(id) ON DELETE CASCADE,
    draw_order INTEGER NOT NULL,
    PRIMARY KEY (group_id, entry_id)
);

CREATE TABLE IF NOT EXISTS tournament_ties (
    id INTEGER PRIMARY KEY,
    stage_id INTEGER NOT NULL REFERENCES tournament_stages(id) ON DELETE CASCADE,
    round INTEGER NOT NULL,
    position INTEGER NOT NULL,
    legs INTEGER NOT NULL DEFAULT 1 CHECK (legs IN (1, 2)),
    is_third_place INTEGER NOT NULL DEFAULT 0,
    entry_a INTEGER REFERENCES tournament_entries(id) ON DELETE SET NULL,
    entry_b INTEGER REFERENCES tournament_entries(id) ON DELETE SET NULL,
    is_bye INTEGER NOT NULL DEFAULT 0,
    winner_entry_id INTEGER REFERENCES tournament_entries(id) ON DELETE SET NULL,
    next_tie_id INTEGER REFERENCES tournament_ties(id) ON DELETE SET NULL,
    next_slot TEXT CHECK (next_slot IN ('a', 'b')),
    loser_next_tie_id INTEGER REFERENCES tournament_ties(id) ON DELETE SET NULL,
    loser_next_slot TEXT CHECK (loser_next_slot IN ('a', 'b'))
);

CREATE INDEX IF NOT EXISTS tournament_ties_stage_idx ON tournament_ties(stage_id, round, position);

CREATE TABLE IF NOT EXISTS tournament_matches (
    id INTEGER PRIMARY KEY,
    stage_id INTEGER NOT NULL REFERENCES tournament_stages(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES tournament_groups(id) ON DELETE CASCADE,
    tie_id INTEGER REFERENCES tournament_ties(id) ON DELETE CASCADE,
    round INTEGER NOT NULL,
    leg INTEGER NOT NULL DEFAULT 1,
    home_entry_id INTEGER NOT NULL REFERENCES tournament_entries(id) ON DELETE CASCADE,
    away_entry_id INTEGER NOT NULL REFERENCES tournament_entries(id) ON DELETE CASCADE,
    home_goals INTEGER,
    away_goals INTEGER,
    home_pens INTEGER,
    away_pens INTEGER,
    -- reported/disputed: confirmação entre os jogadores (fase 2).
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'reported', 'disputed', 'confirmed')),
    result_version INTEGER NOT NULL DEFAULT 0,
    -- Placar reportado por um jogador, esperando o adversário (app/tournament/reports.py).
    reported_by_entry_id INTEGER REFERENCES tournament_entries(id) ON DELETE SET NULL,
    reported_at TEXT,
    confirmed_at TEXT,
    live_session_id INTEGER REFERENCES live_sessions(id) ON DELETE SET NULL,
    -- Quem saiu da sessão do Kaillera no meio do jogo (app/live.py): perde o
    -- triplo de XP se o placar lido do jogo for confirmado (xp.QUIT_FACTOR).
    quitter_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS tournament_matches_stage_idx ON tournament_matches(stage_id, round);
CREATE INDEX IF NOT EXISTS tournament_matches_status_idx ON tournament_matches(status, reported_at);

-- Histórico de cada jogo: reporte, confirmação, contestação, decisão do
-- organizador/admin, confirmação automática e placar apagado.
CREATE TABLE IF NOT EXISTS match_reports (
    id INTEGER PRIMARY KEY,
    match_id INTEGER NOT NULL REFERENCES tournament_matches(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('report', 'confirm', 'dispute', 'resolve', 'auto_confirm', 'clear')),
    entry_id INTEGER REFERENCES tournament_entries(id) ON DELETE SET NULL,
    player_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    home_goals INTEGER,
    away_goals INTEGER,
    home_pens INTEGER,
    away_pens INTEGER,
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS match_reports_match_idx ON match_reports(match_id, id);
CREATE INDEX IF NOT EXISTS tournament_matches_tie_idx ON tournament_matches(tie_id);

CREATE TABLE IF NOT EXISTS wallet_entries (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('bonus_inicial', 'inscricao', 'reembolso', 'premio', 'vitoria', 'empate',
                                       'derrota', 'estorno', 'ajuste_admin', 'recarga_pix')),
    ref_key TEXT NOT NULL UNIQUE,
    tournament_id INTEGER REFERENCES tournaments(id) ON DELETE SET NULL,
    match_id INTEGER REFERENCES tournament_matches(id) ON DELETE SET NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS wallet_entries_player_idx ON wallet_entries(player_id, id);
CREATE INDEX IF NOT EXISTS wallet_entries_tournament_idx ON wallet_entries(tournament_id, kind);
CREATE INDEX IF NOT EXISTS wallet_entries_match_idx ON wallet_entries(match_id);

-- Amizade: pedido de requester para addressee; aceito vira 'accepted', recusado é apagado.
CREATE TABLE IF NOT EXISTS friendships (
    id INTEGER PRIMARY KEY,
    requester_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    addressee_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted')),
    created_at TEXT NOT NULL,
    responded_at TEXT,
    UNIQUE (requester_id, addressee_id),
    CHECK (requester_id != addressee_id)
);
CREATE INDEX IF NOT EXISTS friendships_addressee_idx ON friendships(addressee_id, status);

-- Convite para um campeonato com inscrições abertas (a inscrição continua sendo feita na página do campeonato).
CREATE TABLE IF NOT EXISTS tournament_invites (
    id INTEGER PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    invited_by INTEGER REFERENCES players(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'declined')),
    created_at TEXT NOT NULL,
    UNIQUE (tournament_id, player_id)
);
CREATE INDEX IF NOT EXISTS tournament_invites_player_idx ON tournament_invites(player_id, status);

-- ISOs cadastradas pela administração do WE Camp (app/tournament/iso_catalog.py),
-- para os amistosos: nome, jogo base, imagem opcional (TOURNAMENT_IMAGES_DIR) e o
-- arquivo - uma ISO já enviada num Campeonato (file_id) ou enviada aqui, com o
-- mesmo fluxo do archive.org das ISOs de campeonato (colunas iguais às de tournament_isos).
CREATE TABLE IF NOT EXISTS catalog_isos (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    game_title_id INTEGER NOT NULL REFERENCES game_titles(id) ON DELETE RESTRICT,
    image TEXT,
    -- A imagem como aparece no link compartilhado da página da ISO (JPEG 1200 x 630 montado
    -- no navegador de um responsável - static/js/share-image.js), como a do campeonato.
    share_image TEXT,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    stored_name TEXT,
    original_filename TEXT,
    file_size INTEGER,
    md5 TEXT,
    archive_name TEXT,
    archive_status TEXT,
    archive_url TEXT,
    archive_error TEXT,
    archive_updated_at TEXT,
    local_removed_at TEXT,
    archive_progress INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    -- ISO enviada por um jogador ("Não encontrou a ISO?"): fica inativa até a
    -- administração liberar (approved_at), confirmando nome, jogo base e imagem.
    requested_by INTEGER REFERENCES players(id) ON DELETE SET NULL,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Ajudantes da ISO (app/tournament/editors.py): amigos que quem enviou escolhe para
-- ajudar a editar o nome, o jogo base, a imagem e o arquivo. Quem enviou e os
-- ajudantes são os responsáveis: a alteração feita por um deles numa ISO já liberada
-- entra assim que outro responsável confirma (ou pela administração / pelo prazo).
CREATE TABLE IF NOT EXISTS catalog_iso_editors (
    catalog_iso_id INTEGER NOT NULL REFERENCES catalog_isos(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    added_by INTEGER REFERENCES players(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (catalog_iso_id, player_id)
);
CREATE INDEX IF NOT EXISTS catalog_iso_editors_player_idx ON catalog_iso_editors(player_id);

-- Alteração de uma ISO já liberada, pedida por um responsável (iso_catalog.request_edit):
-- nome, jogo base, imagem e, opcionalmente, um arquivo novo (mesmas colunas do archive.org
-- de catalog_isos - vai para lá na hora). Até ser aplicada, os amistosos continuam com a
-- versão atual. Aplica quando outro responsável confirma (confirmed_by) ou a administração
-- aprova (approved_at nos dois casos), ou sozinha depois de 24 h do envio (submitted_at) -
-- sempre só depois de o arquivo novo terminar de ir para o archive.org. Uma por ISO:
-- editar de novo substitui a anterior. image_change = 1: troca a imagem pela de `image`
-- (NULL = tira a imagem).
CREATE TABLE IF NOT EXISTS catalog_iso_revisions (
    id INTEGER PRIMARY KEY,
    catalog_iso_id INTEGER NOT NULL UNIQUE REFERENCES catalog_isos(id) ON DELETE CASCADE,
    requested_by INTEGER REFERENCES players(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    game_title_id INTEGER NOT NULL REFERENCES game_titles(id) ON DELETE RESTRICT,
    image_change INTEGER NOT NULL DEFAULT 0 CHECK (image_change IN (0, 1)),
    image TEXT,
    confirmed_by INTEGER REFERENCES players(id) ON DELETE SET NULL,
    stored_name TEXT,
    original_filename TEXT,
    file_size INTEGER,
    md5 TEXT,
    content_id TEXT,
    archive_name TEXT,
    archive_status TEXT,
    archive_url TEXT,
    archive_error TEXT,
    archive_updated_at TEXT,
    archive_progress INTEGER,
    local_removed_at TEXT,
    approved_at TEXT,
    submitted_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Amistoso entre dois amigos, fora dos campeonatos (app/tournament/friendly.py):
-- invited -> accepted -> reported (placar proposto por um) -> confirmed (o outro aceitou).
-- Placar recusado = suspended, até um dos dois propor um placar que o outro aceite.
-- ranked é decidido na confirmação (proteção contra contas fake): só os que
-- valem entram no Ranking, no V/E/D e no XP; unranked_reason diz por que não.
CREATE TABLE IF NOT EXISTS friendly_matches (
    id INTEGER PRIMARY KEY,
    -- ISO escolhida (opcional): a de um campeonato ou uma do cadastro; o jogo vem dela.
    iso_tournament_id INTEGER REFERENCES tournaments(id) ON DELETE SET NULL,
    iso_catalog_id INTEGER REFERENCES catalog_isos(id) ON DELETE SET NULL,
    game_title_id INTEGER REFERENCES game_titles(id) ON DELETE SET NULL,
    home_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    away_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'invited'
        CHECK (status IN ('invited', 'declined', 'cancelled', 'accepted', 'reported', 'suspended', 'confirmed')),
    message TEXT NOT NULL DEFAULT '',
    home_goals INTEGER,
    away_goals INTEGER,
    reported_by INTEGER REFERENCES players(id) ON DELETE SET NULL,
    reported_at TEXT,
    rejected_by INTEGER REFERENCES players(id) ON DELETE SET NULL,
    rejected_note TEXT NOT NULL DEFAULT '',
    ranked INTEGER NOT NULL DEFAULT 0,
    unranked_reason TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Placar lido do jogo (Ao vivo, live.report_finished): os dois confirmam
    -- (o primeiro fica em first_confirmed_by) ou ele vale sozinho no prazo.
    game_report INTEGER NOT NULL DEFAULT 0,
    first_confirmed_by INTEGER REFERENCES players(id) ON DELETE SET NULL,
    -- Quem saiu da sessão no meio de um jogo (o placar até ali conta): perde
    -- o triplo de XP se o amistoso for confirmado (xp.QUIT_FACTOR).
    quitter_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
    -- "Placar agregado" (escolhido no convite): no fim mostra a soma dos gols
    -- dos jogos; sem ele, só os jogadores. O XP é sempre jogo a jogo (friendly_games).
    aggregate INTEGER NOT NULL DEFAULT 0,
    CHECK (home_id != away_id)
);
CREATE INDEX IF NOT EXISTS friendly_matches_home_idx ON friendly_matches(home_id, status);
CREATE INDEX IF NOT EXISTS friendly_matches_away_idx ON friendly_matches(away_id, status);
CREATE INDEX IF NOT EXISTS friendly_matches_confirmed_idx ON friendly_matches(status, ranked, confirmed_at);

-- Os jogos do amistoso lidos pelo RetroArch TIERES (live._report_friendlies),
-- do ponto de vista do amistoso (mandante x visitante): cada um vale sozinho
-- no Ranking, no V/E/D e no XP (xp.FRIENDLY_RESULTS). number = o nº do jogo
-- na lista de partidas do amistoso (live.friendly_games); quit = 1 no jogo de
-- que o quitter_id do amistoso saiu no meio.
CREATE TABLE IF NOT EXISTS friendly_games (
    friendly_id INTEGER NOT NULL REFERENCES friendly_matches(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    live_match_id INTEGER REFERENCES live_matches(id) ON DELETE SET NULL,
    home_goals INTEGER NOT NULL,
    away_goals INTEGER NOT NULL,
    home_pens INTEGER,
    away_pens INTEGER,
    quit INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (friendly_id, number)
);
"""


def ensure_player_columns(database):
    """Discord vinculado (app/discord.py) - colunas novas de players."""
    columns = {column["name"] for column in database.execute("PRAGMA table_info(players)")}
    for column in ("discord_id", "discord_name", "discord_linked_at"):
        if column not in columns:
            database.execute(f"ALTER TABLE players ADD COLUMN {column} TEXT")
    database.execute("CREATE UNIQUE INDEX IF NOT EXISTS players_discord_idx ON players(discord_id) "
                     "WHERE discord_id IS NOT NULL")
    # Carteira W$ (app/wallet.py): saldo somado e quando o saldo inicial foi creditado.
    if "wallet_cents" not in columns:
        database.execute("ALTER TABLE players ADD COLUMN wallet_cents INTEGER NOT NULL DEFAULT 0")
    if "wallet_started_at" not in columns:
        database.execute("ALTER TABLE players ADD COLUMN wallet_started_at TEXT")
    # Saldo zerado por um débito por derrota: falta avisar o jogador e a administração
    # (notify.empty_wallets, depois do commit).
    if "wallet_empty_pending" not in columns:
        database.execute("ALTER TABLE players ADD COLUMN wallet_empty_pending INTEGER NOT NULL DEFAULT 0")
    # Foto do perfil (nome do arquivo em AVATARS_DIR - app/images.py).
    if "avatar" not in columns:
        database.execute("ALTER TABLE players ADD COLUMN avatar TEXT")


def ensure_live_columns(database):
    """Colunas novas das tabelas do Ao vivo (bancos criados antes delas)."""
    columns = {column["name"] for column in database.execute("PRAGMA table_info(iso_profiles)")}
    if columns and "catalog_iso_id" not in columns:
        database.execute("ALTER TABLE iso_profiles ADD COLUMN catalog_iso_id INTEGER "
                         "REFERENCES catalog_isos(id) ON DELETE SET NULL")
    columns = {column["name"] for column in database.execute("PRAGMA table_info(friendly_matches)")}
    if columns and "game_report" not in columns:
        database.execute("ALTER TABLE friendly_matches ADD COLUMN game_report INTEGER NOT NULL DEFAULT 0")
    if columns and "first_confirmed_by" not in columns:
        database.execute("ALTER TABLE friendly_matches ADD COLUMN first_confirmed_by INTEGER "
                         "REFERENCES players(id) ON DELETE SET NULL")
    if columns and "quitter_id" not in columns:
        database.execute("ALTER TABLE friendly_matches ADD COLUMN quitter_id INTEGER "
                         "REFERENCES players(id) ON DELETE SET NULL")
    if columns and "aggregate" not in columns:
        database.execute("ALTER TABLE friendly_matches ADD COLUMN aggregate INTEGER NOT NULL DEFAULT 0")
        # Os de antes da opção sempre mostraram a soma dos jogos: continuam assim.
        database.execute("UPDATE friendly_matches SET aggregate = 1")
    columns = {column["name"] for column in database.execute("PRAGMA table_info(tournament_matches)")}
    if columns and "quitter_id" not in columns:
        database.execute("ALTER TABLE tournament_matches ADD COLUMN quitter_id INTEGER "
                         "REFERENCES players(id) ON DELETE SET NULL")
    columns = {column["name"] for column in database.execute("PRAGMA table_info(live_matches)")}
    if columns and "game_report" not in columns:
        database.execute("ALTER TABLE live_matches ADD COLUMN game_report TEXT")
    if columns and "verified" not in columns:
        # As partidas de antes da regra ficam de fora (mandadas sem login garantido).
        database.execute("ALTER TABLE live_matches ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")


def ensure_tournament_columns(database):
    """Colunas novas de tournaments e tournament_stages (bancos criados antes delas)."""
    columns = {column["name"] for column in database.execute("PRAGMA table_info(tournaments)")}
    if not columns:
        return  # banco sem torneios ainda - o TOURNAMENTS_DDL cria a tabela completa
    for column in ("starts_on", "ends_on", "all_played_at", "image", "share_image"):
        if column not in columns:
            database.execute(f"ALTER TABLE tournaments ADD COLUMN {column} TEXT")
    if "friends_only" not in columns:
        database.execute("ALTER TABLE tournaments ADD COLUMN friends_only INTEGER NOT NULL DEFAULT 0")
    stage_columns = {column["name"] for column in database.execute("PRAGMA table_info(tournament_stages)")}
    if stage_columns and "all_played_at" not in stage_columns:
        database.execute("ALTER TABLE tournament_stages ADD COLUMN all_played_at TEXT")
    # CRC32:TAMANHO do arquivo (o content_id que o RetroArch TIERES manda no placar ao vivo - app/live.py).
    for table in ("tournament_isos", "catalog_isos"):
        table_columns = {column["name"] for column in database.execute(f"PRAGMA table_info({table})")}
        if table_columns and "content_id" not in table_columns:
            database.execute(f"ALTER TABLE {table} ADD COLUMN content_id TEXT")
    # Percentual do envio para o archive.org (barra de progresso - static/js/archive-progress.js).
    for table in ("tournament_isos", "catalog_isos", "files"):
        table_columns = {column["name"] for column in database.execute(f"PRAGMA table_info({table})")}
        if table_columns and "archive_progress" not in table_columns:
            database.execute(f"ALTER TABLE {table} ADD COLUMN archive_progress INTEGER")
    catalog_columns = {column["name"] for column in database.execute("PRAGMA table_info(catalog_isos)")}
    for column, kind in (("requested_by", "INTEGER REFERENCES players(id) ON DELETE SET NULL"),
                         ("approved_at", "TEXT"), ("share_image", "TEXT")):
        if catalog_columns and column not in catalog_columns:
            database.execute(f"ALTER TABLE catalog_isos ADD COLUMN {column} {kind}")
    # Imagem na alteração pendente da ISO e quem (outro responsável) a confirmou.
    revision_columns = {column["name"] for column in database.execute("PRAGMA table_info(catalog_iso_revisions)")}
    for column, kind in (("image_change", "INTEGER NOT NULL DEFAULT 0"), ("image", "TEXT"),
                         ("confirmed_by", "INTEGER REFERENCES players(id) ON DELETE SET NULL")):
        if revision_columns and column not in revision_columns:
            database.execute(f"ALTER TABLE catalog_iso_revisions ADD COLUMN {column} {kind}")


def allow_tournament_formats(database):
    """Bancos de antes do formato "Pontos corridos + mata-mata" têm o CHECK de
    tournaments.format sem ele. Como em allow_iso_memcards, a tabela é refeita
    (mesmos ids e colunas) com a definição atual do TOURNAMENTS_DDL; as outras
    tabelas continuam apontando para ela pelo nome."""
    row = database.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tournaments'").fetchone()
    if row is None or "league_knockout" in row["sql"]:
        return
    start = TOURNAMENTS_DDL.index("CREATE TABLE IF NOT EXISTS tournaments (")
    ddl = TOURNAMENTS_DDL[start:TOURNAMENTS_DDL.index(");", start) + 2]
    ddl = ddl.replace("CREATE TABLE IF NOT EXISTS tournaments (", "CREATE TABLE tournaments_new (")
    database.commit()
    database.execute("PRAGMA foreign_keys = OFF")
    try:
        # A tabela nova sozinha primeiro, para saber as colunas em comum (executescript faz COMMIT antes).
        database.executescript("DROP TABLE IF EXISTS tournaments_new;\n" + ddl)
        new_columns = {c["name"] for c in database.execute("PRAGMA table_info(tournaments_new)")}
        columns = ", ".join(c["name"] for c in database.execute("PRAGMA table_info(tournaments)")
                            if c["name"] in new_columns)
        database.executescript(f"""
            BEGIN;
            INSERT INTO tournaments_new ({columns}) SELECT {columns} FROM tournaments;
            DROP TABLE tournaments;
            ALTER TABLE tournaments_new RENAME TO tournaments;
            COMMIT;
        """)
    except Exception:
        database.rollback()  # a tabela antiga fica como estava
        raise
    finally:
        database.execute("PRAGMA foreign_keys = ON")
    broken = database.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise RuntimeError(f"tournaments: {len(broken)} referência(s) quebrada(s) depois de refazer a tabela")


def allow_iso_memcards(database):
    """Bancos de antes do "M. Card da ISO" têm memcards.player_id NOT NULL.
    O SQLite não muda a restrição de uma coluna, então a tabela é refeita com
    os mesmos ids - memcard_versions continua apontando para ela pelo nome."""
    columns = {column["name"]: column for column in database.execute("PRAGMA table_info(memcards)")}
    if "player_id" not in columns or not columns["player_id"]["notnull"]:
        return
    database.commit()
    database.execute("PRAGMA foreign_keys = OFF")
    try:
        database.executescript("""
            BEGIN;
            CREATE TABLE memcards_new (
                id INTEGER PRIMARY KEY,
                player_id INTEGER REFERENCES players(id) ON DELETE CASCADE,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE RESTRICT,
                current_version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (player_id, game_id)
            );
            INSERT INTO memcards_new (id, player_id, game_id, current_version, created_at, updated_at)
                SELECT id, player_id, game_id, current_version, created_at, updated_at FROM memcards;
            DROP TABLE memcards;
            ALTER TABLE memcards_new RENAME TO memcards;
            COMMIT;
        """)
    finally:
        database.execute("PRAGMA foreign_keys = ON")
    broken = database.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise RuntimeError(f"memcards: {len(broken)} referência(s) quebrada(s) depois de refazer a tabela")


def add_iso_card_slots(database):
    """O "M. Card da ISO" passou a ter dois cartões por jogo, um para cada
    slot: memcards.iso_slot (1 ou 2; nos cartões dos jogadores é sempre 1 e
    não quer dizer nada). O cartão da ISO que já existia fica no slot 1, e o
    índice único de um cartão da ISO por jogo vira um por jogo e slot."""
    columns = {column["name"] for column in database.execute("PRAGMA table_info(memcards)")}
    if not columns:
        return  # banco sem memory cards ainda - o PLAYERS_DDL cria a tabela já com a coluna
    if "iso_slot" not in columns:
        database.execute("ALTER TABLE memcards ADD COLUMN iso_slot INTEGER NOT NULL DEFAULT 1")
    database.execute("DROP INDEX IF EXISTS memcards_iso_idx")


# Partidas ao vivo (app/live.py): o placar que o RetroArch TIERES lê da RAM do
# Winning Eleven e o kailleraclient.dll manda durante a partida.
#  - live_matches: uma por partida (a chave junta ISO, jogadores e o frame do
#    pontapé inicial, igual em todos os PCs dela);
#  - live_match_reports: o último estado que cada PC mandou;
#  - iso_team_slots: nome/time de cada slot do jogo, por ISO (aprendido dos
#    jogos de campeonato ou cadastrado pelo admin).
LIVE_DDL = """
CREATE TABLE IF NOT EXISTS live_matches (
    id INTEGER PRIMARY KEY,
    match_key TEXT NOT NULL UNIQUE,
    content_id TEXT NOT NULL DEFAULT '',
    room TEXT NOT NULL DEFAULT '',
    players_json TEXT NOT NULL DEFAULT '[]',
    local_players_json TEXT NOT NULL DEFAULT '[]',
    visita_players_json TEXT NOT NULL DEFAULT '[]',
    kickoff_frame INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'live' CHECK (status IN ('live', 'finished', 'abandoned')),
    period INTEGER NOT NULL DEFAULT 0,
    clock_left INTEGER NOT NULL DEFAULT 0,
    half_frames INTEGER NOT NULL DEFAULT 18000,
    local_slot INTEGER,
    visita_slot INTEGER,
    local_goals INTEGER NOT NULL DEFAULT 0,
    visita_goals INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL DEFAULT '{}',
    reporters INTEGER NOT NULL DEFAULT 0,
    -- No fim: 1 = os PCs que mandaram o estado final concordam no placar (2 ou mais).
    agreed INTEGER NOT NULL DEFAULT 0,
    tournament_match_id INTEGER REFERENCES tournament_matches(id) ON DELETE SET NULL,
    friendly_id INTEGER REFERENCES friendly_matches(id) ON DELETE SET NULL,
    is_test INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    -- Jogo de campeonato encerrado: o placar lido virou o reporte do jogo
    -- ('reportado') ou o motivo de não ter virado (live.report_finished).
    game_report TEXT,
    -- 1 = cada jogador mandou o placar do próprio PC, logado na conta dele
    -- (live.refresh); só assim a partida aparece, entra em campeonato e reporta.
    verified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS live_matches_status_idx ON live_matches(status, updated_at);
CREATE INDEX IF NOT EXISTS live_matches_tournament_match_idx ON live_matches(tournament_match_id);

CREATE TABLE IF NOT EXISTS live_match_reports (
    live_match_id INTEGER NOT NULL REFERENCES live_matches(id) ON DELETE CASCADE,
    reporter TEXT NOT NULL COLLATE NOCASE,
    player_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
    frame INTEGER NOT NULL DEFAULT 0,
    final INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (live_match_id, reporter)
);

CREATE TABLE IF NOT EXISTS iso_team_slots (
    content_id TEXT NOT NULL,
    slot INTEGER NOT NULL,
    ram_name TEXT NOT NULL DEFAULT '',
    room TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT '' CHECK (source IN ('', 'aprendido', 'admin')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (content_id, slot)
);

-- Jogo base e campeonato de cada ISO (logos dos cards): aprendido do jogo de
-- campeonato ligado à partida, achado pelo nome do arquivo ou do admin.
CREATE TABLE IF NOT EXISTS iso_profiles (
    content_id TEXT PRIMARY KEY,
    game_title_id INTEGER REFERENCES game_titles(id) ON DELETE SET NULL,
    tournament_id INTEGER REFERENCES tournaments(id) ON DELETE SET NULL,
    catalog_iso_id INTEGER REFERENCES catalog_isos(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT '' CHECK (source IN ('', 'aprendido', 'admin')),
    updated_at TEXT NOT NULL
);
"""


SITE_SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS site_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def get_setting(key, default=None):
    """Ajuste do site feito pela administração (site_settings), ou default."""
    try:
        row = get_db().execute("SELECT value FROM site_settings WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default  # banco ainda sem a tabela
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO site_settings (key, value, updated_at) VALUES (?, ?, ?)", (key, value, now()))
    db.commit()


def init_db():
    db = get_db()
    with current_app.open_resource("schema.sql") as schema:
        db.executescript(schema.read().decode("utf-8"))
    db.executescript(PLAYERS_DDL)
    db.executescript(SITE_SETTINGS_DDL)
    ensure_player_columns(db)
    db.executescript(TOURNAMENTS_DDL)
    ensure_tournament_columns(db)
    db.executescript(LIVE_DDL)
    ensure_live_columns(db)
    from .tournament.teams import seed_teams
    from .tournament.games import seed_games
    seed_teams(db)
    seed_games(db)
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        init_db()
        click.echo("Banco de dados inicializado.")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.password_option()
    def create_admin(username, password):
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), now(), now()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            raise click.ClickException("Este usuário já existe.")
        click.echo("Administrador criado.")

    @app.cli.command("register-game")
    @click.argument("path")
    @click.argument("name", required=False)
    def register_game(path, name):
        """Cadastra um jogo do Memory Card online a partir do arquivo (.bin/.cue/.iso)."""
        from pathlib import Path
        from .memcards import content_id_for_path, get_or_create_game
        content_id = content_id_for_path(path)
        game = get_or_create_game(get_db(), content_id, name or Path(path).name)
        get_db().commit()
        click.echo(f"{game['name']}: {game['content_id']}")

    @app.cli.command("migrate-db")
    def migrate_db_command():
        migrate_db()
        click.echo("Banco de dados atualizado.")


def migrate_db():
    """Aplica a pequena migração necessária para bancos de testes iniciais.

    Chamada tanto pelo comando `flask --app wsgi migrate-db` (uso local) quanto
    diretamente via script Python em produção, onde a CLI do Flask não
    funciona sob Python 3.9 com as dependências vendorizadas (falta
    `importlib_metadata`) - mesmo motivo pelo qual o bootstrap chama `init_db()`
    direto em vez de `flask --app wsgi init-db`.
    """
    database = get_db()
    columns = {column["name"] for column in database.execute("PRAGMA table_info(championships)")}
    if "league_name" not in columns:
        database.execute("ALTER TABLE championships ADD COLUMN league_name TEXT")
    database.execute("DROP INDEX IF EXISTS one_current_championship")
    database.execute("""
        CREATE TABLE IF NOT EXISTS live_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            app_name TEXT NOT NULL DEFAULT '',
            game_name TEXT NOT NULL DEFAULT '',
            owner_name TEXT NOT NULL DEFAULT '',
            player_names TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'live' CHECK (status IN ('live', 'finished')),
            stored_name TEXT NOT NULL,
            bytes_received INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT
        )
    """)
    live_session_columns = {column["name"] for column in database.execute("PRAGMA table_info(live_sessions)")}
    if "owner_name" not in live_session_columns:
        database.execute("ALTER TABLE live_sessions ADD COLUMN owner_name TEXT NOT NULL DEFAULT ''")
    if "duration_seconds" not in live_session_columns:
        database.execute("ALTER TABLE live_sessions ADD COLUMN duration_seconds INTEGER")
    if "state_requested_at" not in live_session_columns:
        database.execute("ALTER TABLE live_sessions ADD COLUMN state_requested_at TEXT")
    if "last_sequence" not in live_session_columns:
        database.execute("ALTER TABLE live_sessions ADD COLUMN last_sequence INTEGER NOT NULL DEFAULT -1")
    if "lost_batches" not in live_session_columns:
        database.execute("ALTER TABLE live_sessions ADD COLUMN lost_batches INTEGER NOT NULL DEFAULT 0")
    file_columns = {column["name"] for column in database.execute("PRAGMA table_info(files)")}
    for column in ("md5", "archive_url", "archive_status", "archive_error", "archive_updated_at", "local_removed_at",
                   "content_id"):
        if column not in file_columns:
            database.execute(f"ALTER TABLE files ADD COLUMN {column} TEXT")
    # Antes do PLAYERS_DDL, que cria o memcards_iso_slot_idx na tabela refeita.
    allow_iso_memcards(database)
    add_iso_card_slots(database)
    database.executescript(PLAYERS_DDL)
    database.executescript(SITE_SETTINGS_DDL)
    ensure_player_columns(database)
    ensure_tournament_columns(database)  # antes de refazer a tabela, para copiar as colunas novas
    allow_tournament_formats(database)
    database.executescript(TOURNAMENTS_DDL)
    ensure_tournament_columns(database)
    database.executescript(LIVE_DDL)
    ensure_live_columns(database)
    # Jogos dos amistosos com placar lido do jogo de antes da tabela friendly_games.
    from .live import backfill_friendly_games
    backfill_friendly_games(database)
    from .tournament.teams import seed_teams
    from .tournament.games import seed_games
    seed_teams(database)
    seed_games(database)
    database.execute("CREATE INDEX IF NOT EXISTS live_sessions_game_name_idx ON live_sessions(game_name)")
    database.execute("CREATE INDEX IF NOT EXISTS live_sessions_replay_idx ON live_sessions(status, duration_seconds)")
    rows = database.execute(
        "SELECT session_id, started_at, ended_at FROM live_sessions WHERE status = 'finished' AND duration_seconds IS NULL AND ended_at IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"])
            ended = datetime.fromisoformat(row["ended_at"])
        except ValueError:
            continue
        duration = max(0, int((ended - started).total_seconds()))
        database.execute(
            "UPDATE live_sessions SET duration_seconds = ? WHERE session_id = ?",
            (duration, row["session_id"]),
        )
    database.commit()
