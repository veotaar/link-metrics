-- migrate:up
ALTER TABLE links
    ALTER COLUMN click_count TYPE BIGINT,
    ADD COLUMN last_clicked_at TIMESTAMPTZ;

-- migrate:down
ALTER TABLE links
    DROP COLUMN last_clicked_at,
    ALTER COLUMN click_count TYPE INTEGER;
