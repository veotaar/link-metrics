import { pgTable, uniqueIndex, check, uuid, varchar, timestamp, index, foreignKey, text, integer } from "drizzle-orm/pg-core"
import { sql } from "drizzle-orm"



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
	shortCode: varchar("short_code", { length: 10 }).primaryKey().notNull(),
	originalUrl: text("original_url").notNull(),
	clickCount: integer("click_count").default(0).notNull(),
	userId: uuid("user_id").notNull(),
	createdAt: timestamp("created_at", { withTimezone: true, mode: 'string' }).defaultNow().notNull(),
}, (table) => [
	index("idx_links_user_id").using("btree", table.userId.asc().nullsLast().op("uuid_ops")),
	foreignKey({
			columns: [table.userId],
			foreignColumns: [users.id],
			name: "links_user_id_fkey"
		}).onDelete("cascade"),
]);

export const schemaMigrations = pgTable("schema_migrations", {
	version: varchar().primaryKey().notNull(),
});
