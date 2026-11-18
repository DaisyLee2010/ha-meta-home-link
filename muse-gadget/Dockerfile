ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache bluez dbus \
    && pip install --no-cache-dir aiohttp cryptography dbus-next

COPY app/muse_gadget /app/muse_gadget
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD ["/run.sh"]
