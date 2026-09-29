# base digest 已以 Docker build 實測可用；升級需重新審核並更新 digest。
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /sandbox

COPY sandbox/requirements.txt /sandbox/requirements.txt
COPY sandbox/bootstrap.py /sandbox/bootstrap.py
RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        fonts-noto-cjk=1:20220127+repack1-1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --disable-pip-version-check \
    --require-hashes \
    -r /sandbox/requirements.txt \
    && mkdir -p /usr/local/share/fonts \
    && python -c "from fontTools.ttLib import TTCollection; c=TTCollection('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'); next(f for f in c.fonts if f['name'].getDebugName(1) == 'Noto Sans CJK TC').save('/usr/local/share/fonts/NotoSansCJKTC-Regular.otf')" \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin sandbox \
    && mkdir -p /sandbox/input /sandbox/output /tmp/matplotlib \
    && chown -R sandbox:sandbox /sandbox /tmp/matplotlib

USER 1000:1000
