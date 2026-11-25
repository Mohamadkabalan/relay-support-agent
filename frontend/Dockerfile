# syntax=docker/dockerfile:1
# Stages: deps (npm ci) -> builder (next build, standalone output) -> runtime (default target).
# dev runs `next dev`; compose.dev.yaml bind-mounts the source for hot reload.
# Debian slim rather than Alpine: same libc as most dev machines, so npm's native binaries match.

ARG NODE_IMAGE=node:26.13.1-trixie-slim

FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

FROM ${NODE_IMAGE} AS dev
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps --chown=node:node /app/node_modules ./node_modules
COPY --chown=node:node . .
# compose.dev.yaml mounts a volume at .next; it takes this directory's owner when first created.
RUN mkdir -p .next && chown node:node .next
USER node
EXPOSE 3000
CMD ["npm", "run", "dev"]

FROM ${NODE_IMAGE} AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM ${NODE_IMAGE} AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN groupadd --system --gid 10001 relay \
    && useradd --system --uid 10001 --gid relay --no-create-home --shell /usr/sbin/nologin relay
# Standalone output: a minimal server.js plus only the node_modules it needs.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
USER relay:relay
EXPOSE 3000
CMD ["node", "server.js"]
