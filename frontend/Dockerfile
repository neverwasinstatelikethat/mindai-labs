FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:22-alpine
ENV NODE_ENV=production HOST=0.0.0.0 PORT=3000 INTERNAL_API_URL=http://backend:8000
RUN addgroup -S mindai && adduser -S mindai -G mindai
WORKDIR /app
COPY --from=builder --chown=mindai:mindai /app/build ./build
COPY --from=builder --chown=mindai:mindai /app/package.json ./package.json
USER mindai
EXPOSE 3000
HEALTHCHECK --interval=20s --timeout=5s --retries=5 \
  CMD wget -q -O - http://127.0.0.1:3000/ >/dev/null || exit 1
CMD ["node", "build"]
