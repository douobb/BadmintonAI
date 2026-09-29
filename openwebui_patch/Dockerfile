FROM ghcr.io/open-webui/open-webui:v0.11.3

COPY openwebui_patch/apply_safe_provider_errors.py /tmp/apply_safe_provider_errors.py
RUN python /tmp/apply_safe_provider_errors.py && rm /tmp/apply_safe_provider_errors.py
