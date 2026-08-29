FROM mcr.microsoft.com/playwright@sha256:9bd26ad900bb5e0f4dee75839e957a89ae89c2b7ab1e76050e559790e946b948

ENV NODE_ENV=production \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
WORKDIR /app

COPY container/package.json container/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force

COPY --chmod=0444 render_guizang_brief.mjs /app/render_guizang_brief.mjs
COPY --chmod=0444 render-guizang-worker.mjs /app/render-guizang-worker.mjs
COPY --chmod=0444 third_party/guizang-social-card-skill/template-swiss-card.html /app/template-swiss-card.html
COPY --chmod=0444 third_party/guizang-social-card-skill/LICENSE /app/GUIZANG-LICENSE
COPY --chmod=0444 third_party/guizang-social-card-skill/UPSTREAM.md /app/GUIZANG-UPSTREAM.md

USER pwuser
ENTRYPOINT ["node", "/app/render-guizang-worker.mjs"]
