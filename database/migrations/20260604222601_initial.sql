-- migrate:up
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuidv7(),
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_users_email ON users USING btree (email);

CREATE TABLE IF NOT EXISTS links (
    short_code VARCHAR(10) PRIMARY KEY,
    original_url TEXT NOT NULL,
    click_count INTEGER NOT NULL DEFAULT 0,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_links_user_id ON links USING btree (user_id);

-- migrate:down
DROP INDEX IF EXISTS idx_links_user_id;

DROP TABLE IF EXISTS links;

DROP INDEX IF EXISTS idx_users_email;

DROP TABLE IF EXISTS users;
