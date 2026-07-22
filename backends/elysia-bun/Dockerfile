FROM oven/bun:1.3.14-slim@sha256:d56a2534ffd262e92c12fd3249d3924d296d97086da773f821d7d0477435ea04 AS install

WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile --production

FROM oven/bun:1.3.14-slim@sha256:d56a2534ffd262e92c12fd3249d3924d296d97086da773f821d7d0477435ea04

ENV NODE_ENV=production
WORKDIR /app
COPY --from=install --chown=bun:bun /app/node_modules ./node_modules
COPY --chown=bun:bun package.json ./
COPY --chown=bun:bun drizzle/schema.ts ./drizzle/schema.ts
COPY --chown=bun:bun src ./src

USER bun
EXPOSE 3000
CMD ["bun", "run", "src/server.ts"]
