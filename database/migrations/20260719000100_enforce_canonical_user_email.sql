-- migrate:up
ALTER TABLE users
    ALTER COLUMN email TYPE VARCHAR(254),
    ADD CONSTRAINT users_email_is_canonical CHECK (
        octet_length(email) <= 254
        AND email = lower(email)
        AND email COLLATE "C" ~ '^[a-z0-9.!#$%&''*+/=?^_\x60{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$'
    );

-- migrate:down
ALTER TABLE users
    DROP CONSTRAINT users_email_is_canonical,
    ALTER COLUMN email TYPE VARCHAR(255);
