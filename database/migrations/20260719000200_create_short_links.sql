-- migrate:up
CREATE SEQUENCE links_short_code_sequence
    AS BIGINT
    MINVALUE 0
    MAXVALUE 218340105584895
    START WITH 1
    NO CYCLE;

CREATE FUNCTION short_code_from_sequence(sequence_value BIGINT)
RETURNS VARCHAR(8)
LANGUAGE SQL
IMMUTABLE
STRICT
PARALLEL SAFE
AS $function$
    WITH RECURSIVE digits (remaining, encoded) AS (
        VALUES (sequence_value, ''::TEXT)

        UNION ALL

        SELECT
            remaining / 62,
            substr(
                '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz',
                (remaining % 62)::INTEGER + 1,
                1
            ) || encoded
        FROM digits
        WHERE remaining > 0
    )
    SELECT lpad(encoded, 8, '0')
    FROM digits
    WHERE remaining = 0
$function$;

ALTER TABLE links
    ALTER COLUMN short_code TYPE VARCHAR(8),
    ALTER COLUMN short_code SET DEFAULT (
        short_code_from_sequence(nextval('links_short_code_sequence')) COLLATE "C"
    ),
    ADD CONSTRAINT links_short_code_is_canonical CHECK (
        octet_length(short_code) = 8
        AND short_code COLLATE "C" ~ '^[0-9A-Za-z]{8}$'
    ),
    ADD CONSTRAINT links_original_url_is_valid CHECK (
        octet_length(original_url) BETWEEN 1 AND 2048
        AND original_url COLLATE "C" ~ '^https?://([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?([/?#][!-~]*)?$'
    ),
    ADD CONSTRAINT links_click_count_is_nonnegative CHECK (click_count >= 0);

-- migrate:down
ALTER TABLE links
    DROP CONSTRAINT links_click_count_is_nonnegative,
    DROP CONSTRAINT links_original_url_is_valid,
    DROP CONSTRAINT links_short_code_is_canonical,
    ALTER COLUMN short_code DROP DEFAULT,
    ALTER COLUMN short_code TYPE VARCHAR(10);

DROP FUNCTION short_code_from_sequence(BIGINT);
DROP SEQUENCE links_short_code_sequence;
