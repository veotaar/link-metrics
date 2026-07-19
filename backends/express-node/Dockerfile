FROM node:26.2.0-bookworm-slim@sha256:ba2f9edd0785ee291c1a5287764cb41fdb7922336f878993f9d58622613e67a8 AS build

RUN npm install --global pnpm@11.5.1
WORKDIR /app

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY tsconfig.json ./
COPY src ./src
RUN pnpm build && pnpm prune --prod

FROM node:26.2.0-bookworm-slim@sha256:ba2f9edd0785ee291c1a5287764cb41fdb7922336f878993f9d58622613e67a8

ENV NODE_ENV=production
WORKDIR /app
COPY --from=build --chown=node:node /app/package.json ./
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/dist ./dist

USER node
EXPOSE 3000
CMD ["node", "dist/server.js"]
