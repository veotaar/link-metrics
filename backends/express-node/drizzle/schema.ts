import { pgTable, uniqueIndex, check, uuid, varchar, timestamp, index, foreignKey, text, bigint, pgSequence } from "drizzle-orm/pg-core"
import { sql } from "drizzle-orm"


export const linksShortCodeSequence = pgSequence("links_short_code_sequence", {  startWith: "1", increment: "1", minValue: "0", maxValue: "218340105584895", cache: "1", cycle: false })

export const users = pgTable("users", {
	id: uuid().default(sql`uuidv7()`).primaryKey().notNull(),
	email: varchar({ length: 254 }).notNull(),
	passwordHash: varchar("password_hash", { length: 255 }).notNull(),
	createdAt: timestamp("created_at", { withTimezone: true, mode: 'string' }).defaultNow().notNull(),
}, (table) => [
	uniqueIndex("idx_users_email").using("btree", table.email.asc().nullsLast().op("text_ops")),
	check("users_email_is_canonical", sql`(octet_length((email)::text) <= 254) AND ((email)::text = lower((email)::text)) AND (((email)::text COLLATE "C") ~ '^[a-z0-9.!#$%&''*+/=?^_\x60{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$'::text)`),
]);

export const links = pgTable("links", {
	shortCode: varchar("short_code", { length: 8 }).default(sql`(short_code_from_sequence(nextval(\'links_short_code_sequence\'::regclass)) COLLATE "C")`).primaryKey().notNull(),
	originalUrl: text("original_url").notNull(),
	// You can use { mode: "bigint" } if numbers are exceeding js number limitations
	clickCount: bigint("click_count", { mode: "number" }).default(0).notNull(),
	userId: uuid("user_id").notNull(),
	createdAt: timestamp("created_at", { withTimezone: true, mode: 'string' }).defaultNow().notNull(),
	lastClickedAt: timestamp("last_clicked_at", { withTimezone: true, mode: 'string' }),
}, (table) => [
	index("idx_links_user_id").using("btree", table.userId.asc().nullsLast().op("uuid_ops")),
	foreignKey({
			columns: [table.userId],
			foreignColumns: [users.id],
			name: "links_user_id_fkey"
		}).onDelete("cascade"),
	check("links_short_code_is_canonical", sql`(octet_length((short_code)::text) = 8) AND (((short_code)::text COLLATE "C") ~ '^[0-9A-Za-z]{8}$'::text)`),
	check("links_original_url_is_valid", sql`((octet_length(original_url) >= 1) AND (octet_length(original_url) <= 2048)) AND ((original_url COLLATE "C") ~ '^https?://([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?([/?#][!-~]*)?$'::text)`),
	check("links_click_count_is_nonnegative", sql`click_count >= 0`),
]);

export const schemaMigrations = pgTable("schema_migrations", {
	version: varchar().primaryKey().notNull(),
});
