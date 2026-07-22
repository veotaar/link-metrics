-- Current sql file was generated after introspecting the database
-- If you want to run this migration please uncomment this code before executing migrations
/*
CREATE SEQUENCE "public"."links_short_code_sequence" INCREMENT BY 1 MINVALUE 0 MAXVALUE 218340105584895 START WITH 1 CACHE 1;--> statement-breakpoint
CREATE TABLE "users" (
	"id" uuid PRIMARY KEY DEFAULT uuidv7() NOT NULL,
	"email" varchar(254) NOT NULL,
	"password_hash" varchar(255) NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "users_email_is_canonical" CHECK ((octet_length((email)::text) <= 254) AND ((email)::text = lower((email)::text)) AND (((email)::text COLLATE "C") ~ '^[a-z0-9.!#$%&''*+/=?^_\x60{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$'::text))
);
--> statement-breakpoint
CREATE TABLE "links" (
	"short_code" varchar(8) PRIMARY KEY DEFAULT (short_code_from_sequence(nextval('links_short_code_sequence'::regclass)) COLLATE "C") NOT NULL,
	"original_url" text NOT NULL,
	"click_count" bigint DEFAULT 0 NOT NULL,
	"user_id" uuid NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_clicked_at" timestamp with time zone,
	CONSTRAINT "links_short_code_is_canonical" CHECK ((octet_length((short_code)::text) = 8) AND (((short_code)::text COLLATE "C") ~ '^[0-9A-Za-z]{8}$'::text)),
	CONSTRAINT "links_original_url_is_valid" CHECK (((octet_length(original_url) >= 1) AND (octet_length(original_url) <= 2048)) AND ((original_url COLLATE "C") ~ '^https?://([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?([/?#][!-~]*)?$'::text)),
	CONSTRAINT "links_click_count_is_nonnegative" CHECK (click_count >= 0)
);
--> statement-breakpoint
CREATE TABLE "schema_migrations" (
	"version" varchar PRIMARY KEY NOT NULL
);
--> statement-breakpoint
ALTER TABLE "links" ADD CONSTRAINT "links_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "idx_users_email" ON "users" USING btree ("email" text_ops);--> statement-breakpoint
CREATE INDEX "idx_links_user_id" ON "links" USING btree ("user_id" uuid_ops);
*/