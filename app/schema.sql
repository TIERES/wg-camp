PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS championships (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    league_name TEXT,
    description TEXT NOT NULL DEFAULT '',
    arena17_url TEXT,
    start_date TEXT,
    end_date TEXT,
    is_published INTEGER NOT NULL DEFAULT 1 CHECK (is_published IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    championship_id INTEGER NOT NULL REFERENCES championships(id) ON DELETE RESTRICT,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    stored_name TEXT NOT NULL UNIQUE,
    original_filename TEXT NOT NULL,
    file_type TEXT NOT NULL CHECK (file_type IN ('iso', 'rom', 'patch', 'update', 'other')),
    file_size INTEGER NOT NULL CHECK (file_size >= 0),
    sha256 TEXT,
    is_published INTEGER NOT NULL DEFAULT 1 CHECK (is_published IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS files_championship_idx ON files(championship_id);

CREATE TRIGGER IF NOT EXISTS files_one_per_championship
BEFORE INSERT ON files
WHEN EXISTS (SELECT 1 FROM files WHERE championship_id = NEW.championship_id)
BEGIN
    SELECT RAISE(ABORT, 'Cada campeonato pode ter apenas um arquivo.');
END;
