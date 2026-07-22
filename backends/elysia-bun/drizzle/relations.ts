import { relations } from "drizzle-orm/relations";
import { users, links } from "./schema";

export const linksRelations = relations(links, ({one}) => ({
	user: one(users, {
		fields: [links.userId],
		references: [users.id]
	}),
}));

export const usersRelations = relations(users, ({many}) => ({
	links: many(links),
}));