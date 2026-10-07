FROM --platform=$BUILDPLATFORM node:22-alpine3.20 AS frontend

RUN apk add --no-cache git python3 \
    && git clone --depth 1 --branch v0.11.3 https://github.com/open-webui/open-webui.git /app \
    && test "$(git -C /app rev-parse HEAD)" = "2a960a59fe1dbbd35282f0556b3666d81102e781"

WORKDIR /app
RUN npm ci --force

COPY openwebui_patch/apply_chatmenu_exports.py /tmp/apply_chatmenu_exports.py
COPY openwebui_patch/apply_evaluation_observability.py /tmp/apply_evaluation_observability.py
COPY openwebui_patch/apply_manual_retry.py /tmp/apply_manual_retry.py
COPY openwebui_patch/apply_plotly_asset.py /tmp/apply_plotly_asset.py
COPY openwebui_patch/evaluation_message_sync.js /tmp/evaluation_message_sync.js
COPY openwebui_patch/plotly_asset_normalize.js /tmp/plotly_asset_normalize.js
COPY openwebui_patch/plotly_asset_route.py /tmp/plotly_asset_route.py
RUN python3 /tmp/apply_chatmenu_exports.py /app \
    && python3 /tmp/apply_evaluation_observability.py --frontend \
    && python3 /tmp/apply_manual_retry.py /app \
    && python3 /tmp/apply_plotly_asset.py --frontend /app \
    && rm /tmp/apply_chatmenu_exports.py /tmp/apply_evaluation_observability.py /tmp/apply_manual_retry.py /tmp/apply_plotly_asset.py /tmp/evaluation_message_sync.js /tmp/plotly_asset_normalize.js /tmp/plotly_asset_route.py

ENV APP_BUILD_HASH=2a960a59fe1dbbd35282f0556b3666d81102e781
RUN npm run build

FROM ghcr.io/open-webui/open-webui:v0.11.3 AS runtime

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir "playwright==1.63.0" \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/ms-playwright

COPY openwebui_patch/apply_safe_provider_errors.py /tmp/apply_safe_provider_errors.py
RUN python /tmp/apply_safe_provider_errors.py && rm /tmp/apply_safe_provider_errors.py

COPY openwebui_patch/apply_evaluation_observability.py /tmp/apply_evaluation_observability.py
COPY openwebui_patch/evaluation_observability.py /tmp/evaluation_observability.py
COPY openwebui_patch/apply_plotly_asset.py /tmp/apply_plotly_asset.py
COPY openwebui_patch/plotly_asset_route.py /tmp/plotly_asset_route.py
RUN python /tmp/apply_evaluation_observability.py --backend \
    && python /tmp/apply_plotly_asset.py --backend /app \
    && rm /tmp/apply_evaluation_observability.py /tmp/evaluation_observability.py /tmp/apply_plotly_asset.py /tmp/plotly_asset_route.py

COPY --from=frontend /app/build /app/build
COPY --from=frontend /app/LICENSE /app/LICENSE
COPY --from=frontend /app/LICENSE_NOTICE /app/LICENSE_NOTICE
COPY --from=frontend /app/LICENSE_HISTORY /app/LICENSE_HISTORY
