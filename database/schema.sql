\restrict dbmate

-- Dumped from database version 18.4 (Debian 18.4-1.pgdg12+1)
-- Dumped by pg_dump version 18.4

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pg_prewarm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_prewarm WITH SCHEMA pg_catalog;


--
-- Name: EXTENSION pg_prewarm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_prewarm IS 'prewarm relation data';


--
-- Name: short_code_from_sequence(bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.short_code_from_sequence(sequence_value bigint) RETURNS character varying
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
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
$$;


--
-- Name: links_short_code_sequence; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.links_short_code_sequence
    START WITH 1
    INCREMENT BY 1
    MINVALUE 0
    MAXVALUE 218340105584895
    CACHE 1;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.links (
    short_code character varying(8) DEFAULT (public.short_code_from_sequence(nextval('public.links_short_code_sequence'::regclass)) COLLATE "C") NOT NULL,
    original_url text NOT NULL,
    click_count integer DEFAULT 0 NOT NULL,
    user_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT links_click_count_is_nonnegative CHECK ((click_count >= 0)),
    CONSTRAINT links_original_url_is_valid CHECK ((((octet_length(original_url) >= 1) AND (octet_length(original_url) <= 2048)) AND ((original_url COLLATE "C") ~ '^https?://([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?([/?#][!-~]*)?$'::text))),
    CONSTRAINT links_short_code_is_canonical CHECK (((octet_length((short_code)::text) = 8) AND (((short_code)::text COLLATE "C") ~ '^[0-9A-Za-z]{8}$'::text)))
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version character varying NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid DEFAULT uuidv7() NOT NULL,
    email character varying(254) NOT NULL,
    password_hash character varying(255) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT users_email_is_canonical CHECK (((octet_length((email)::text) <= 254) AND ((email)::text = lower((email)::text)) AND (((email)::text COLLATE "C") ~ '^[a-z0-9.!#$%&''*+/=?^_\x60{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$'::text)))
);


--
-- Name: links links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.links
    ADD CONSTRAINT links_pkey PRIMARY KEY (short_code);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: idx_links_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_links_user_id ON public.links USING btree (user_id);


--
-- Name: idx_users_email; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_users_email ON public.users USING btree (email);


--
-- Name: links links_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.links
    ADD CONSTRAINT links_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict dbmate


--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('20260604222601'),
    ('20260719000100'),
    ('20260719000200');
