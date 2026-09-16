FROM node:22-bookworm-slim
WORKDIR /app
# Exact upstream MCP version; the package pins its Playwright dependency.
RUN npm install --omit=dev --save-exact @playwright/mcp@0.0.81 \
    && PLAYWRIGHT_BROWSERS_PATH=/opt/browsers npx playwright install --with-deps chromium \
    && chmod -R a+rX /opt/browsers
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
USER node
ENTRYPOINT ["./node_modules/.bin/playwright-mcp"]
