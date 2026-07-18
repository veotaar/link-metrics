# Allow idiomatic data access

Contenders may use language-appropriate data access—such as Drizzle schemas introspected from PostgreSQL, generated queries, generated models, or direct SQL—provided they own no DDL or migrations and pass database integration checks against a freshly migrated catalog. This preserves migrations as the schema authority without forcing an unnatural code-generation technique across languages.
